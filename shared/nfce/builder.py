"""Montagem do XML da NFC-e (modelo 65, leiaute 4.00, PL_009) a partir do motor tributário.

Entrada: o documento calculado por ``shared.fiscal.compute_document`` (schema pdv2.fiscal.v1),
os dados do emitente, numeração, pagamentos e destinatário opcional. Saída: ``NfceDraft`` com a
árvore ``NFe`` (sem assinatura), a chave de acesso e o DigestValue que a assinatura terá.

Ordem das tags conforme o leiaute 4.00 (MOC 7.0, anexo I). O mesmo código roda na nuvem e no
caixa (contingência off-line), por isso é Python puro.
"""
import base64
import hashlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from shared.fiscal.data import UF_CODES
from shared.fiscal.validators import cnpj_value, digits, valid_cnpj, valid_cpf

from . import keys
from .payments import CARD_TPAG, tpag
from .webservices import time_zone
from .xml import E, NFE_NS, DSIG_NS, canonical, clean_text, leaf

LAYOUT_VERSION = "4.00"
HOMOLOGATION_TEXT = "NOTA FISCAL EMITIDA EM AMBIENTE DE HOMOLOGACAO - SEM VALOR FISCAL"
ENVIRONMENT_CODE = {"production": "1", "homologation": "2"}
CONTINGENCY_REASON = "Falha de comunicacao com a SEFAZ no momento da venda (sem internet)"


class NfceError(ValueError):
    """Dados insuficientes ou inconsistentes para montar a nota; ``issues`` em português."""

    def __init__(self, message, issues=None):
        super().__init__(message)
        self.issues = issues or []


def money(cents):
    cents = int(cents)
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def local_iso(moment, uf):
    """dhEmi/dhCont: AAAA-MM-DDThh:mm:ssTZD in the issuer's UF time zone."""
    if moment.tzinfo is None:
        raise ValueError("Data/hora sem fuso")
    value = moment.astimezone(ZoneInfo(time_zone(uf))).replace(microsecond=0)
    text = value.isoformat()
    return text


def digest_value(infnfe):
    """Base64 SHA-1 of the canonical infNFe (the DigestValue of the signature Reference)."""
    data = canonical(infnfe, NFE_NS).encode("utf-8")
    return base64.b64encode(hashlib.sha1(data).digest()).decode("ascii")


class NfceDraft:
    def __init__(self, nfe, key, environment, emission_type, issued_at_text, total_cents, dest_document):
        self.nfe = nfe
        self.key = key
        self.environment = environment
        self.emission_type = emission_type
        self.issued_at = issued_at_text
        self.total_cents = total_cents
        self.dest_document = dest_document

    @property
    def infnfe(self):
        return self.nfe.find("infNFe")

    @property
    def digest_value(self):
        return digest_value(self.infnfe)

    @property
    def environment_code(self):
        return ENVIRONMENT_CODE[self.environment]

    def supplement(self, qr_code, url_key):
        """infNFeSupl comes after infNFe and before the Signature; it is not signed."""
        if self.nfe.find("infNFeSupl") is not None:
            raise NfceError("QR Code já incluído")
        self.nfe.add(E("infNFeSupl", E("qrCode", text=qr_code), E("urlChave", text=url_key)))
        return self


def _rate(value):
    return value if value not in (None, "") else "0.00"


