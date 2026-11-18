"""Mensagens da SEFAZ (cStat) e pedidos XML de consulta, evento e inutilização."""
from .xml import E, NFE_NS, clean_text

AUTHORIZED = {"100", "150"}            # autorizado / autorizado fora do prazo
DENIED = {"110", "301", "302", "303"}  # uso denegado
DUPLICATE = {"204", "539"}             # duplicidade de NF-e (com ou sem diferença na chave)
CANCELLED = {"101", "151", "155"}      # cancelamento homologado (também fora do prazo)
EVENT_OK = {"135", "136", "155"}
LOT_PROCESSED = "104"
LOT_RECEIVED = {"103", "105"}           # lote recebido / em processamento (resposta assíncrona)
SERVICE_OK = "107"
INUTILIZED = "102"

# Rejeições frequentes da NFC-e, com orientação ao lojista (o texto da SEFAZ vai junto).
HINTS = {
    "202": "Os dados da nota não puderam ser validados. Confira o cadastro fiscal da empresa e dos produtos.",
    "213": "O CNPJ do certificado não é o mesmo da empresa. Envie o certificado correto em Minha empresa > Nota fiscal.",
    "215": "Falha no formato do XML. Reenvie; se repetir, chame o suporte.",
    "225": "Falha no formato do XML. Reenvie; se repetir, chame o suporte.",
    "228": "A data de emissão está atrasada demais. Confira o relógio do computador do caixa.",
    "233": "A inscrição estadual do consumidor não pode ser usada na NFC-e.",
    "239": "Versão do leiaute não suportada pela SEFAZ.",
    "241": "Esta numeração já foi usada. Ajuste o próximo número da NFC-e no painel (nunca reutilize números).",
    "280": "O certificado digital é inválido.",
    "281": "O certificado digital venceu. Renove o certificado A1 e envie o novo arquivo no painel.",
    "297": "A assinatura não confere. Reenvie; se repetir, envie de novo o certificado no painel.",
    "302": "Uso denegado: a situação cadastral do consumidor tem pendência.",
    "301": "Uso denegado: a inscrição estadual da empresa está irregular na SEFAZ.",
    "373": "Em homologação a descrição do primeiro produto deve ser o texto de homologação.",
    "386": "Venda presencial exige data e hora de emissão próximas do envio.",
    "389": "O código do município da empresa está incorreto. Corrija o código IBGE em Minha empresa.",
    "394": "O QR Code está fora do padrão. Confira o CSC e o identificador em Minha empresa > Nota fiscal.",
    "395": "O QR Code está fora do padrão. Confira o CSC e o identificador em Minha empresa > Nota fiscal.",
    "462": "O CSC informado não confere. Confira o CSC e o identificador do CSC no portal da SEFAZ.",
    "464": "O código de hash do QR Code não confere. Confira o CSC.",
    "502": "Erro na chave de acesso. Reenvie; se repetir, chame o suporte.",
    "539": "Esta numeração já foi usada com outros dados. Ajuste o próximo número da NFC-e no painel.",
    "591": "O regime tributário (CRT) da empresa não confere com o cadastro na SEFAZ.",
    "598": "O documento do emitente não corresponde ao certificado.",
    "656": "Consumo indevido: muitas tentativas seguidas. Aguarde 1 hora antes de reenviar.",
    "694": "Falta o grupo de pagamento com cartão (bandeira/integração).",
    "778": "NCM inexistente. Corrija o NCM do produto.",
    "806": "Operação com ICMS-ST não permitida para este CFOP.",
    "972": "Falta o responsável técnico. A plataforma precisa configurar o CNPJ do responsável técnico.",
}


def status_message(code, reason=""):
    code = str(code or "")
    hint = HINTS.get(code, "")
    base = f"{code} - {reason}".strip(" -") if reason else code
    return f"{base}. {hint}" if hint else base


def consult_request(key, environment_code):
    return E("consSitNFe", E("tpAmb", text=environment_code), E("xServ", text="CONSULTAR"), E("chNFe", text=key),
             ns=NFE_NS, versao="4.00")


def status_request(cuf, environment_code):
    return E("consStatServ", E("tpAmb", text=environment_code), E("cUF", text=cuf), E("xServ", text="STATUS"),
             ns=NFE_NS, versao="4.00")


def cancel_event(key, cuf, environment_code, cnpj, when_text, protocol, reason, sequence=1):
    """evento 110111 (cancelamento) sem assinatura."""
    reason = clean_text(reason, 255)
    if len(reason) < 15:
        raise ValueError("Justificativa deve ter pelo menos 15 caracteres")
    event_id = f"ID110111{key}{int(sequence):02d}"
    info = E("infEvento",
             E("cOrgao", text=cuf), E("tpAmb", text=environment_code), E("CNPJ", text=cnpj),
             E("chNFe", text=key), E("dhEvento", text=when_text), E("tpEvento", text="110111"),
             E("nSeqEvento", text=int(sequence)), E("verEvento", text="1.00"),
             E("detEvento", E("descEvento", text="Cancelamento"), E("nProt", text=protocol),
               E("xJust", text=reason), versao="1.00"),
             Id=event_id)
    return E("evento", info, ns=NFE_NS, versao="1.00")


def event_batch(event, batch_id):
    return E("envEvento", E("idLote", text=batch_id), event, ns=NFE_NS, versao="1.00")


def inutilization_request(cuf, environment_code, year, cnpj, series, start, end, reason):
    reason = clean_text(reason, 255)
    if len(reason) < 15:
        raise ValueError("Justificativa deve ter pelo menos 15 caracteres")
    if not 1 <= int(start) <= int(end) <= 999999999:
        raise ValueError("Faixa de numeração inválida")
    year = str(year)[-2:]
    inut_id = f"ID{cuf}{year}{cnpj}65{int(series):03d}{int(start):09d}{int(end):09d}"
    info = E("infInut",
             E("tpAmb", text=environment_code), E("xServ", text="INUTILIZAR"), E("cUF", text=cuf),
             E("ano", text=year), E("CNPJ", text=cnpj), E("mod", text="65"), E("serie", text=int(series)),
             E("nNFIni", text=int(start)), E("nNFFin", text=int(end)), E("xJust", text=reason),
             Id=inut_id)
    return E("inutNFe", info, ns=NFE_NS, versao="4.00")


def authorization_batch(nfe, batch_id):
    return E("enviNFe", E("idLote", text=batch_id), E("indSinc", text="1"), nfe, ns=NFE_NS, versao="4.00")
