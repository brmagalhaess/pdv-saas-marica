"""Web services da NFC-e (modelo 65, leiaute 4.00) por UF e ambiente, URLs do QR Code e de consulta.

Fonte conferida em 18/11/2026: tabelas públicas mantidas pelo projeto NFePHP
(``storage/autorizadores.json`` modelo 65, ``storage/wsnfe_4.00_mod65.xml`` e
``storage/uri_consulta_nfce.json``), que acompanham o Portal Nacional da NF-e e os portais
das SEFAZ. Os endereços mudam: a validação final é sempre um "Testar comunicação" em
homologação (docs/NFCE.md). Autorizadores próprios de NFC-e: AM, GO, MG, MS, MT, PR, RS e SP;
as demais UFs usam a SVRS (SEFAZ Virtual do RS) — inclusive BA, CE, PE, RJ, ES e DF.

``PDV_NFCE_WS_OVERRIDES`` (JSON ``{"UF": {"homologation": {"authorize": "https://..."}}}``) permite
corrigir um endereço sem nova versão.
"""
import json
import os

SERVICES = {
    # service: (wsdl namespace, SOAP action/operation)
    "authorize": ("http://www.portalfiscal.inf.br/nfe/wsdl/NFeAutorizacao4", "nfeAutorizacaoLote"),
    "consult": ("http://www.portalfiscal.inf.br/nfe/wsdl/NFeConsultaProtocolo4", "nfeConsultaNF"),
    "status": ("http://www.portalfiscal.inf.br/nfe/wsdl/NFeStatusServico4", "nfeStatusServicoNF"),
    "event": ("http://www.portalfiscal.inf.br/nfe/wsdl/NFeRecepcaoEvento4", "nfeRecepcaoEvento"),
    "inutilize": ("http://www.portalfiscal.inf.br/nfe/wsdl/NFeInutilizacao4", "nfeInutilizacaoNF"),
}

AUTHORIZER = {
    "AC": "SVRS", "AL": "SVRS", "AM": "AM", "AP": "SVRS", "BA": "SVRS", "CE": "SVRS", "DF": "SVRS",
    "ES": "SVRS", "GO": "GO", "MA": "SVRS", "MG": "MG", "MS": "MS", "MT": "MT", "PA": "SVRS",
    "PB": "SVRS", "PE": "SVRS", "PI": "SVRS", "PR": "PR", "RJ": "SVRS", "RN": "SVRS", "RO": "SVRS",
    "RR": "SVRS", "RS": "RS", "SC": "SVRS", "SE": "SVRS", "SP": "SP", "TO": "SVRS",
}


def _asmx(base):
    return {"authorize": base + "/ws/NfeAutorizacao/NFeAutorizacao4.asmx",
            "consult": base + "/ws/NfeConsulta/NfeConsulta4.asmx",
            "status": base + "/ws/NfeStatusServico/NfeStatusServico4.asmx",
            "event": base + "/ws/recepcaoevento/recepcaoevento4.asmx",
            "inutilize": base + "/ws/nfeinutilizacao/nfeinutilizacao4.asmx"}


def _named(base, consult="NFeConsultaProtocolo4", status="NFeStatusServico4", event="NFeRecepcaoEvento4",
           inutilize="NFeInutilizacao4", authorize="NFeAutorizacao4", suffix=""):
    return {"authorize": f"{base}/{authorize}{suffix}", "consult": f"{base}/{consult}{suffix}",
            "status": f"{base}/{status}{suffix}", "event": f"{base}/{event}{suffix}",
            "inutilize": f"{base}/{inutilize}{suffix}"}


_AM_MT = dict(authorize="NfeAutorizacao4", consult="NfeConsulta4", status="NfeStatusServico4",
              event="RecepcaoEvento4", inutilize="NfeInutilizacao4")

