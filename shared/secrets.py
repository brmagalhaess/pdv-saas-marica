"""Encrypt provider credentials before persisting them in the application DB."""
import os
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken

def _key():
    value=os.environ.get("PDV_ENCRYPTION_KEY","")
    filename=os.environ.get("PDV_ENCRYPTION_KEY_FILE","")
    if filename: value=Path(filename).read_text().strip()
    if not value: raise ValueError("PDV_ENCRYPTION_KEY não configurada")
    return Fernet(value.encode())

def encrypt_secret(value):
    if not isinstance(value,str): raise ValueError("Segredo inválido")
    return _key().encrypt(value.encode()).decode()

def decrypt_secret(value):
    try: return _key().decrypt(value.encode()).decode()
    except InvalidToken as exc: raise ValueError("Credencial não pôde ser descriptografada") from exc
