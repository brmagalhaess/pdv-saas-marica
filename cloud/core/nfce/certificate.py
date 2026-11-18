"""Certificado A1 (PKCS#12 .pfx) da empresa: leitura, titular, CNPJ e validade.

O CNPJ do e-CNPJ ICP-Brasil está no SubjectAltName (otherName OID 2.16.76.1.3.3); certificados
antigos também trazem "RAZAO SOCIAL:CNPJ" no CN, usado como alternativa.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12

CNPJ_OID = "2.16.76.1.3.3"
MAX_PFX_BYTES = 64 * 1024


class CertificateError(ValueError):
    pass


@dataclass
class Certificate:
    private_key: object
    certificate: object
    chain: list

    @property
    def der(self):
        return self.certificate.public_bytes(serialization.Encoding.DER)

    @property
    def subject(self):
        names = self.certificate.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
        return names[0].value if names else self.certificate.subject.rfc4514_string()

    @property
    def not_before(self):
        return self.certificate.not_valid_before_utc

    @property
    def not_after(self):
        return self.certificate.not_valid_after_utc

    @property
    def cnpj(self):
        return certificate_cnpj(self.certificate)

    def pem_bundle(self):
        """Private key + certificate chain in PEM, for the TLS client handshake."""
        key = self.private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption())
        certs = b"".join(cert.public_bytes(serialization.Encoding.PEM) for cert in [self.certificate, *self.chain])
        return key, certs

    def sign(self, data):
        from shared.nfce.signature import rsa_sha1
        return rsa_sha1(self.private_key, data)

    def info(self):
        return {"subject": self.subject, "cnpj": self.cnpj, "not_before": self.not_before.isoformat(),
                "not_after": self.not_after.isoformat()}


def _der_string(raw):
    """Value of an otherName: a DER OCTET STRING / PrintableString / UTF8String (or raw text)."""
    if len(raw) >= 2 and raw[0] in (0x04, 0x0C, 0x13, 0x16) and raw[1] < 0x80:
        return raw[2:2 + raw[1]].decode("latin-1", "ignore")
    return raw.decode("latin-1", "ignore")


def certificate_cnpj(cert):
    try:
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        for other in names:
            if isinstance(other, x509.OtherName) and other.type_id.dotted_string == CNPJ_OID:
                value = re.sub(r"[^0-9A-Z]", "", _der_string(other.value).upper())
                if len(value) >= 14:
                    return value[:14]
    except x509.ExtensionNotFound:
        pass
    common = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    match = re.search(r":([0-9A-Z]{14})\b", common[0].value.upper()) if common else None
    return match.group(1) if match else ""


def load_pfx(data, password):
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise CertificateError("Envie o arquivo do certificado A1 (.pfx ou .p12)")
    if len(data) > MAX_PFX_BYTES:
        raise CertificateError("Arquivo do certificado grande demais")
    try:
        key, cert, chain = pkcs12.load_key_and_certificates(bytes(data), (password or "").encode("utf-8"))
    except ValueError as exc:
        raise CertificateError("Não foi possível abrir o certificado: confira o arquivo e a senha") from exc
    if key is None or cert is None:
        raise CertificateError("O arquivo não contém a chave privada e o certificado")
    if not isinstance(key, rsa.RSAPrivateKey):
        raise CertificateError("O certificado A1 precisa ter chave RSA")
    return Certificate(key, cert, list(chain or []))


def validity(cert, now=None):
    """(state, days_left): valid | expiring (<= 30 days) | expired | not_yet."""
    now = now or datetime.now(timezone.utc)
    if now < cert.not_before:
        return "not_yet", None
    days = (cert.not_after - now).days
    if days < 0:
        return "expired", days
    return ("expiring" if days <= 30 else "valid"), days