ENDPOINTS = {
    "SVRS": {"production": _asmx("https://nfce.svrs.rs.gov.br"),
             "homologation": _asmx("https://nfce-homologacao.svrs.rs.gov.br")},
    "RS": {"production": _asmx("https://nfce.sefazrs.rs.gov.br"),
           "homologation": _asmx("https://nfce-homologacao.sefazrs.rs.gov.br")},
    "SP": {"production": _named("https://nfce.fazenda.sp.gov.br/ws", suffix=".asmx"),
           "homologation": _named("https://homologacao.nfce.fazenda.sp.gov.br/ws", suffix=".asmx")},
    "MG": {"production": _named("https://nfce.fazenda.mg.gov.br/nfce/services"),
           "homologation": _named("https://hnfce.fazenda.mg.gov.br/nfce/services")},
    "PR": {"production": _named("https://nfce.sefa.pr.gov.br/nfce"),
           "homologation": _named("https://homologacao.nfce.sefa.pr.gov.br/nfce")},
    "GO": {"production": _named("https://nfe.sefaz.go.gov.br/nfe/services"),
           "homologation": _named("https://homolog.sefaz.go.gov.br/nfe/services")},
    "MS": {"production": _named("https://nfce.sefaz.ms.gov.br/ws"),
           "homologation": _named("https://hom.nfce.sefaz.ms.gov.br/ws")},
    "MT": {"production": _named("https://nfce.sefaz.mt.gov.br/nfcews/services", **_AM_MT),
           "homologation": _named("https://homologacao.sefaz.mt.gov.br/nfcews/services", **_AM_MT)},
    "AM": {"production": _named("https://nfce.sefaz.am.gov.br/nfce-services/services", **_AM_MT),
           "homologation": _named("https://homnfce.sefaz.am.gov.br/nfce-services/services", **_AM_MT)},
}