def _icms_group(item):
    icms = item["icms"]
    group = icms["group"]
    if not group:
        raise NfceError(f"Item {item['number']}: falta o CST/CSOSN do ICMS")
    code = E("CSOSN", text=icms["csosn"]) if icms["csosn"] else E("CST", text=icms["cst"])
    node = E(group, E("orig", text=icms["origin"]), code)
    cst = icms["cst"] or ""
    csosn = icms["csosn"] or ""
    st = icms.get("st")
    retained = icms.get("st_retained")

    def own(reduction_first):
        parts = [E("modBC", text=icms["bc_mode"])]
        reduction = leaf("pRedBC", icms.get("bc_reduction"))
        if reduction_first:
            parts.append(reduction)
        parts += [E("vBC", text=money(icms["bc_cents"]))]
        if not reduction_first:
            parts.append(reduction)
        parts += [E("pICMS", text=_rate(icms["rate"])), E("vICMS", text=money(icms["value_cents"]))]
        return parts

    def fcp(with_base):
        if not icms.get("fcp_rate"):
            return []
        parts = [E("vBCFCP", text=money(icms["bc_cents"]))] if with_base else []
        return parts + [E("pFCP", text=icms["fcp_rate"]), E("vFCP", text=money(icms["fcp_cents"]))]

    def st_parts():
        if not st:
            return []
        parts = [E("modBCST", text=st["bc_mode"]), leaf("pMVAST", st.get("mva")), leaf("pRedBCST", st.get("bc_reduction")),
                 E("vBCST", text=money(st["bc_cents"])), E("pICMSST", text=_rate(st["rate"])),
                 E("vICMSST", text=money(st["value_cents"]))]
        if st.get("fcp_rate"):
            parts += [E("vBCFCPST", text=money(st["fcp_bc_cents"])), E("pFCPST", text=st["fcp_rate"]),
                      E("vFCPST", text=money(st["fcp_cents"]))]
        return parts

    def retained_parts():
        if not retained:
            return []
        parts = [E("vBCSTRet", text=money(retained["bc_cents"])), E("pST", text=_rate(retained["rate"])),
                 E("vICMSSubstituto", text=money(retained["substitute_cents"])),
                 E("vICMSSTRet", text=money(retained["value_cents"]))]
        if retained.get("fcp_rate"):
            parts += [E("vBCFCPSTRet", text=money(retained["fcp_bc_cents"])), E("pFCPSTRet", text=retained["fcp_rate"]),
                      E("vFCPSTRet", text=money(retained["fcp_cents"]))]
        return parts

    def credit():
        return [E("pCredSN", text=_rate(icms["sn_credit_rate"])), E("vCredICMSSN", text=money(icms["sn_credit_cents"]))]

    if cst == "00":
        node.add(own(False) + fcp(False))
    elif cst == "10":
        node.add(own(False) + fcp(True) + st_parts())
    elif cst == "20":
        node.add(own(True) + fcp(True))
    elif cst == "30":
        node.add(st_parts())
    elif cst == "51":
        if icms.get("operation_cents") is not None:
            node.add([E("modBC", text=icms["bc_mode"]), leaf("pRedBC", icms.get("bc_reduction")),
                      E("vBC", text=money(icms["bc_cents"])), E("pICMS", text=_rate(icms["rate"])),
                      E("vICMSOp", text=money(icms["operation_cents"])), E("pDif", text=icms["deferral_rate"]),
                      E("vICMSDif", text=money(icms["deferred_cents"])), E("vICMS", text=money(icms["value_cents"]))])
    elif cst == "60":
        node.add(retained_parts())
    elif cst == "70":
        node.add(own(True) + fcp(True) + st_parts())
    elif cst == "90":
        if icms.get("rate") is not None and icms["bc_mode"] is not None:
            node.add([E("modBC", text=icms["bc_mode"]), E("vBC", text=money(icms["bc_cents"])),
                      leaf("pRedBC", icms.get("bc_reduction")), E("pICMS", text=_rate(icms["rate"])),
                      E("vICMS", text=money(icms["value_cents"]))] + fcp(True))
        node.add(st_parts())
    elif csosn == "101":
        node.add(credit())
    elif csosn in ("201", "202", "203"):
        node.add(st_parts())
        if csosn == "201":
            node.add(credit())
    elif csosn == "500":
        node.add(retained_parts())
    elif csosn == "900":
        if icms.get("rate") is not None and icms["bc_mode"] is not None:
            node.add([E("modBC", text=icms["bc_mode"]), E("vBC", text=money(icms["bc_cents"])),
                      leaf("pRedBC", icms.get("bc_reduction")), E("pICMS", text=_rate(icms["rate"])),
                      E("vICMS", text=money(icms["value_cents"]))])
        node.add(st_parts())
        if icms.get("sn_credit_rate"):
            node.add(credit())
    return E("ICMS", node)


