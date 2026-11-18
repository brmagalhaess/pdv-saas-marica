import requests

class ProviderError(RuntimeError):
    def __init__(self, message, status=None, outcome_unknown=False):
        super().__init__(message)
        self.status = status
        self.outcome_unknown = outcome_unknown

class ProviderClient:
    def __init__(self, base_url, session=None):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = (5, 35)

    def request(self, method, path, **kwargs):
        try:
            response = self.session.request(method, self.base_url + path, timeout=self.timeout,
                                            allow_redirects=False, **kwargs)
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise ProviderError("Provedor indisponível; consulte a referência antes de reenviar.",
                                outcome_unknown=method.upper() not in ("GET", "HEAD")) from exc
        if not 200 <= response.status_code < 300:
            raise ProviderError(f"Provedor recusou a operação (HTTP {response.status_code}).",
                                status=response.status_code,
                                outcome_unknown=method.upper() != "GET" and response.status_code >= 500)
        try:
            body = response.json()
        except (ValueError, requests.JSONDecodeError) as exc:
            raise ProviderError("Resposta inválida do provedor; consulte antes de reenviar.",
                                outcome_unknown=method.upper() != "GET") from exc
        if not isinstance(body, dict):
            raise ProviderError("Formato de resposta inesperado do provedor.", outcome_unknown=method.upper() != "GET")
        return body
