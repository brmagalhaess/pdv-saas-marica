"""Release metadata signed with a pinned Ed25519 key; never executes an update."""
import base64
import re
from urllib.parse import urlsplit
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from .licensing import canonical,key_id,LicenseError

def validate_release(payload):
    if not isinstance(payload,dict) or payload.get('schema')!=1 or payload.get('purpose')!='pdv2-release':
        raise LicenseError('Manifesto de atualização inválido')
    if payload.get('component') not in ('terminal','cloud') or payload.get('channel') not in ('stable','pilot'):
        raise LicenseError('Canal de atualização inválido')
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',str(payload.get('version',''))):
        raise LicenseError('Versão inválida')
    if not re.fullmatch(r'[0-9a-f]{64}',str(payload.get('sha256',''))):raise LicenseError('Checksum inválido')
    if type(payload.get('size_bytes')) is not int or not 0<payload['size_bytes']<=2*1024**3:
        raise LicenseError('Tamanho do pacote inválido')
    parsed=urlsplit(str(payload.get('url','')))
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
        raise LicenseError('Pacote deve usar URL HTTPS sem credenciais')
    return payload

def sign_release(payload,private_pem):
    validate_release(payload)
    key=serialization.load_pem_private_key(private_pem.encode(),password=None)
    public=key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return {'payload':payload,'signature':base64.urlsafe_b64encode(key.sign(canonical(payload))).decode().rstrip('='),'key_id':key_id(public)}

def verify_release(envelope,public_pem):
    try:
        payload=envelope['payload'];signature=envelope['signature']
        if envelope['key_id']!=key_id(public_pem):raise LicenseError('Chave de atualização divergente')
        key=serialization.load_pem_public_key(public_pem.encode())
        key.verify(base64.urlsafe_b64decode(signature+'='*(-len(signature)%4)),canonical(payload))
        return validate_release(payload)
    except (KeyError,TypeError,ValueError,InvalidSignature) as exc:
        raise LicenseError('Assinatura da atualização inválida') from exc
