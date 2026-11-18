"""QR Code da NFC-e: versão 2 (NT 2015.002, com CSC) e versão 3 (NT 2025.001).

Versão 2 (``?p=``):
- emissão normal: ``chNFe|2|tpAmb|cIdToken|cHashQRCode``
- contingência off-line: ``chNFe|2|tpAmb|dia|vNF|digValHex|cIdToken|cHashQRCode``
  ``cHashQRCode = SHA-1(parâmetros + CSC)`` em hexadecimal maiúsculo; ``digValHex`` é o
  DigestValue (texto Base64 da assinatura) em hexadecimal; ``cIdToken`` sem zeros à esquerda.

Versão 3 (NT 2025.001 v1.03):
- emissão normal: ``chNFe|3|tpAmb`` (sem CSC e sem hash);
- contingência off-line: ``chNFe|3|tpAmb|dia|vNF|tpIdDest|idDest|assinatura`` em que
  ``assinatura = Base64(RSA-SHA1(certificado A1, "chNFe|3|tpAmb|dia|vNF|tpIdDest|idDest"))``;
  ``tpIdDest`` 1 = CNPJ, 2 = CPF, 3 = estrangeiro, vazio sem destinatário (idDest vazio também).

A versão 3 em contingência exige a chave privada do certificado no momento da venda;
por isso a contingência feita no próprio caixa usa a versão 2 (CSC) — ver docs/NFCE.md.
"""
import base64
import hashlib

from .webservices import qr_url


def _amount(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def _day(issued_at_iso):
    # dhEmi "2026-11-18T10:15:00-03:00": the day of the month in the issuer's time zone.
    return str(issued_at_iso)[8:10]


def digest_hex(digest_value_b64):
    return "".join(f"{byte:02x}" for byte in str(digest_value_b64).encode("ascii"))


def build_v2(key, environment_code, csc_id, csc, uf=None, environment=None, offline=False,
             issued_at=None, total_cents=None, digest_value=None, base_url=None):
    if not csc or not str(csc_id or "").strip():
        raise ValueError("QR Code versão 2 exige o CSC e o identificador do CSC")
    token_id = str(int(str(csc_id).strip()))
    if offline:
        if digest_value is None or issued_at is None or total_cents is None:
            raise ValueError("Contingência exige dia, valor e DigestValue no QR Code")
        params = f"{key}|2|{environment_code}|{_day(issued_at)}|{_amount(total_cents)}|{digest_hex(digest_value)}|{token_id}"
    else:
        params = f"{key}|2|{environment_code}|{token_id}"
    digest = hashlib.sha1((params + str(csc)).encode("utf-8")).hexdigest().upper()
    url = base_url or qr_url(uf, environment)
    return f"{url}?p={params}|{digest}"


def v3_offline_payload(key, environment_code, issued_at, total_cents, dest_type="", dest_id=""):
    dest_type = str(dest_type or "")
    if dest_type not in ("1", "2", "3"):
        dest_type, dest_id = "", ""
    return f"{key}|3|{environment_code}|{_day(issued_at)}|{_amount(total_cents)}|{dest_type}|{dest_id}"


def build_v3(key, environment_code, uf=None, environment=None, offline=False, issued_at=None,
             total_cents=None, dest_type="", dest_id="", signer=None, base_url=None):
    url = base_url or qr_url(uf, environment)
    if not offline:
        return f"{url}?p={key}|3|{environment_code}"
    if signer is None:
        raise ValueError("QR Code versão 3 em contingência exige assinar com o certificado")
    payload = v3_offline_payload(key, environment_code, issued_at, total_cents, dest_type, dest_id)
    signature = base64.b64encode(signer(payload.encode("utf-8"))).decode("ascii")
    return f"{url}?p={payload}|{signature}"


def dest_identity(document):
    """(tpIdDest, idDest) for the v3 QR: CNPJ 14 -> 1, CPF 11 -> 2, none -> ("", "")."""
    value = str(document or "").upper()
    if len(value) == 14:
        return "1", value
    if len(value) == 11:
        return "2", value
    return "", ""