def _contribution(tax, data):
    if not data:
        return None
    upper = tax.upper()
    group = data["group"]
    if group == "NT":
        body = E(upper + "NT", E("CST", text=data["cst"]))
    elif group == "Qtde":
        raise NfceError(f"{upper} por quantidade (CST 03) não é suportado")
    else:
        body = E(upper + group, E("CST", text=data["cst"]), E("vBC", text=money(data["bc_cents"])),
                 E("p" + upper, text=_rate(data["rate"])), E("v" + upper, text=money(data["value_cents"])))
    return E(upper, body)


def _prod(item, homologation_text):
    description = homologation_text or clean_text(item["description"], 120)
    if not description:
        raise NfceError(f"Item {item['number']}: descrição vazia")
    if not item["ncm"]:
        raise NfceError(f"Item {item['number']}: falta o NCM")
    unit = clean_text(item["unit"], 6) or "UN"
    tax_unit = clean_text(item["tax_unit"], 6) or unit
    return E("prod",
             E("cProd", text=clean_text(item["code"] or item["product_id"], 60)),
             E("cEAN", text=item["gtin"]),
             E("xProd", text=description),
             E("NCM", text=item["ncm"]),
             leaf("CEST", item.get("cest")),
             leaf("cBenef", item.get("cbenef")),
             leaf("EXTIPI", item.get("ex_tipi")),
             E("CFOP", text=item["cfop"]),
             E("uCom", text=unit),
             E("qCom", text=item["quantity"]),
             E("vUnCom", text=item["unit_price"]),
             E("vProd", text=money(item["gross_cents"])),
             E("cEANTrib", text=item.get("tax_gtin") or item["gtin"]),
             E("uTrib", text=tax_unit),
             E("qTrib", text=item["tax_quantity"]),
             E("vUnTrib", text=item["tax_unit_price"]),
             leaf("vFrete", money(item["freight_cents"]) if item["freight_cents"] else None),
             leaf("vDesc", money(item["discount_cents"]) if item["discount_cents"] else None),
             leaf("vOutro", money(item["other_cents"]) if item["other_cents"] else None),
             E("indTot", text="1"))


def _det(item, homologation_text):
    approx = item["approx_tax"]["total_cents"]
    imposto = E("imposto", leaf("vTotTrib", money(approx) if approx else None), _icms_group(item),
                _contribution("pis", item.get("pis")), _contribution("cofins", item.get("cofins")))
    return E("det", _prod(item, homologation_text), imposto, nItem=item["number"])


def _total(totals):
    t = totals
    return E("total", E("ICMSTot",
        E("vBC", text=money(t["icms_bc_cents"])), E("vICMS", text=money(t["icms_cents"])),
        E("vICMSDeson", text="0.00"), E("vFCP", text=money(t["fcp_cents"])),
        E("vBCST", text=money(t["st_bc_cents"])), E("vST", text=money(t["st_cents"])),
        E("vFCPST", text=money(t["fcp_st_cents"])), E("vFCPSTRet", text=money(t["fcp_st_retained_cents"])),
        E("vProd", text=money(t["products_cents"])), E("vFrete", text=money(t["freight_cents"])),
        E("vSeg", text="0.00"), E("vDesc", text=money(t["discount_cents"])),
        E("vII", text="0.00"), E("vIPI", text=money(t["ipi_cents"])), E("vIPIDevol", text="0.00"),
        E("vPIS", text=money(t["pis_cents"])), E("vCOFINS", text=money(t["cofins_cents"])),
        E("vOutro", text=money(t["other_cents"])), E("vNF", text=money(t["total_cents"])),
        leaf("vTotTrib", money(t["approx_tax_cents"]) if t["approx_tax_cents"] else None)))


def _payments(payments, change_cents, total_cents, names=None):
    if not payments:
        raise NfceError("Venda sem pagamento")
    paid = sum(int(p["amount_cents"]) for p in payments)
    if paid - int(change_cents or 0) != total_cents:
        raise NfceError("Pagamentos menos troco não fecham o total da nota")
    pag = E("pag")
    for payment in payments:
        code = tpag(payment["method"])
        name = None
        if code == "99":
            name = clean_text((names or {}).get(payment.get("payment_method_id")) or "Outros", 60)
        detail = E("detPag", E("tPag", text=code), leaf("xPag", name), E("vPag", text=money(payment["amount_cents"])))
        if code in CARD_TPAG:
            detail.add(E("card", E("tpIntegra", text="2")))  # 2 = POS sem integração com o PDV
        pag.add(detail)
    if change_cents:
        pag.add(E("vTroco", text=money(change_cents)))
    return pag