# URL do QR Code (consulta pelo leitor) por UF: (produção, homologação).
QR_URLS = {
    "AC": ("http://www.sefaznet.ac.gov.br/nfce/qrcode", "http://www.hml.sefaznet.ac.gov.br/nfce/qrcode"),
    "AL": ("http://nfce.sefaz.al.gov.br/QRCode/consultarNFCe.jsp", "http://nfce.sefaz.al.gov.br/QRCode/consultarNFCe.jsp"),
    "AM": ("https://sistemas.sefaz.am.gov.br/nfceweb/consultarNFCe.jsp", "https://sistemas.sefaz.am.gov.br/nfceweb-hom/consultarNFCe.jsp"),
    "AP": ("https://www.sefaz.ap.gov.br/nfce/nfcep.php", "https://www.sefaz.ap.gov.br/nfcehml/nfce.php"),
    "BA": ("http://nfe.sefaz.ba.gov.br/servicos/nfce/qrcode.aspx", "http://hnfe.sefaz.ba.gov.br/servicos/nfce/qrcode.aspx"),
    "CE": ("http://nfce.sefaz.ce.gov.br/pages/ShowNFCe.html", "http://nfceh.sefaz.ce.gov.br/pages/ShowNFCe.html"),
    "DF": ("http://www.fazenda.df.gov.br/nfce/qrcode", "http://dec.fazenda.df.gov.br/ConsultarNFCe.aspx"),
    "ES": ("http://app.sefaz.es.gov.br/ConsultaNFCe/qrcode.aspx", "http://homologacao.sefaz.es.gov.br/ConsultaNFCe/qrcode.aspx"),
    "GO": ("https://nfeweb.sefaz.go.gov.br/nfeweb/sites/nfce/danfeNFCe", "https://nfewebhomolog.sefaz.go.gov.br/nfeweb/sites/nfce/danfeNFCe"),
    "MA": ("http://www.nfce.sefaz.ma.gov.br/portal/consultarNFCe.jsp", "http://www.hom.nfce.sefaz.ma.gov.br/portal/consultarNFCe.jsp"),
    "MG": ("https://portalsped.fazenda.mg.gov.br/portalnfce/sistema/qrcode.xhtml", "https://portalsped.fazenda.mg.gov.br/portalnfce/sistema/qrcode.xhtml"),
    "MS": ("http://www.dfe.ms.gov.br/nfce/qrcode", "http://www.dfe.ms.gov.br/nfce/qrcode"),
    "MT": ("http://www.sefaz.mt.gov.br/nfce/consultanfce", "http://homologacao.sefaz.mt.gov.br/nfce/consultanfce"),
    "PA": ("https://appnfc.sefa.pa.gov.br/portal/view/consultas/nfce/nfceForm.seam", "https://appnfc.sefa.pa.gov.br/portal-homologacao/view/consultas/nfce/nfceForm.seam"),
    "PB": ("http://www.sefaz.pb.gov.br/nfce", "http://www.sefaz.pb.gov.br/nfcehom"),
    "PE": ("http://nfce.sefaz.pe.gov.br/nfce/consulta", "http://nfcehomolog.sefaz.pe.gov.br/nfce/consulta"),
    "PI": ("http://www.sefaz.pi.gov.br/nfce/qrcode", "http://www.sefaz.pi.gov.br/nfce/qrcode"),
    "PR": ("http://www.fazenda.pr.gov.br/nfce/qrcode", "http://www.fazenda.pr.gov.br/nfce/qrcode"),
    "RJ": ("https://consultadfe.fazenda.rj.gov.br/consultaNFCe/QRCode", "http://www4.fazenda.rj.gov.br/consultaNFCe/QRCode"),
    "RN": ("http://nfce.set.rn.gov.br/consultarNFCe.aspx", "http://hom.nfce.set.rn.gov.br/consultarNFCe.aspx"),
    "RO": ("http://www.nfce.sefin.ro.gov.br/consultanfce/consulta.jsp", "http://www.nfce.sefin.ro.gov.br/consultanfce/consulta.jsp"),
    "RR": ("https://www.sefaz.rr.gov.br/servlet/qrcode", "http://200.174.88.103:8080/nfce/servlet/qrcode"),
    "RS": ("https://www.sefaz.rs.gov.br/NFCE/NFCE-COM.aspx", "https://www.sefaz.rs.gov.br/NFCE/NFCE-COM.aspx"),
    "SC": ("https://sat.sef.sc.gov.br/nfce/consulta", "https://hom.sat.sef.sc.gov.br/nfce/consulta"),
    "SE": ("http://www.nfce.se.gov.br/nfce/qrcode", "http://www.hom.nfe.se.gov.br/nfce/qrcode"),
    "SP": ("https://www.nfce.fazenda.sp.gov.br/qrcode", "https://www.homologacao.nfce.fazenda.sp.gov.br/qrcode"),
    "TO": ("http://www.sefaz.to.gov.br/nfce/qrcode", "http://homologacao.sefaz.to.gov.br/nfce/qrcode"),
}

