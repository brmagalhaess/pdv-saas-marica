"""SOAP 1.2 sobre TLS com certificado de cliente (A1) para os web services da SEFAZ.

- O certificado do servidor é sempre verificado. As SEFAZ usam cadeias ICP-Brasil que nem
  sempre estão no pacote do ``certifi``: ``PDV_SEFAZ_CA_BUNDLE`` aponta para um arquivo PEM com
  as autoridades (certifi + ICP-Brasil). Nunca se desliga a verificação.
- A chave privada só existe em memória durante a carga do contexto TLS: o PEM vai para um
  diretório temporário 0700, é carregado e apagado em seguida (o ``ssl`` do Python só lê arquivo).
- Erros: ``unreachable`` = certamente nada foi enviado (DNS, conexão recusada, tempo de conexão,
  handshake TLS) e o mesmo número pode sair em contingência; ``outcome_unknown`` = o pedido pode
  ter chegado (tempo de leitura esgotado, conexão derrubada depois de aberta, resposta truncada):
  a emissão consulta e, com o cliente esperando, sai contingência com número NOVO (``never_sent``).
"""
import os
import shutil
import ssl
import tempfile

import requests
from requests.adapters import HTTPAdapter
from urllib3 import exceptions as urllib3_exceptions

from integrations.base import ProviderError
from shared.nfce.webservices import SERVICES
from shared.nfce.xml import canonical

SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"


class TransportError(ProviderError):
    def __init__(self, message, status=None, outcome_unknown=False, unreachable=False):
        super().__init__(message, status=status, outcome_unknown=outcome_unknown)
        self.unreachable = unreachable


def envelope(service, body):
    namespace, _operation = SERVICES[service]
    return (f'<soap12:Envelope xmlns:soap12="{SOAP_NS}" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            f'xmlns:xsd="http://www.w3.org/2001/XMLSchema"><soap12:Body><nfeDadosMsg xmlns="{namespace}">'
            f'{canonical(body)}</nfeDadosMsg></soap12:Body></soap12:Envelope>')


def client_context(certificate):
    cafile = os.environ.get("PDV_SEFAZ_CA_BUNDLE") or None
    if cafile is None:
        try:
            import certifi
            cafile = certifi.where()
        except ImportError:
            cafile = None
    context = ssl.create_default_context(cafile=cafile)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    key_pem, cert_pem = certificate.pem_bundle()
    folder = tempfile.mkdtemp(prefix="pdv2-tls-")
    try:
        os.chmod(folder, 0o700)
        key_path, cert_path = os.path.join(folder, "k.pem"), os.path.join(folder, "c.pem")
        for path, content in ((key_path, key_pem), (cert_path, cert_pem)):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
        context.load_cert_chain(cert_path, key_path)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return context


class _ContextAdapter(HTTPAdapter):
    def __init__(self, context, **kwargs):
        self._context = context
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._context
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self._context
        return super().proxy_manager_for(*args, **kwargs)


def _chain(exc, depth=0):
    """The exception, its wrapped reasons (urllib3 MaxRetryError.reason, args) and causes."""
    if exc is None or depth > 8:
        return
    yield exc
    seen = []
    for inner in (getattr(exc, "reason", None), exc.__cause__, exc.__context__,
                  *(arg for arg in getattr(exc, "args", ()) if isinstance(arg, BaseException))):
        if isinstance(inner, BaseException) and inner is not exc and all(inner is not s for s in seen):
            seen.append(inner)
            yield from _chain(inner, depth + 1)


def never_sent(exc):
    """True only when the request certainly did not leave this machine: connection not established
    (DNS failure, refused, connect timeout) or the TLS handshake failed. Everything else is unknown."""
    if isinstance(exc, requests.ConnectTimeout):
        return True
    for item in _chain(exc):
        if isinstance(item, (urllib3_exceptions.NewConnectionError, urllib3_exceptions.ConnectTimeoutError,
                             urllib3_exceptions.SSLError, ssl.SSLError)):
            return True
        if isinstance(item, (urllib3_exceptions.ProtocolError, urllib3_exceptions.ReadTimeoutError)):
            return False
    return False


class SoapTransport:
    """``call(url, service, body_element) -> bytes`` (resposta SOAP)."""

    def __init__(self, certificate, timeout=(4, 25), session=None):
        self.timeout = timeout
        if session is None:
            session = requests.Session()
            session.mount("https://", _ContextAdapter(client_context(certificate)))
        self.session = session

    def call(self, url, service, body):
        if not str(url).startswith("https://"):
            raise TransportError("Endereço da SEFAZ precisa ser HTTPS", unreachable=True)
        namespace, operation = SERVICES[service]
        headers = {"Content-Type": f'application/soap+xml; charset=utf-8; action="{namespace}/{operation}"'}
        try:
            response = self.session.post(url, data=envelope(service, body).encode("utf-8"), headers=headers,
                                         timeout=self.timeout, allow_redirects=False)
        except requests.RequestException as exc:
            if never_sent(exc):
                raise TransportError("SEFAZ indisponível (sem conexão)", unreachable=True) from exc
            # Anything after the connection was open (read timeout, connection dropped, broken
            # chunked answer...) may have delivered the request: the outcome is unknown and the
            # number must never be reused for a contingency note.
            raise TransportError("SEFAZ não respondeu; a nota será consultada", outcome_unknown=True) from exc
        if response.status_code >= 500 and not response.content:
            raise TransportError(f"SEFAZ fora do ar (HTTP {response.status_code})", status=response.status_code,
                                 outcome_unknown=True)
        if len(response.content) > 5 * 1024 * 1024:
            raise TransportError("Resposta da SEFAZ grande demais", outcome_unknown=True)
        return response.content
