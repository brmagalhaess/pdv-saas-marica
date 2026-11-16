"""Release metadata signed with a pinned Ed25519 key; never executes an update."""
import base64
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from .licensing import canonical,key_id,LicenseError,load_private_key,load_public_key

RELEASE_KINDS=('bundle','installer')
VERSION_PATTERN=r'[0-9]+\.[0-9]+\.[0-9]+'

def version_tuple(value):
    if not isinstance(value,str) or not re.fullmatch(VERSION_PATTERN,value):raise LicenseError('Versão inválida')
    return tuple(map(int,value.split('.')))

def validate_release(payload):
    if not isinstance(payload,dict) or payload.get('schema')!=1 or payload.get('purpose')!='pdv2-release':
        raise LicenseError('Manifesto de atualização inválido')
    if payload.get('component') not in ('terminal','cloud') or payload.get('channel') not in ('stable','pilot'):
        raise LicenseError('Canal de atualização inválido')
    if not re.fullmatch(VERSION_PATTERN,str(payload.get('version',''))):
        raise LicenseError('Versão inválida')
    # Optional fields: absent on manifests published before the automatic updater.
    if 'kind' in payload and payload['kind'] not in RELEASE_KINDS:
        raise LicenseError('Tipo de pacote inválido')
    if 'min_version' in payload:
        try:minimum=version_tuple(payload['min_version'])
        except LicenseError:raise LicenseError('Versão mínima inválida')
        if minimum>version_tuple(payload['version']):raise LicenseError('Versão mínima acima da versão publicada')
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

def _configured(name):
    value=os.environ.get(name,'')
    filename=os.environ.get(name+'_FILE','')
    if filename:
        try:value=Path(filename).read_text().strip()
        except OSError as exc:raise LicenseError(f'Arquivo de {name} indisponível') from exc
    return value.replace('\\n','\n')

def _release_keys():
    """Dedicated release key pair; the license pair is the fallback when none is configured."""
    private=_configured('PDV_RELEASE_PRIVATE_KEY')
    if not private:
        if _configured('PDV_RELEASE_PUBLIC_KEY'):raise LicenseError('Chave pública de versões configurada sem a chave privada')
        return load_private_key(),load_public_key()
    try:
        key=serialization.load_pem_private_key(private.encode(),password=None)
        if not isinstance(key,Ed25519PrivateKey):raise ValueError('not Ed25519')
        derived=key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        public=_configured('PDV_RELEASE_PUBLIC_KEY') or derived
        if key_id(public)!=key_id(derived):raise ValueError('key pair mismatch')
    except (ValueError,TypeError) as exc:
        raise LicenseError('Chaves de versões inválidas') from exc
    return private,public

def load_release_private_key():
    return _release_keys()[0]

def load_release_public_key():
    return _release_keys()[1]
