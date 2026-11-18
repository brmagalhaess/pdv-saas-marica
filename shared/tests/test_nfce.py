"""NFC-e 4.00: chave e DV, XML, C14N, QR Code v2/v3, assinatura, retornos da SEFAZ e regra de impressão."""
import base64
import hashlib
import os
import random
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from shared.fiscal import compute_document
from shared.nfce import build_nfce, keys, messages, qrcode, responses, signature, webservices
from shared.nfce.builder import HOMOLOGATION_TEXT, NfceError, digest_value
from shared.nfce.payments import print_decision, tpag
from shared.nfce.testing import (make_certificate, ret_authorization, ret_consult, ret_event, ret_inutilization,
                                 ret_status)
from shared.nfce.xml import NFE_NS, E, XMLError, canonical, from_etree, parse, tostring

ISSUER = dict(document="11222333000181", name="Mercado Teste LTDA", trade_name="Mercado Teste",
              address_line="Rua das Flores", address_number="100", address_neighborhood="Centro",
              address_city="Sao Paulo", city_ibge_code="3550308", address_zip="01001000", address_state="SP",
              state_registration="110042490114", crt=1)
SIMPLES = {"origin": "0", "ncm": "21069090", "cfop": "5102", "icms_csosn": "102", "pis_cst": "49", "cofins_cst": "49"}
NORMAL = {"origin": "0", "ncm": "21069090", "cfop": "5102", "icms_cst": "00", "icms_rate": "18", "fcp_rate": "2",
          "pis_cst": "01", "pis_rate": "0.65", "cofins_cst": "01", "cofins_rate": "3"}
WHEN = datetime(2026, 11, 18, 13, 0, 5, tzinfo=timezone.utc)


def engine(fields=SIMPLES, crt=1, items=None, **kw):
    items = items or [{"product_id": "p1", "code": "SKU1", "description": "Biscoito & <recheado>",
                       "barcode": "7891000100103", "unit": "UN", "quantity_milli": 2000, "unit_price_cents": 899,
                       "discount_cents": 100, "fiscal": fields}]
    return compute_document({"crt": crt, "uf": "SP"}, items, model=65, **kw)


def build(doc=None, environment="production", **kw):
    doc = doc or engine()
    total = doc["totals"]["total_cents"]
    options = dict(series=1, number=42, issued_at=WHEN, environment=environment, cnf="12121212",
                   payments=[{"method": "cash", "amount_cents": total + 302}], change_cents=302)
    options.update(kw)
    return build_nfce(dict(ISSUER, crt=doc["issuer"]["crt"]), doc, **options)


class KeyTests(unittest.TestCase):
    def test_key_layout_and_check_digit(self):
        key = keys.access_key("SP", "2611", "11222333000181", 65, 1, 42, 1, "12121212")
        self.assertEqual(len(key), 44)
        self.assertEqual(key[:2], "35")
        self.assertEqual(key[2:6], "2611")
        self.assertEqual(key[20:22], "65")
        self.assertEqual(key[22:25], "001")
        self.assertEqual(key[25:34], "000000042")
        self.assertEqual(key[34], "1")
        self.assertEqual(key[35:43], "12121212")
        self.assertTrue(keys.valid_key(key))
        broken = key[:10] + str((int(key[10]) + 1) % 10) + key[11:]
        self.assertFalse(keys.valid_key(broken))
        self.assertEqual(keys.split_key(key)["number"], 42)

    def test_mod11_rest_zero_or_one_gives_zero(self):
        # Weights 2..9 from the right: "1" -> 2 -> rest 2 -> 9; build values whose rest is 0 and 1.
        self.assertEqual(keys.mod11("0"), 0)
        self.assertEqual(keys.mod11("1"), 9)
        for _ in range(200):
            base = "".join(random.choice("0123456789") for _ in range(43))
            total = sum(int(c) * w for c, w in zip(reversed(base), [2, 3, 4, 5, 6, 7, 8, 9] * 6))
            rest = total % 11
            self.assertEqual(keys.mod11(base), 0 if rest in (0, 1) else 11 - rest)

    def test_alphanumeric_cnpj_key(self):
        key = keys.access_key("SP", "2607", "12ABC34501DE35", 65, 1, 1, 1, "87651234")
        self.assertTrue(keys.valid_key(key))
        self.assertEqual(key[6:20], "12ABC34501DE35")

    def test_random_cnf_avoids_forbidden_patterns(self):
        rng = random.Random(3)
        for number in (1, 12345678, 99999999):
            for _ in range(300):
                code = keys.random_cnf(number, rng)
                self.assertEqual(len(code), 8)
                self.assertNotEqual(code, str(number % 10**8).zfill(8))
                self.assertGreater(len(set(code)), 1)
                self.assertNotIn(code, ("12345678", "87654321"))


