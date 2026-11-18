"""Focus NFe NFC-e connector (API v2). The payload is built from the tax engine (core.nfce.backends)."""
import os
import re
from urllib.parse import quote, urljoin, urlsplit
import requests
import xml.etree.ElementTree as ET
from .base import ProviderClient, ProviderError

URLS = {"homologation":"https://homologacao.focusnfe.com.br", "production":"https://api.focusnfe.com.br"}
NS = {'n':'http://www.portalfiscal.inf.br/nfe'}

class FocusNFe(ProviderClient):
    def __init__(self, token, environment="homologation", session=None):
        if environment not in URLS or not isinstance(token,str) or not token.strip():
            raise ValueError("Configure token e ambiente fiscal")
        super().__init__(URLS[environment],session)
        self.token, self.environment = token, environment

    def _ref(self, reference):
        if not isinstance(reference,str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}",reference):
            raise ValueError("Referência fiscal inválida")
        return quote(reference,safe="")

    def _live(self):
        if self.environment == "production" and os.environ.get("PDV_ALLOW_LIVE_FISCAL") != "1":
            raise ProviderError("Emissão em produção ainda não habilitada neste ambiente.")

    def submit(self, reference, payload):
        self._ref(reference)
        if not isinstance(payload,dict) or not payload.get("items"):
            raise ValueError("Informe o payload fiscal validado, incluindo itens")
        self._live()
        return self.request("POST","/v2/nfce",params={"ref":reference},json=payload,auth=(self.token,""))

    def get(self, reference):
        return self.request("GET","/v2/nfce/"+self._ref(reference),params={"completa":1},auth=(self.token,""))

    def cancel(self, reference, reason):
        if not isinstance(reason,str) or not 15 <= len(reason.strip()) <= 255:
            raise ValueError("Justificativa fiscal deve ter de 15 a 255 caracteres")
        self._live()
        return self.request("DELETE","/v2/nfce/"+self._ref(reference),json={"justificativa":reason.strip()},auth=(self.token,""))

    def inutilize(self, cnpj, series, start, end, reason):
        if not isinstance(reason,str) or not 15 <= len(reason.strip()) <= 255:
            raise ValueError("Justificativa fiscal deve ter de 15 a 255 caracteres")
        self._live()
        return self.request("POST","/v2/nfce/inutilizacao",json={"cnpj":cnpj,"serie":str(series),"numero_inicial":str(start),
                            "numero_final":str(end),"justificativa":reason.strip()},auth=(self.token,""))

    def _download(self, path):
        """Download only from this configured provider; never forward credentials elsewhere."""
        if not isinstance(path,str) or not path or len(path)>2048:
            raise ProviderError("Caminho do XML indisponível")
        url=urljoin(self.base_url+'/',path)
        target,base=urlsplit(url),urlsplit(self.base_url)
        if (target.scheme,target.netloc)!=(base.scheme,base.netloc) or target.username or target.password or target.fragment:
            raise ProviderError("Origem do XML não autorizada")
        try:
            with self.session.get(url,auth=(self.token,''),timeout=(5,35),allow_redirects=False,stream=True) as response:
                if response.status_code!=200:
                    raise ProviderError("Download fiscal indisponível",status=response.status_code)
                chunks=[];length=0
                for chunk in response.iter_content(65536):
                    length+=len(chunk)
                    if length>10*1024*1024:raise ProviderError("XML excede o limite de tamanho")
                    chunks.append(chunk)
            data=b''.join(chunks)
        except requests.RequestException as exc:
            raise ProviderError("Não foi possível recuperar o XML fiscal") from exc
        # DTD/entity declarations are unnecessary for NFC-e and must never be expanded.
        if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
            raise ProviderError("Declaração XML não permitida")
        try:root=ET.fromstring(data)
        except ET.ParseError as exc:raise ProviderError("Provedor retornou XML inválido") from exc
        return data,root

    def download_xml(self, path, access_key, protocol):
        data,root=self._download(path)
        invoice=root.find('.//n:infNFe',NS);authorization=root.find('.//n:infProt',NS)
        if (invoice is None or invoice.get('Id')!='NFe'+str(access_key) or authorization is None
            or authorization.findtext('n:chNFe',namespaces=NS)!=str(access_key)
            or authorization.findtext('n:nProt',namespaces=NS)!=str(protocol)
            or authorization.findtext('n:cStat',namespaces=NS) not in ('100','150')):
            raise ProviderError("XML não corresponde à autorização da nota")
        return data

    def download_event_xml(self, path, access_key):
        """procEventoNFe of the cancellation (110111) of this access key, registered (135/136/155)."""
        data,root=self._download(path)
        info=root.find('.//n:retEvento/n:infEvento',NS)
        event=root.find('.//n:evento/n:infEvento',NS)
        if (event is None or info is None or event.findtext('n:chNFe',namespaces=NS)!=str(access_key)
            or event.findtext('n:tpEvento',namespaces=NS)!='110111'
            or info.findtext('n:cStat',namespaces=NS) not in ('135','136','155')):
            raise ProviderError("XML não corresponde ao cancelamento da nota")
        return data

    def download_inutilization_xml(self, path):
        data,root=self._download(path)
        info=root.find('.//n:retInutNFe/n:infInut',NS)
        if info is None or info.findtext('n:cStat',namespaces=NS)!='102':
            raise ProviderError("XML não corresponde à inutilização homologada")
        return data

    @staticmethod
    def normalized_status(response):
        return {"autorizado":"authorized","cancelado":"cancelled","erro_autorizacao":"rejected",
                "denegado":"denied","processando_autorizacao":"processing"}.get(response.get("status"),"pending")