def approx_text(totals, items):
    """Lei 12.741/2012: valor aproximado dos tributos, por esfera quando a fonte é o IBPT."""
    total = totals["approx_tax_cents"]
    if not total:
        return ""
    federal = sum(item["approx_tax"].get("federal_cents") or 0 for item in items)
    state = sum(item["approx_tax"].get("state_cents") or 0 for item in items)
    city = sum(item["approx_tax"].get("municipal_cents") or 0 for item in items)
    sources = {item["approx_tax"]["source"] for item in items}
    if federal or state or city:
        versions = sorted({item["approx_tax"].get("version") for item in items if item["approx_tax"].get("version")})
        source = "IBPT" + (" " + versions[0] if versions else "")
        return (f"Trib aprox R$: {money(federal).replace('.', ',')} Federal, {money(state).replace('.', ',')} Estadual "
                f"e {money(city).replace('.', ',')} Municipal. Fonte: {source}. Lei 12.741/2012")
    label = "estimativa da empresa" if "fallback" in sources else "IBPT"
    return f"Trib aprox R$ {money(total).replace('.', ',')} ({label}). Lei 12.741/2012"


def build_nfce(issuer, doc, *, series, number, issued_at, environment, cnf=None, emission_type=1,
               contingency=None, customer=None, payments, change_cents=0, payment_names=None,
               tech=None, presence=1, app_version="PDV2", extra_info=""):
    """Monta a NFC-e sem assinatura. ``issuer`` usa as chaves do Tenant (document, name...)."""
    if not isinstance(doc, dict) or doc.get("schema") != "pdv2.fiscal.v1" or doc.get("model") != 65:
        raise NfceError("Documento do motor fiscal inválido para NFC-e")
    if not doc.get("ready"):
        raise NfceError("Há produtos sem dados fiscais completos para a nota",
                        [f"Item {item['number']} ({item['description']}): {issue['message']}"
                         for item in doc["items"] for issue in item.get("issues", []) if issue.get("severity") == "error"])
    if environment not in ENVIRONMENT_CODE:
        raise NfceError("Ambiente fiscal deve ser homologação ou produção")
    uf = issuer.get("address_state") or ""
    if uf not in UF_CODES:
        raise NfceError("UF do emitente inválida")
    document = cnpj_value(issuer.get("document"))
    if not valid_cnpj(document):
        raise NfceError("A NFC-e exige o CNPJ do emitente válido")
    for field, label in (("name", "razão social"), ("address_line", "logradouro"), ("address_number", "número"),
                         ("address_neighborhood", "bairro"), ("address_city", "cidade"),
                         ("city_ibge_code", "código IBGE do município"), ("address_zip", "CEP"),
                         ("state_registration", "inscrição estadual")):
        if not str(issuer.get(field) or "").strip():
            raise NfceError(f"Falta {label} da empresa")
    if not 0 <= int(series) <= 999 or not 1 <= int(number) <= 999999999:
        raise NfceError("Série ou número da NFC-e fora da faixa")
    if emission_type not in (1, 9):
        raise NfceError("Tipo de emissão inválido")
    if emission_type == 9 and not contingency:
        raise NfceError("Contingência exige data/hora e justificativa")
    cnf = cnf or keys.random_cnf(int(number))
    issued_text = local_iso(issued_at, uf)
    year_month = issued_text[2:4] + issued_text[5:7]
    key = keys.access_key(uf, year_month, document, 65, series, number, emission_type, cnf)
    homologation = environment == "homologation"

    ide = E("ide",
            E("cUF", text=UF_CODES[uf]), E("cNF", text=cnf), E("natOp", text="VENDA"),
            E("mod", text="65"), E("serie", text=int(series)), E("nNF", text=int(number)),
            E("dhEmi", text=issued_text), E("tpNF", text="1"), E("idDest", text="1"),
            E("cMunFG", text=issuer["city_ibge_code"]), E("tpImp", text="4"), E("tpEmis", text=emission_type),
            E("cDV", text=key[-1]), E("tpAmb", text=ENVIRONMENT_CODE[environment]), E("finNFe", text="1"),
            E("indFinal", text="1"), E("indPres", text=presence),
            leaf("indIntermed", "0" if int(presence) in (2, 3, 4, 9) else None),
            E("procEmi", text="0"), E("verProc", text=clean_text(app_version, 20)))
    if emission_type == 9:
        ide.add([E("dhCont", text=local_iso(contingency["at"], uf)),
                 E("xJust", text=clean_text(contingency.get("reason") or CONTINGENCY_REASON, 256))])

    address = E("enderEmit",
                E("xLgr", text=clean_text(issuer["address_line"], 60)),
                E("nro", text=clean_text(issuer["address_number"], 60)),
                leaf("xCpl", clean_text(issuer.get("address_complement"), 60)),
                E("xBairro", text=clean_text(issuer["address_neighborhood"], 60)),
                E("cMun", text=issuer["city_ibge_code"]),
                E("xMun", text=clean_text(issuer["address_city"], 60)),
                E("UF", text=uf), E("CEP", text=digits(issuer["address_zip"])),
                E("cPais", text="1058"), E("xPais", text="BRASIL"),
                leaf("fone", digits(issuer.get("phone")) or None))
    ie = cnpj_value(issuer["state_registration"])
    emit = E("emit", E("CNPJ", text=document), E("xNome", text=clean_text(issuer["name"], 60)),
             leaf("xFant", clean_text(issuer.get("trade_name"), 60)), address,
             E("IE", text="ISENTO" if ie == "ISENTO" else ie),
             E("CRT", text=int(issuer.get("crt") or doc["issuer"]["crt"])))

    dest = None
    dest_document = ""
    if customer and customer.get("document"):
        dest_document = cnpj_value(customer["document"])
        if len(dest_document) == 11 and valid_cpf(dest_document):
            dest = E("dest", E("CPF", text=dest_document))
        elif len(dest_document) == 14 and valid_cnpj(dest_document):
            dest = E("dest", E("CNPJ", text=dest_document))
        else:
            raise NfceError("CPF/CNPJ do consumidor inválido")
        name = HOMOLOGATION_TEXT if homologation else clean_text(customer.get("name"), 60)
        dest.add([leaf("xNome", name if customer.get("name") or homologation else None), E("indIEDest", text="9")])

    infnfe = E("infNFe", ide, emit, dest, Id="NFe" + key, versao=LAYOUT_VERSION)
    for index, item in enumerate(doc["items"]):
        infnfe.add(_det(item, HOMOLOGATION_TEXT if homologation and index == 0 else None))
    infnfe.add(_total(doc["totals"]))
    infnfe.add(E("transp", E("modFrete", text="9")))
    infnfe.add(_payments(payments, change_cents, doc["totals"]["total_cents"], payment_names))
    info = " ".join(part for part in (approx_text(doc["totals"], doc["items"]), clean_text(extra_info)) if part)
    if info:
        infnfe.add(E("infAdic", E("infCpl", text=clean_text(info, 5000))))
    tech = tech or {}
    # infRespTec needs CNPJ, contact, e-mail and phone; an incomplete platform setup leaves it out.
    if (valid_cnpj(cnpj_value(tech.get("cnpj"))) and clean_text(tech.get("contact")) and clean_text(tech.get("email"))
            and 6 <= len(digits(tech.get("phone"))) <= 14):
        infnfe.add(E("infRespTec", E("CNPJ", text=cnpj_value(tech["cnpj"])),
                     E("xContato", text=clean_text(tech["contact"], 60)),
                     E("email", text=clean_text(tech["email"], 60)),
                     E("fone", text=digits(tech["phone"]))))
    nfe = E("NFe", infnfe, ns=NFE_NS)
    return NfceDraft(nfe, key, environment, emission_type, issued_text, doc["totals"]["total_cents"], dest_document)


def now_utc():
    return datetime.now(timezone.utc)


__all__ = ["NfceError", "NfceDraft", "build_nfce", "digest_value", "local_iso", "money", "approx_text",
           "HOMOLOGATION_TEXT", "ENVIRONMENT_CODE", "DSIG_NS"]