class XmlTests(unittest.TestCase):
    def test_ide_emit_and_key(self):
        draft = build()
        inf = draft.infnfe
        self.assertEqual(inf.attrs, {"Id": "NFe" + draft.key, "versao": "4.00"})
        ide = {child.tag: child.text for child in inf.find("ide").children}
        self.assertEqual(ide["cUF"], "35")
        self.assertEqual(ide["cNF"], "12121212")
        self.assertEqual(ide["mod"], "65")
        self.assertEqual(ide["serie"], "1")
        self.assertEqual(ide["nNF"], "42")
        self.assertEqual(ide["dhEmi"], "2026-11-18T10:00:05-03:00")  # SP time zone
        self.assertEqual((ide["tpImp"], ide["tpEmis"], ide["tpAmb"]), ("4", "1", "1"))
        self.assertEqual((ide["finNFe"], ide["indFinal"], ide["indPres"], ide["procEmi"]), ("1", "1", "1", "0"))
        self.assertEqual(ide["cDV"], draft.key[-1])
        self.assertNotIn("indIntermed", ide)
        self.assertEqual(inf.findtext("emit/CNPJ"), "11222333000181")
        self.assertEqual(inf.findtext("emit/enderEmit/cMun"), "3550308")
        self.assertEqual(inf.findtext("emit/IE"), "110042490114")
        self.assertEqual(inf.findtext("emit/CRT"), "1")
        self.assertIsNone(inf.find("dest"))
        self.assertEqual(inf.findtext("transp/modFrete"), "9")
        tags = [child.tag for child in inf.children]
        self.assertEqual(tags, ["ide", "emit", "det", "total", "transp", "pag"])

    def test_time_zone_of_the_uf(self):
        issuer = dict(ISSUER, address_state="AM", city_ibge_code="1302603", state_registration="041234560")
        doc = engine()
        draft = build_nfce(issuer, doc, series=1, number=1, issued_at=WHEN, environment="production",
                           payments=[{"method": "pix", "amount_cents": doc["totals"]["total_cents"]}])
        self.assertEqual(draft.infnfe.findtext("ide/dhEmi"), "2026-11-18T09:00:05-04:00")
        self.assertEqual(draft.key[:2], "13")

    def test_item_values_come_from_the_engine(self):
        draft = build()
        det = draft.infnfe.find("det")
        self.assertEqual(det.attrs, {"nItem": "1"})
        prod = det.find("prod")
        self.assertEqual(prod.findtext("xProd"), "Biscoito & <recheado>")
        self.assertEqual(prod.findtext("NCM"), "21069090")
        self.assertEqual((prod.findtext("qCom"), prod.findtext("vUnCom"), prod.findtext("vProd")), ("2.0000", "8.99", "17.98"))
        self.assertEqual(prod.findtext("vDesc"), "1.00")
        self.assertEqual(prod.findtext("cEAN"), "7891000100103")
        self.assertEqual(prod.findtext("indTot"), "1")
        self.assertEqual(det.findtext("imposto/ICMS/ICMSSN102/CSOSN"), "102")
        self.assertEqual(det.findtext("imposto/PIS/PISOutr/CST"), "49")
        self.assertIn("&amp; &lt;recheado&gt;", tostring(draft.nfe))

    def test_regime_normal_icms00_with_fcp_and_totals(self):
        doc = engine(NORMAL, crt=3)
        draft = build(doc)
        icms = draft.infnfe.find("det/imposto/ICMS/ICMS00")
        self.assertEqual([c.tag for c in icms.children], ["orig", "CST", "modBC", "vBC", "pICMS", "vICMS", "pFCP", "vFCP"])
        self.assertEqual((icms.findtext("vBC"), icms.findtext("pICMS"), icms.findtext("vICMS")), ("16.98", "18.00", "3.06"))
        total = draft.infnfe.find("total/ICMSTot")
        self.assertEqual(total.findtext("vICMS"), "3.06")
        self.assertEqual(total.findtext("vFCP"), "0.34")
        self.assertEqual(total.findtext("vNF"), "16.98")
        self.assertEqual(total.findtext("vPIS"), "0.11")
        self.assertEqual([c.tag for c in total.children][:4], ["vBC", "vICMS", "vICMSDeson", "vFCP"])
        self.assertEqual(draft.infnfe.findtext("det/imposto/PIS/PISAliq/pPIS"), "0.65")

    def test_st_retained_csosn_500(self):
        fields = dict(SIMPLES, icms_csosn="500", cfop="5405", cest="1700100", st_retained_bc_unit="10.00",
                      st_retained_rate="18", st_retained_substitute_unit="1.00")
        draft = build(engine(fields))
        group = draft.infnfe.find("det/imposto/ICMS/ICMSSN500")
        self.assertEqual([c.tag for c in group.children], ["orig", "CSOSN", "vBCSTRet", "pST", "vICMSSubstituto", "vICMSSTRet"])
        self.assertEqual(draft.infnfe.findtext("det/prod/CEST"), "1700100")

    def test_homologation_texts_and_consumer(self):
        draft = build(environment="homologation", customer={"document": "529.982.247-25", "name": "Maria"})
        self.assertEqual(draft.infnfe.findtext("ide/tpAmb"), "2")
        self.assertEqual(draft.infnfe.findtext("det/prod/xProd"), HOMOLOGATION_TEXT)
        self.assertEqual(draft.infnfe.findtext("dest/CPF"), "52998224725")
        self.assertEqual(draft.infnfe.findtext("dest/xNome"), HOMOLOGATION_TEXT)
        self.assertEqual(draft.infnfe.findtext("dest/indIEDest"), "9")
        self.assertEqual(draft.dest_document, "52998224725")

    def test_consumer_cnpj_and_invalid_document(self):
        draft = build(customer={"document": "11.444.777/0001-61"})
        self.assertEqual(draft.infnfe.findtext("dest/CNPJ"), "11444777000161")
        self.assertIsNone(draft.infnfe.find("dest/xNome"))
        with self.assertRaises(NfceError):
            build(customer={"document": "123.456.789-00"})

    def test_payments_tpag_change_and_card_group(self):
        doc = engine()
        total = doc["totals"]["total_cents"]
        payments = [{"method": "card_debit", "amount_cents": 500}, {"method": "pix", "amount_cents": 300},
                    {"method": "other", "amount_cents": 200, "payment_method_id": "v1"},
                    {"method": "credit_account", "amount_cents": 100},
                    {"method": "cash", "amount_cents": total - 1100 + 50}]
        draft = build(doc, payments=payments, change_cents=50, payment_names={"v1": "Vale refeição"})
        pag = draft.infnfe.find("pag")
        details = pag.findall("detPag")
        self.assertEqual([d.findtext("tPag") for d in details], ["04", "17", "99", "05", "01"])
        self.assertEqual(details[0].findtext("card/tpIntegra"), "2")
        self.assertIsNone(details[1].find("card"))
        self.assertEqual(details[2].findtext("xPag"), "Vale refeição")
        self.assertEqual(pag.findtext("vTroco"), "0.50")
        self.assertEqual(tpag("card_credit"), "03")
        with self.assertRaises(NfceError):
            build(doc, payments=[{"method": "cash", "amount_cents": 10}], change_cents=0)

    def test_lei_12741_and_tech_responsible(self):
        doc = engine(approx=lambda ncm, ex: {"federal_national": "10", "federal_imported": "20", "state": "18",
                                             "municipal": "0", "version": "26.2.A"})
        draft = build(doc, tech={"cnpj": "11444777000161", "contact": "Suporte", "email": "s@example.test",
                                 "phone": "(21) 99999-0000"})
        text = draft.infnfe.findtext("infAdic/infCpl")
        self.assertIn("Federal", text)
        self.assertIn("Fonte: IBPT 26.2.A", text)
        self.assertEqual(draft.infnfe.findtext("total/ICMSTot/vTotTrib"), "4.76")
        self.assertEqual(draft.infnfe.findtext("infRespTec/CNPJ"), "11444777000161")
        self.assertEqual(draft.infnfe.findtext("infRespTec/fone"), "21999990000")
        self.assertIsNone(build(doc, tech={"cnpj": "11444777000161"}).infnfe.find("infRespTec"))

    def test_contingency_fields_and_key_type(self):
        draft = build(emission_type=9, contingency={"at": WHEN, "reason": "Sem internet no caixa ha alguns minutos"})
        ide = draft.infnfe.find("ide")
        self.assertEqual(ide.findtext("tpEmis"), "9")
        self.assertEqual(draft.key[34], "9")
        self.assertEqual(ide.findtext("dhCont"), "2026-11-18T10:00:05-03:00")
        self.assertEqual([c.tag for c in ide.children][-2:], ["dhCont", "xJust"])
        with self.assertRaises(NfceError):
            build(emission_type=9)

    def test_refuses_engine_document_not_ready_and_bad_issuer(self):
        doc = engine(dict(SIMPLES, ncm=""))
        with self.assertRaises(NfceError) as failure:
            build(doc)
        self.assertTrue(failure.exception.issues)
        with self.assertRaises(NfceError):
            build_nfce(dict(ISSUER, document="11222333000180"), engine(), series=1, number=1, issued_at=WHEN,
                       environment="production", payments=[{"method": "pix", "amount_cents": 1698}])
        with self.assertRaises(NfceError):
            build_nfce(dict(ISSUER, city_ibge_code=""), engine(), series=1, number=1, issued_at=WHEN,
                       environment="production", payments=[{"method": "pix", "amount_cents": 1698}])

    def test_canonical_form_matches_standard_c14n(self):
        draft = build(customer={"document": "52998224725", "name": "José \"Zé\" <Teste>"})
        draft.supplement("https://example.test/qr?p=1|2", "https://example.test/consulta")
        text = tostring(draft.nfe)
        self.assertEqual(ET.canonicalize(text), text)
        sub = canonical(draft.infnfe, NFE_NS)
        self.assertTrue(sub.startswith('<infNFe xmlns="http://www.portalfiscal.inf.br/nfe" Id="NFe'))
        self.assertEqual(ET.canonicalize(sub), sub)
        # Parse and render back: byte-identical (what the cloud does with a register XML).
        self.assertEqual(tostring(from_etree(parse(text))), text)
        self.assertEqual(digest_value(from_etree(parse(text)).find("infNFe")), draft.digest_value)
        self.assertNotIn("\n", text)

    def test_parse_refuses_dtd(self):
        with self.assertRaises(XMLError):
            parse('<!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>')


