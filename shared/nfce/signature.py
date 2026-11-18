"""Assinatura XMLDSig enveloped da NF-e/NFC-e, eventos e inutilização (Manual de Integração 4.0, item 4.4).

- C14N 1.0 sem comentários (``REC-xml-c14n-20010315``) em SignedInfo e no Reference;
- transforms: enveloped-signature + C14N; DigestMethod SHA-1; SignatureMethod RSA-SHA1;
- Reference URI = "#" + Id do elemento assinado (infNFe, infEvento, infInut);
- KeyInfo/X509Data/X509Certificate com o certificado do titular (DER em Base64).

Implementação própria sobre ``cryptography`` (já dependência do projeto): os documentos
assinados são gerados por ``shared.nfce.xml`` e já estão em forma canônica, então a
canonicalização é exata sem lxml. ``verify`` confere digest e assinatura (usado nos testes e
para conferir o XML recebido do caixa).
"""
import base64
import hashlib

from .xml import DSIG_NS, E, NFE_NS, canonical

C14N = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
ENVELOPED = "http://www.w3.org/2000/09/xmldsig#enveloped-signature"
RSA_SHA1 = "http://www.w3.org/2000/09/xmldsig#rsa-sha1"
SHA1 = "http://www.w3.org/2000/09/xmldsig#sha1"


class SignatureError(ValueError):
    pass


def _digest(element, inherited_ns=NFE_NS):
    return base64.b64encode(hashlib.sha1(canonical(element, inherited_ns).encode("utf-8")).digest()).decode("ascii")


def signed_info(reference_id, digest_value):
    return E("SignedInfo",
             E("CanonicalizationMethod", Algorithm=C14N),
             E("SignatureMethod", Algorithm=RSA_SHA1),
             E("Reference",
               E("Transforms", E("Transform", Algorithm=ENVELOPED), E("Transform", Algorithm=C14N)),
               E("DigestMethod", Algorithm=SHA1),
               E("DigestValue", text=digest_value),
               URI="#" + reference_id),
             ns=DSIG_NS)


def rsa_sha1(private_key, data):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    return private_key.sign(data, padding.PKCS1v15(), hashes.SHA1())


def sign(parent, target, private_key, certificate_der, inherited_ns=NFE_NS):
    """Append <Signature> to ``parent`` signing its child ``target`` (which has an Id)."""
    if parent.find("Signature") is not None:
        raise SignatureError("Documento já assinado")
    reference = target.attrs.get("Id")
    if not reference:
        raise SignatureError("Elemento a assinar sem Id")
    info = signed_info(reference, _digest(target, inherited_ns))
    value = base64.b64encode(rsa_sha1(private_key, canonical(info).encode("utf-8"))).decode("ascii")
    # SignedInfo is canonicalized on its own: its namespace comes from Signature, so the
    # element inside the Signature does not repeat it.
    info.ns = None
    signature = E("Signature", info, E("SignatureValue", text=value),
                  E("KeyInfo", E("X509Data", E("X509Certificate", text=base64.b64encode(certificate_der).decode("ascii")))),
                  ns=DSIG_NS)
    parent.add(signature)
    return signature


def verify(parent, target_tag, inherited_ns=NFE_NS):
    """Check digest and RSA signature of ``parent``'s Signature. Returns the signer certificate."""
    from cryptography import x509
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    signature = parent.find("Signature")
    target = parent.find(target_tag)
    if signature is None or target is None:
        raise SignatureError("Assinatura ou elemento assinado ausente")
    info = signature.find("SignedInfo")
    reference = info.find("Reference")
    if reference.attrs.get("URI") != "#" + target.attrs.get("Id", ""):
        raise SignatureError("Referência da assinatura não aponta para o documento")
    if info.find("SignatureMethod").attrs.get("Algorithm") != RSA_SHA1 or \
            info.find("CanonicalizationMethod").attrs.get("Algorithm") != C14N:
        raise SignatureError("Algoritmo de assinatura inesperado")
    if reference.findtext("DigestValue") != _digest(target, inherited_ns):
        raise SignatureError("DigestValue não confere: o conteúdo foi alterado")
    certificate = x509.load_der_x509_certificate(base64.b64decode(signature.findtext("KeyInfo/X509Data/X509Certificate")))
    try:
        certificate.public_key().verify(base64.b64decode(signature.findtext("SignatureValue")),
                                        canonical(info, DSIG_NS).encode("utf-8"), padding.PKCS1v15(), hashes.SHA1())
    except InvalidSignature as exc:
        raise SignatureError("Assinatura inválida") from exc
    return certificate
