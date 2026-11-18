"""Apoio aos testes (sem rede, sem dado real): certificado A1 autoassinado e respostas da SEFAZ simuladas.

Gera na hora um e-CNPJ de mentira (RSA 2048, CNPJ no SubjectAltName OID 2.16.76.1.3.3 e no CN)
e monta envelopes SOAP com os retornos que a SEFAZ devolveria. Nada aqui é usado em produção.
"""
from datetime import datetime, timedelta, timezone

from .xml import E, NFE_NS, canonical, from_etree, parse

TEST_CNPJ = "11222333000181"
_KEYS = {}


def make_certificate(cnpj=TEST_CNPJ, days=365, password="senha-teste", not_before=None, name="EMPRESA TESTE LTDA"):
    """(pfx_bytes, password, private_key, certificate) of a throwaway self-signed e-CNPJ."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID

    key = _KEYS.get("rsa")
    if key is None:
        key = _KEYS["rsa"] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    start = not_before or datetime.now(timezone.utc) - timedelta(days=1)
    subject = x509.Name([x509.NameAttribute(NameOID.COUNTRY_NAME, "BR"),
                         x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ICP-Brasil (TESTE)"),
                         x509.NameAttribute(NameOID.COMMON_NAME, f"{name}:{cnpj}")])
    other = x509.OtherName(x509.ObjectIdentifier("2.16.76.1.3.3"), b"\x04\x0e" + cnpj.encode("ascii"))
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(start)
            .not_valid_after(start + timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([other]), critical=False)
            .sign(key, hashes.SHA256()))
    pfx = pkcs12.serialize_key_and_certificates(b"teste", key, cert, None,
                                                serialization.BestAvailableEncryption(password.encode()))
    return pfx, password, key, cert


def soap(inner):
    return ('<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"><soap:Body>'
            '<nfeResultMsg xmlns="http://www.portalfiscal.inf.br/nfe/wsdl/NFeAutorizacao4">'
            + inner + '</nfeResultMsg></soap:Body></soap:Envelope>').encode("utf-8")


def now_text():
    """Current time as the SEFAZ writes it (Brasília, -03:00)."""
    return datetime.now(timezone(timedelta(hours=-3))).replace(microsecond=0).isoformat()


def _prot(key, status="100", reason="Autorizado o uso da NF-e", protocol="135260000000001", digest=""):
    return (f'<protNFe versao="4.00"><infProt><tpAmb>2</tpAmb><verAplic>TESTE</verAplic><chNFe>{key}</chNFe>'
            f'<dhRecbto>{now_text()}</dhRecbto>' + (f'<nProt>{protocol}</nProt>' if status in ("100", "150") else "")
            + f'<digVal>{digest}</digVal><cStat>{status}</cStat><xMotivo>{reason}</xMotivo></infProt></protNFe>')


def ret_authorization(key, status="100", reason="Autorizado o uso da NF-e", protocol="135260000000001", batch="104"):
    prot = _prot(key, status, reason, protocol) if batch == "104" else ""
    return soap(f'<retEnviNFe xmlns="{NFE_NS}" versao="4.00"><tpAmb>2</tpAmb><verAplic>TESTE</verAplic>'
                f'<cStat>{batch}</cStat><xMotivo>Lote processado</xMotivo><cUF>35</cUF>'
                f'<dhRecbto>2026-11-18T10:00:05-03:00</dhRecbto>{prot}</retEnviNFe>')


def ret_consult(key, status="100", reason="Autorizado o uso da NF-e", protocol="135260000000001", cancel=None):
    prot = _prot(key, "100", "Autorizado o uso da NF-e", protocol) if status in ("100", "101", "135") else ""
    events = ""
    if cancel:
        events = (f'<procEventoNFe versao="1.00"><evento versao="1.00"><infEvento Id="ID110111{key}01">'
                  f'<chNFe>{key}</chNFe><tpEvento>110111</tpEvento></infEvento></evento><retEvento versao="1.00">'
                  f'<infEvento><cStat>135</cStat><xMotivo>Evento registrado</xMotivo><chNFe>{key}</chNFe>'
                  f'<nProt>{cancel}</nProt></infEvento></retEvento></procEventoNFe>')
    return soap(f'<retConsSitNFe xmlns="{NFE_NS}" versao="4.00"><tpAmb>2</tpAmb><cStat>{status}</cStat>'
                f'<xMotivo>{reason}</xMotivo><cUF>35</cUF><chNFe>{key}</chNFe>{prot}{events}</retConsSitNFe>')


def ret_status(status="107", reason="Servico em Operacao"):
    return soap(f'<retConsStatServ xmlns="{NFE_NS}" versao="4.00"><tpAmb>2</tpAmb><verAplic>TESTE</verAplic>'
                f'<cStat>{status}</cStat><xMotivo>{reason}</xMotivo><cUF>35</cUF>'
                f'<dhRecbto>2026-11-18T10:00:00-03:00</dhRecbto><tMed>1</tMed></retConsStatServ>')


def ret_event(key, status="135", reason="Evento registrado e vinculado a NF-e", protocol="135260000000099"):
    return soap(f'<retEnvEvento xmlns="{NFE_NS}" versao="1.00"><idLote>1</idLote><tpAmb>2</tpAmb>'
                f'<verAplic>TESTE</verAplic><cOrgao>35</cOrgao><cStat>128</cStat><xMotivo>Lote de evento processado</xMotivo>'
                f'<retEvento versao="1.00"><infEvento><tpAmb>2</tpAmb><verAplic>TESTE</verAplic><cOrgao>35</cOrgao>'
                f'<cStat>{status}</cStat><xMotivo>{reason}</xMotivo><chNFe>{key}</chNFe><tpEvento>110111</tpEvento>'
                f'<nSeqEvento>1</nSeqEvento><dhRegEvento>2026-11-18T10:10:00-03:00</dhRegEvento>'
                + (f'<nProt>{protocol}</nProt>' if status in ("135", "136", "155") else "")
                + '</infEvento></retEvento></retEnvEvento>')


def ret_inutilization(status="102", reason="Inutilizacao de numero homologado", protocol="135260000000077"):
    return soap(f'<retInutNFe xmlns="{NFE_NS}" versao="4.00"><infInut><tpAmb>2</tpAmb><verAplic>TESTE</verAplic>'
                f'<cStat>{status}</cStat><xMotivo>{reason}</xMotivo><cUF>35</cUF>'
                f'<dhRecbto>2026-11-18T10:00:00-03:00</dhRecbto>'
                + (f'<nProt>{protocol}</nProt>' if status == "102" else "") + '</infInut></retInutNFe>')


class FakeSefaz:
    """Transport double: records calls and answers by service. ``answers[service]`` may be bytes,
    an exception instance, or a callable(body_element) -> bytes."""

    def __init__(self, **answers):
        self.answers = answers
        self.calls = []

    def call(self, url, service, body):
        self.calls.append({"url": url, "service": service, "body": canonical(body)})
        answer = self.answers.get(service)
        if isinstance(answer, list):
            answer = answer.pop(0) if answer else None
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return answer(body)
        if answer is None:
            raise AssertionError("Chamada inesperada: " + service)
        return answer

    def sent(self, service):
        return [from_etree(parse(call["body"])) for call in self.calls if call["service"] == service]


def authorize_ok(protocol="135260000000001"):
    """Answer for the authorize service that authorizes whatever key was sent."""
    def answer(body):
        key = body.find("NFe/infNFe").attrs["Id"][3:]
        return ret_authorization(key, protocol=protocol)
    return answer


__all__ = ["make_certificate", "FakeSefaz", "ret_authorization", "ret_consult", "ret_status", "ret_event",
           "ret_inutilization", "authorize_ok", "TEST_CNPJ", "E"]