class QrTests(unittest.TestCase):
    def test_v2_online_hash(self):
        key = "3" * 44
        url = qrcode.build_v2(key, "2", "000001", "CSC0TESTE0123456789", uf="SP", environment="homologation")
        params = f"{key}|2|2|1"
        expected = hashlib.sha1((params + "CSC0TESTE0123456789").encode()).hexdigest().upper()
        self.assertEqual(url, "https://www.homologacao.nfce.fazenda.sp.gov.br/qrcode?p=" + params + "|" + expected)

    def test_v2_offline_with_digest_hex(self):
        draft = build(emission_type=9, contingency={"at": WHEN, "reason": "Sem internet no caixa ha alguns minutos"})
        url = qrcode.build_v2(draft.key, "1", "2", "CSC0TESTE0123456789", uf="SP", environment="production",
                              offline=True, issued_at=draft.issued_at, total_cents=draft.total_cents,
                              digest_value=draft.digest_value)
        params = url.split("?p=")[1].split("|")
        self.assertEqual(params[:5], [draft.key, "2", "1", "18", "16.98"])
        self.assertEqual(bytes.fromhex(params[5]).decode(), draft.digest_value)
        self.assertEqual(params[6], "2")
        self.assertEqual(params[7], hashlib.sha1(("|".join(params[:7]) + "CSC0TESTE0123456789").encode()).hexdigest().upper())
        with self.assertRaises(ValueError):
            qrcode.build_v2(draft.key, "1", "", "", uf="SP", environment="production")

    def test_v3_online_and_offline_signature(self):
        _pfx, _password, private_key, cert = make_certificate()
        key = "3" * 44
        self.assertEqual(qrcode.build_v3(key, "1", uf="RJ", environment="production"),
                         "https://consultadfe.fazenda.rj.gov.br/consultaNFCe/QRCode?p=" + key + "|3|1")
        url = qrcode.build_v3(key, "1", uf="RJ", environment="production", offline=True,
                              issued_at="2026-11-18T10:00:05-03:00", total_cents=1698, dest_type="2",
                              dest_id="52998224725", signer=lambda data: signature.rsa_sha1(private_key, data))
        params = url.split("?p=")[1].split("|")
        self.assertEqual(params[:7], [key, "3", "1", "18", "16.98", "2", "52998224725"])
        cert.public_key().verify(base64.b64decode(params[7]), "|".join(params[:7]).encode(), padding.PKCS1v15(), hashes.SHA1())
        anonymous = qrcode.v3_offline_payload(key, "1", "2026-11-18T10:00:05-03:00", 1698)
        self.assertTrue(anonymous.endswith("|16.98||"))
        self.assertEqual(qrcode.dest_identity("11444777000161"), ("1", "11444777000161"))


class SignatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _pfx, _password, cls.key, cls.cert = make_certificate()
        cls.der = cls.cert.public_bytes(serialization.Encoding.DER)

    def signed(self):
        draft = build()
        draft.supplement("https://example.test/qr?p=x", "https://example.test/c")
        signature.sign(draft.nfe, draft.infnfe, self.key, self.der)
        return draft

    def test_sign_and_verify(self):
        draft = self.signed()
        node = draft.nfe.find("Signature")
        self.assertEqual(node.ns, signature.DSIG_NS)
        self.assertEqual([c.tag for c in draft.nfe.children], ["infNFe", "infNFeSupl", "Signature"])
        self.assertEqual(node.findtext("SignedInfo/Reference/DigestValue"), draft.digest_value)
        self.assertEqual(node.find("SignedInfo/Reference").attrs["URI"], "#NFe" + draft.key)
        text = tostring(draft.nfe)
        self.assertIn('<Signature xmlns="http://www.w3.org/2000/09/xmldsig#"><SignedInfo><CanonicalizationMethod', text)
        # Round trip through a parser, as the SEFAZ would read it.
        parsed = from_etree(parse(text))
        self.assertEqual(signature.verify(parsed, "infNFe").serial_number, self.cert.serial_number)
        # The SignedInfo is signed in its own canonical form, with the xmldsig namespace.
        info = canonical(parsed.find("Signature/SignedInfo"), signature.DSIG_NS)
        self.assertEqual(ET.canonicalize(info), info)
        self.cert.public_key().verify(base64.b64decode(parsed.findtext("Signature/SignatureValue")), info.encode(),
                                      padding.PKCS1v15(), hashes.SHA1())

    def test_tampering_is_detected(self):
        text = tostring(self.signed().nfe).replace("<vNF>16.98</vNF>", "<vNF>1.98</vNF>")
        with self.assertRaises(signature.SignatureError):
            signature.verify(from_etree(parse(text)), "infNFe")
        with self.assertRaises(signature.SignatureError):
            signature.sign(self.signed().nfe, E("x", Id="a"), self.key, self.der)

    def test_signature_survives_embedding_in_the_batch(self):
        draft = self.signed()
        nfe = from_etree(parse(tostring(draft.nfe)))
        nfe.ns = None
        batch = canonical(messages.authorization_batch(nfe, "1"))
        self.assertIn('<enviNFe xmlns="http://www.portalfiscal.inf.br/nfe" versao="4.00"><idLote>1</idLote><indSinc>1</indSinc><NFe>', batch)
        inner = from_etree(parse(batch)).find("NFe")
        self.assertTrue(signature.verify(inner, "infNFe"))

    def test_events_and_inutilization_are_signed(self):
        key = build().key
        event = messages.cancel_event(key, "35", "2", "11222333000181", "2026-11-18T10:10:00-03:00",
                                      "135260000000001", "Cliente desistiu da compra no caixa")
        self.assertEqual(event.find("infEvento").attrs["Id"], "ID110111" + key + "01")
        signature.sign(event, event.find("infEvento"), self.key, self.der)
        self.assertTrue(signature.verify(from_etree(parse(tostring(event))), "infEvento"))
        inut = messages.inutilization_request("35", "2", 2026, "11222333000181", 1, 10, 12, "Falha no sistema emissor")
        self.assertEqual(inut.find("infInut").attrs["Id"], "ID35261122233300018165001000000010000000012")
        signature.sign(inut, inut.find("infInut"), self.key, self.der)
        self.assertTrue(signature.verify(from_etree(parse(tostring(inut))), "infInut"))
        with self.assertRaises(ValueError):
            messages.cancel_event(key, "35", "2", "11222333000181", "x", "1", "curta")