# urlChave (consulta pública pela chave, impressa no DANFE): (produção, homologação).
KEY_URLS = {
    "AC": ("www.sefaznet.ac.gov.br/nfce/consulta",) * 2,
    "AL": ("www.sefaz.al.gov.br/nfce/consulta",) * 2,
    "AM": ("www.sefaz.am.gov.br/nfce/consulta",) * 2,
    "AP": ("www.sefaz.ap.gov.br/nfce/consulta",) * 2,
    "BA": ("http://www.sefaz.ba.gov.br/nfce/consulta", "http://hinternet.sefaz.ba.gov.br/nfce/consulta"),
    "CE": ("www.sefaz.ce.gov.br/nfce/consulta",) * 2,
    "DF": ("www.fazenda.df.gov.br/nfce/consulta",) * 2,
    "ES": ("www.sefaz.es.gov.br/nfce/consulta",) * 2,
    "GO": ("www.sefaz.go.gov.br/nfce/consulta", "www.nfce.go.gov.br/post/ver/214413/consulta-nfc-e-homologacao"),
    "MA": ("www.sefaz.ma.gov.br/nfce/consulta",) * 2,
    "MG": ("https://portalsped.fazenda.mg.gov.br/portalnfce", "https://hportalsped.fazenda.mg.gov.br/portalnfce"),
    "MS": ("http://www.dfe.ms.gov.br/nfce/consulta",) * 2,
    "MT": ("http://www.sefaz.mt.gov.br/nfce/consultanfce", "http://homologacao.sefaz.mt.gov.br/nfce/consultanfce"),
    "PA": ("www.sefa.pa.gov.br/nfce/consulta",) * 2,
    "PB": ("www.sefaz.pb.gov.br/nfce/consulta", "www.sefaz.pb.gov.br/nfcehom"),
    "PE": ("nfce.sefaz.pe.gov.br/nfce/consulta",) * 2,
    "PI": ("www.sefaz.pi.gov.br/nfce/consulta",) * 2,
    "PR": ("http://www.fazenda.pr.gov.br/nfce/consulta",) * 2,
    "RJ": ("www.fazenda.rj.gov.br/nfce/consulta",) * 2,
    "RN": ("www.set.rn.gov.br/nfce/consulta",) * 2,
    "RO": ("www.sefin.ro.gov.br/nfce/consulta",) * 2,
    "RR": ("www.sefaz.rr.gov.br/nfce/consulta",) * 2,
    "RS": ("www.sefaz.rs.gov.br/nfce/consulta",) * 2,
    "SC": ("https://sat.sef.sc.gov.br/nfce/consulta", "https://hom.sat.sef.sc.gov.br/nfce/consulta"),
    "SE": ("http://www.nfce.se.gov.br/nfce/consulta", "http://www.hom.nfe.se.gov.br/nfce/consulta"),
    "SP": ("https://www.nfce.fazenda.sp.gov.br/NFCeConsultaPublica", "https://www.homologacao.nfce.fazenda.sp.gov.br/NFCeConsultaPublica"),
    "TO": ("www.sefaz.to.gov.br/nfce/consulta", "http://homologacao.sefaz.to.gov.br/nfce/consulta.jsf"),
}

# Fuso da UF para dhEmi (sem horário de verão desde 2019). AM ocidental (-05) não é tratado.
TIME_ZONES = {"AC": "America/Rio_Branco", "AM": "America/Manaus", "RR": "America/Boa_Vista",
              "RO": "America/Porto_Velho", "MT": "America/Cuiaba", "MS": "America/Campo_Grande"}
DEFAULT_TIME_ZONE = "America/Sao_Paulo"

# Prazo de cancelamento da NFC-e em minutos (regra geral: 30 minutos da autorização,
# Ajuste SINIEF 19/16). UFs com prazo diferente entram aqui depois de conferidas.
CANCEL_MINUTES = {}
DEFAULT_CANCEL_MINUTES = 30


def _index(environment):
    if environment not in ("production", "homologation"):
        raise ValueError("Ambiente fiscal inválido")
    return 0 if environment == "production" else 1


def _overrides():
    raw = os.environ.get("PDV_NFCE_WS_OVERRIDES", "")
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def endpoint(uf, environment, service):
    if service not in SERVICES:
        raise ValueError("Serviço desconhecido")
    _index(environment)
    if uf not in AUTHORIZER:
        raise ValueError("UF sem autorizador de NFC-e conhecido")
    override = (_overrides().get(uf) or {}).get(environment) or {}
    url = override.get(service) if isinstance(override, dict) else None
    if isinstance(url, str) and url.startswith("https://"):
        return url
    return ENDPOINTS[AUTHORIZER[uf]][environment][service]


def qr_url(uf, environment):
    return QR_URLS[uf][_index(environment)]


def key_url(uf, environment):
    return KEY_URLS[uf][_index(environment)]


def time_zone(uf):
    return TIME_ZONES.get(uf, DEFAULT_TIME_ZONE)


def cancel_minutes(uf):
    return CANCEL_MINUTES.get(uf, DEFAULT_CANCEL_MINUTES)
