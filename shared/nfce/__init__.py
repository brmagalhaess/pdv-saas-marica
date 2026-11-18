"""NFC-e (modelo 65, leiaute 4.00): XML, chave de acesso, QR Code, assinatura e mensagens da SEFAZ.

Python puro (``cryptography`` só para assinar/verificar): a nuvem monta e transmite, o caixa monta
a nota de contingência off-line com o mesmo código. Ver docs/NFCE.md.
"""
from .builder import NfceDraft, NfceError, build_nfce, digest_value
from .keys import access_key, valid_key
from .payments import print_decision, tpag

__all__ = ["NfceDraft", "NfceError", "build_nfce", "digest_value", "access_key", "valid_key", "print_decision", "tpag"]