class ResponseTests(unittest.TestCase):
    def test_authorized_protocol_and_nfe_proc(self):
        draft = build()
        answer = responses.authorization(ret_authorization(draft.key))
        self.assertEqual(answer["batch_status"], "104")
        protocol = answer["protocol"]
        self.assertEqual((protocol["status"], protocol["protocol"], protocol["key"]), ("100", "135260000000001", draft.key))
        proc = responses.nfe_proc(draft.nfe, protocol["element"])
        self.assertTrue(proc.startswith(b'<?xml version="1.0" encoding="UTF-8"?><nfeProc xmlns="http://www.portalfiscal.inf.br/nfe" versao="4.00"><NFe><infNFe'))
        root = ET.fromstring(proc)
        self.assertEqual(root.find(f"{{{NFE_NS}}}protNFe/{{{NFE_NS}}}infProt/{{{NFE_NS}}}nProt").text, "135260000000001")

    def test_rejection_duplicity_and_denial_messages(self):
        key = build().key
        rejected = responses.authorization(ret_authorization(key, "778", "Rejeicao: NCM inexistente"))
        self.assertEqual(rejected["protocol"]["status"], "778")
        self.assertIn("Corrija o NCM", messages.status_message("778", "Rejeicao: NCM inexistente"))
        self.assertIn("204", messages.DUPLICATE)
        self.assertIn("110", messages.DENIED)
        received = responses.authorization(ret_authorization(key, batch="103"))
        self.assertIsNone(received["protocol"])

    def test_consult_status_event_inutilization(self):
        key = build().key
        consult = responses.consultation(ret_consult(key, "101", "Cancelamento homologado", cancel="135260000000099"))
        self.assertEqual(consult["status"], "101")
        self.assertEqual(consult["events"][0]["protocol"], "135260000000099")
        self.assertTrue(responses.service_status(ret_status())["online"])
        self.assertFalse(responses.service_status(ret_status("108", "Paralisado"))["online"])
        event = responses.event(ret_event(key))
        self.assertEqual((event["status"], event["protocol"]), ("135", "135260000000099"))
        inut = responses.inutilization(ret_inutilization())
        self.assertEqual((inut["status"], inut["protocol"]), ("102", "135260000000077"))
        with self.assertRaises(XMLError):
            responses.authorization(b"<soap:Envelope xmlns:soap='x'><soap:Body><soap:Fault>erro</soap:Fault></soap:Body></soap:Envelope>")


