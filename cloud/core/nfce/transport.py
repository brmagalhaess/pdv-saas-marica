"""SOAP 1.2 sobre TLS com certificado de cliente (A1) para os web services da SEFAZ.

- O certificado do servidor é sempre verificado. As SEFAZ usam cadeias ICP-Brasil que nem
  sempre estão no pacote do ``certifi``: ``PDV_SEFAZ_CA_BUNDLE`` aponta para um arquivo PEM com
  as autoridades (certifi + ICP-Brasil). Nunca se desliga a verificação.
- A chave privada só existe em memória durante a carga do contexto TLS: o PEM vai para um
  diretório temporário 0700, é carregado e apagado em seguida (o ``ssl`` do Python só lê arquivo).
- Erros: ``unreachable`` = nada foi enviado (DNS, conexão recusada, handshake); ``outcome_unknown``
  = o pedido pode ter chegado (tempo de resposta esgotado). A emissão trata cada caso.
"""
import os
import shutil
import ssl
import tempfile

import requests
from requests.adapters import HTTPAdapter

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
        except (requests.ConnectTimeout, requests.exceptions.SSLError) as exc:
            raise TransportError("SEFAZ indisponível (sem conexão)", unreachable=True) from exc
        except requests.ConnectionError as exc:
            # A connection reset after the request was written is ambiguous; a refused one is not.
            unknown = isinstance(exc, requests.exceptions.ChunkedEncodingError)
            raise TransportError("SEFAZ indisponível (sem conexão)", unreachable=not unknown,
                                 outcome_unknown=unknown) from exc
        except requests.Timeout as exc:
            raise TransportError("SEFAZ não respondeu a tempo; a nota será consultada", outcome_unknown=True) from exc
        except requests.RequestException as exc:
            raise TransportError("Falha de comunicação com a SEFAZ", outcome_unknown=True) from exc
        if response.status_code >= 500 and not response.content:
            raise TransportError(f"SEFAZ fora do ar (HTTP {response.status_code})", status=response.status_code,
                                 outcome_unknown=True)
        if len(response.content) > 5 * 1024 * 1024:
            raise TransportError("Resposta da SEFAZ grande demais", outcome_unknown=True)
        return response.content
