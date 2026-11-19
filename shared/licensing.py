"""Signed, device-bound offline leases. Private keys never leave the cloud."""
import base64
import hashlib
import json
import os
import time
from pathlib import Path
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

MAX_LEASE_SECONDS = 45 * 24 * 60 * 60
CLOCK_TOLERANCE_SECONDS = 300

class LicenseError(ValueError):
    pass

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")

def payload_digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()

def generate_keypair():
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()).decode()
    public_pem = private.public_key().public_bytes(serialization.Encoding.PEM,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private_pem, public_pem

def _private(value):
    if isinstance(value, str):
        value = value.encode()
    key = serialization.load_pem_private_key(value, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise LicenseError("Chave privada de licença inválida")
    return key

def _public(value):
    if isinstance(value, str):
        value = value.encode()
    key = serialization.load_pem_public_key(value)
    if not isinstance(key, Ed25519PublicKey):
        raise LicenseError("Chave pública de licença inválida")
    return key

def key_id(public_pem):
    raw = _public(public_pem).public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:24]

def _validate_claims(claims):
    if not isinstance(claims, dict) or claims.get("schema") != 1:
        raise LicenseError("Formato da licença inválido")
    for field in ("tenant_id", "shop_id", "terminal_id", "device_id"):
        if not isinstance(claims.get(field), str) or not 1 <= len(claims[field]) <= 160:
            raise LicenseError("Identidade da licença inválida")
    issued, expires = claims.get("issued_at"), claims.get("expires_at")
    if type(issued) is not int or type(expires) is not int or issued <= 0 or not 0 < expires-issued <= MAX_LEASE_SECONDS:
        raise LicenseError("Prazo de licença inválido")
    if claims.get("status") not in ("trial", "active", "past_due"):
        raise LicenseError("Assinatura não permite nova operação")
    features = claims.get("features")
    if not isinstance(features, list) or len(features) > 100 or any(not isinstance(x, str) or len(x) > 80 for x in features):
        raise LicenseError("Recursos da licença inválidos")
    max_discount = claims.get("max_discount_bps", 10000)
    if type(max_discount) is not int or not 0 <= max_discount <= 10000:
        raise LicenseError("Limite de desconto inválido")
    if len(canonical(claims)) > 256000:
        raise LicenseError("Licença excede o tamanho permitido")

def sign_lease(claims, private_pem):
    _validate_claims(claims)
    key = _private(private_pem)
    public_pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    signature = base64.urlsafe_b64encode(key.sign(canonical(claims))).decode().rstrip("=")
    return {"payload": claims, "signature": signature, "key_id": key_id(public_pem)}

def verify_lease(envelope, public_pem, expected=None, now=None, last_seen=None):
    try:
        if not isinstance(envelope, dict) or set(envelope) != {"payload", "signature", "key_id"}:
            raise LicenseError("Licença incompleta")
        claims = envelope["payload"]
        _validate_claims(claims)
        signature_text = envelope["signature"]
        if not isinstance(signature_text, str) or len(signature_text) > 128:
            raise LicenseError("Assinatura inválida")
        signature = base64.b64decode(signature_text + "=" * (-len(signature_text) % 4), altchars=b"-_", validate=True)
        if envelope["key_id"] != key_id(public_pem):
            raise LicenseError("Chave de licença não reconhecida")
        _public(public_pem).verify(signature, canonical(claims))
        current = int(time.time()) if now is None else int(now)
        if claims["issued_at"] > current + CLOCK_TOLERANCE_SECONDS:
            raise LicenseError("Relógio anterior à emissão da licença")
        if last_seen is not None and current + CLOCK_TOLERANCE_SECONDS < int(last_seen):
            raise LicenseError("Relógio retrocedeu; conecte para validar o terminal")
        if current >= claims["expires_at"]:
            raise LicenseError("Prazo offline expirado; conecte para renovar a licença")
        for field, value in (expected or {}).items():
            if claims.get(field) != str(value):
                raise LicenseError("Licença pertence a outro terminal ou empresa")
        return dict(claims)
    except LicenseError:
        raise
    except (InvalidSignature, ValueError, TypeError, KeyError, OverflowError) as exc:
        raise LicenseError("Assinatura da licença inválida") from exc

def _load(name):
    value = os.environ.get(name, "")
    filename = os.environ.get(name + "_FILE", "")
    if filename:
        value = Path(filename).read_text(encoding="utf-8").strip()
    if not value:
        raise LicenseError(f"Configure {name} ou {name}_FILE")
    return value.replace("\\n", "\n")

def load_private_key():
    value = _load("PDV_LICENSE_PRIVATE_KEY")
    _private(value)
    return value

def load_public_key():
    try:
        value = _load("PDV_LICENSE_PUBLIC_KEY")
        _public(value)
        return value
    except LicenseError:
        private = _private(load_private_key())
        return private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