class WebserviceTests(unittest.TestCase):
    def test_authorizers_and_urls(self):
        self.assertTrue(webservices.endpoint("BA", "production", "authorize").startswith("https://nfce.svrs.rs.gov.br/"))
        self.assertEqual(webservices.endpoint("SP", "homologation", "authorize"),
                         "https://homologacao.nfce.fazenda.sp.gov.br/ws/NFeAutorizacao4.asmx")
        self.assertEqual(webservices.endpoint("MG", "production", "consult"),
                         "https://nfce.fazenda.mg.gov.br/nfce/services/NFeConsultaProtocolo4")
        self.assertEqual(webservices.endpoint("MT", "production", "event"),
                         "https://nfce.sefaz.mt.gov.br/nfcews/services/RecepcaoEvento4")
        for uf in webservices.AUTHORIZER:
            for env in ("production", "homologation"):
                for service in webservices.SERVICES:
                    self.assertTrue(webservices.endpoint(uf, env, service).startswith("https://"))
                self.assertTrue(webservices.qr_url(uf, env))
                self.assertTrue(webservices.key_url(uf, env))
        with patch.dict(os.environ, {"PDV_NFCE_WS_OVERRIDES": '{"SP": {"homologation": {"status": "https://x.test/s"}}}'}):
            self.assertEqual(webservices.endpoint("SP", "homologation", "status"), "https://x.test/s")
        self.assertEqual(webservices.cancel_minutes("SP"), 30)


class PrintRuleTests(unittest.TestCase):
    def test_defaults_and_split_payment(self):
        self.assertEqual(print_decision([{"method": "cash"}], {}), "ask")
        self.assertEqual(print_decision([{"method": "pix"}], {}), "ask")
        self.assertEqual(print_decision([{"method": "card_debit"}], {}), "always")
        self.assertEqual(print_decision([{"method": "cash"}, {"method": "card_credit"}], {}), "always")
        self.assertEqual(print_decision([{"method": "cash"}], {"cash": "never"}), "never")
        self.assertEqual(print_decision([{"method": "cash"}, {"method": "pix"}], {"cash": "never"}), "ask")
        self.assertEqual(print_decision([{"method": "cash"}, {"method": "pix"}], {"cash": "never", "pix": "never"}), "never")
        self.assertEqual(print_decision([{"method": "other", "payment_method_id": "v1"}], {"v1": "always", "other": "never"}), "always")


if __name__ == "__main__":
    unittest.main()
