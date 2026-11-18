"""Dois modos de emissão com o mesmo contrato (docs/NFCE.md, "Abstração"):

    backend.status() -> {"online": bool, "message": str}
    backend.issue(IssueRequest) -> IssueResult        # tpEmis 1; contingência: issue_offline
    backend.consult(ref: Ref) -> IssueResult
    backend.cancel(ref: Ref, reason, when) -> EventResult
    backend.inutilize(series, start, end, reason, year) -> EventResult

``SefazBackend`` monta, assina e transmite direto na SEFAZ (certificado A1 da empresa);
``FocusBackend`` usa o provedor Focus NFe com o payload JSON montado do mesmo motor. Os dois
recebem o transporte por injeção: os testes de contrato rodam sem rede.

Nenhum método aqui toca no banco: ``core.nfce.service`` faz a ponte com os modelos.
"""
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from integrations.base import ProviderError
from shared.fiscal.data import UF_CODES
from shared.fiscal.validators import cnpj_value
from shared.nfce import messages, qrcode, responses, signature
from shared.nfce.builder import ENVIRONMENT_CODE, NfceError, approx_text, build_nfce, local_iso, money
from shared.nfce.payments import CARD_TPAG, tpag
from shared.nfce.webservices import endpoint, key_url
from shared.nfce.xml import E, NFE_NS, XMLError, from_etree, parse, tobytes

from .transport import TransportError


@dataclass
class IssueRequest:
    issuer: dict
    doc: dict
    environment: str
    reference: str
    payments: list
    issued_at: datetime
    series: int = None
    number: int = None
    cnf: str = None
    emission_type: int = 1
    contingency: dict = None
    customer: dict = None
    change_cents: int = 0
    payment_names: dict = field(default_factory=dict)
    tech: dict = field(default_factory=dict)
    app_version: str = "PDV2"
    qr_version: int = 3
    csc_id: str = ""
    csc: str = ""


@dataclass
class IssueResult:
    status: str                      # authorized | rejected | pending | contingency | cancelled | not_found
    key: str = ""
    number: int = None
    series: int = None
    protocol: str = ""
    authorized_at: str = ""
    code: str = ""
    message: str = ""
    nfe_xml: bytes = None            # NFe assinada (modo SEFAZ)
    proc_xml: bytes = None           # nfeProc autorizado
    qr_url: str = ""
    url_key: str = ""
    xml_path: str = ""               # Focus: caminho para baixar o XML
    issued_at: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class EventResult:
    status: str                      # registered | rejected | pending
    protocol: str = ""
    code: str = ""
    message: str = ""
    proc_xml: bytes = None
    raw: dict = field(default_factory=dict)


@dataclass
class Ref:
    """Documento já existente: chave + XML assinado (SEFAZ) ou referência (Focus)."""
    reference: str = ""
    key: str = ""
    protocol: str = ""
    nfe_xml: bytes = None
    environment: str = "homologation"


def _now():
    return datetime.now(timezone.utc)


def _batch_id(key_or_ref):
    # idLote: up to 15 digits, unique enough per call.
    return str(int(hashlib.sha1((str(key_or_ref) + _now().isoformat()).encode()).hexdigest(), 16))[:15]


# ======================================================================== SEFAZ


class SefazBackend:
    name = "sefaz"

    def __init__(self, certificate, transport, uf, environment):
        if environment not in ENVIRONMENT_CODE:
            raise ValueError("Ambiente fiscal inválido")
        self.certificate = certificate
        self.transport = transport
        self.uf = uf
        self.environment = environment

    @property
    def code(self):
        return ENVIRONMENT_CODE[self.environment]

    def _call(self, service, body):
        return self.transport.call(endpoint(self.uf, self.environment, service), service, body)

    # ---------------------------------------------------------------- status
    def status(self):
        data = self._call("status", messages.status_request(UF_CODES[self.uf], self.code))
        result = responses.service_status(data)
        return {"online": result["online"], "message": messages.status_message(result["status"], result["reason"]),
                "code": result["status"]}

    # ---------------------------------------------------------------- build
    def build(self, request):
        """Build, add the QR Code and sign. Returns (draft, signed NFe element)."""
        draft = build_nfce(request.issuer, request.doc, series=request.series, number=request.number,
                           issued_at=request.issued_at, environment=self.environment, cnf=request.cnf,
                           emission_type=request.emission_type, contingency=request.contingency,
                           customer=request.customer, payments=request.payments, change_cents=request.change_cents,
                           payment_names=request.payment_names, tech=request.tech, app_version=request.app_version)
        offline = request.emission_type == 9
        if request.qr_version == 2:
            qr = qrcode.build_v2(draft.key, self.code, request.csc_id, request.csc, uf=self.uf,
                                 environment=self.environment, offline=offline, issued_at=draft.issued_at,
                                 total_cents=draft.total_cents, digest_value=draft.digest_value)
        else:
            dest_type, dest_id = qrcode.dest_identity(draft.dest_document)
            qr = qrcode.build_v3(draft.key, self.code, uf=self.uf, environment=self.environment, offline=offline,
                                 issued_at=draft.issued_at, total_cents=draft.total_cents, dest_type=dest_type,
                                 dest_id=dest_id, signer=self.certificate.sign)
        draft.supplement(qr, key_url(self.uf, self.environment))
        signature.sign(draft.nfe, draft.infnfe, self.certificate.private_key, self.certificate.der)
        return draft, draft.nfe

    def sign_received(self, nfe):
        """Sign an unsigned NFe built by the register (offline contingency)."""
        infnfe = nfe.find("infNFe")
        if infnfe is None or nfe.find("infNFeSupl") is None:
            raise NfceError("XML da contingência incompleto")
        signature.sign(nfe, infnfe, self.certificate.private_key, self.certificate.der)
        return nfe

    # ---------------------------------------------------------------- issue
    def issue(self, request):
        draft, nfe = self.build(request)
        result = self._send(nfe, draft.key)
        result.qr_url = draft.nfe.findtext("infNFeSupl/qrCode")
        result.url_key = draft.nfe.findtext("infNFeSupl/urlChave")
        result.number, result.series, result.issued_at = request.number, request.series, draft.issued_at
        return result

    def issue_offline(self, request):
        """Contingência off-line feita na nuvem (SEFAZ fora): assina, não transmite."""
        request.emission_type = 9
        draft, nfe = self.build(request)
        return IssueResult(status="contingency", key=draft.key, number=request.number, series=request.series,
                           nfe_xml=tobytes(nfe), qr_url=nfe.findtext("infNFeSupl/qrCode"),
                           url_key=nfe.findtext("infNFeSupl/urlChave"), issued_at=draft.issued_at)

    def transmit(self, nfe_xml):
        """Send an already signed NFe (contingency) and read the answer."""
        nfe = from_etree(parse(nfe_xml))
        key = nfe.find("infNFe").attrs["Id"][3:]
        return self._send(nfe, key)

    def _send(self, nfe, key):
        signed = tobytes(nfe)
        nfe_for_batch = from_etree(parse(signed))
        nfe_for_batch.ns = None
        try:
            data = self._call("authorize", messages.authorization_batch(nfe_for_batch, _batch_id(key)))
        except TransportError as exc:
            if exc.unreachable:
                raise
            return IssueResult(status="pending", key=key, nfe_xml=signed, message=str(exc))
        try:
            answer = responses.authorization(data)
        except XMLError as exc:
            return IssueResult(status="pending", key=key, nfe_xml=signed, message=str(exc))
        return self._result(answer["batch_status"], answer["batch_reason"], answer["protocol"], key, signed)

    def _result(self, batch_status, batch_reason, protocol, key, signed):
        if batch_status in messages.LOT_RECEIVED:
            return IssueResult(status="pending", key=key, nfe_xml=signed, code=batch_status,
                               message=messages.status_message(batch_status, batch_reason))
        if not protocol:
            return IssueResult(status="rejected", key=key, nfe_xml=signed, code=batch_status,
                               message=messages.status_message(batch_status, batch_reason))
        code, reason = protocol["status"], protocol["reason"]
        if code in messages.AUTHORIZED:
            return IssueResult(status="authorized", key=key, protocol=protocol["protocol"],
                               authorized_at=protocol["received_at"], code=code, message=reason, nfe_xml=signed,
                               proc_xml=responses.nfe_proc(from_etree(parse(signed)), protocol["element"]))
        if code in messages.DUPLICATE:
            # Already received before (answer lost): the consultation brings the protocol.
            found = self.consult(Ref(key=key, nfe_xml=signed))
            if found.status in ("authorized", "cancelled"):
                return found
            return IssueResult(status="rejected", key=key, nfe_xml=signed, code=code,
                               message=messages.status_message(code, reason))
        status = "rejected"
        message = messages.status_message(code, reason)
        if code in messages.DENIED:
            message = "Uso denegado: " + message
        return IssueResult(status=status, key=key, nfe_xml=signed, code=code, message=message)

    # ---------------------------------------------------------------- consult
    def consult(self, ref):
        data = self._call("consult", messages.consult_request(ref.key, self.code))
        answer = responses.consultation(data)
        code = answer["status"]
        protocol = answer["protocol"]
        if code in messages.CANCELLED or any(e["type"] == "110111" and e["status"] in messages.EVENT_OK
                                            for e in answer["events"]):
            cancel = next((e for e in answer["events"] if e["type"] == "110111"), None)
            return IssueResult(status="cancelled", key=ref.key, code=code, message=answer["reason"],
                               protocol=protocol["protocol"] if protocol else "",
                               raw={"cancel_protocol": cancel["protocol"] if cancel else "",
                                    "cancel_proc": tobytes(cancel["element"], True) if cancel else None})
        if code in messages.AUTHORIZED and protocol:
            proc = responses.nfe_proc(from_etree(parse(ref.nfe_xml)), protocol["element"]) if ref.nfe_xml else None
            return IssueResult(status="authorized", key=ref.key, protocol=protocol["protocol"],
                               authorized_at=protocol["received_at"], code=code, message=answer["reason"],
                               nfe_xml=ref.nfe_xml, proc_xml=proc)
        if code == "217":  # NF-e não consta na base de dados da SEFAZ
            return IssueResult(status="not_found", key=ref.key, code=code, message=answer["reason"])
        if code in messages.DENIED:
            return IssueResult(status="rejected", key=ref.key, code=code,
                               message="Uso denegado: " + messages.status_message(code, answer["reason"]))
        return IssueResult(status="pending", key=ref.key, code=code,
                           message=messages.status_message(code, answer["reason"]))

    # ---------------------------------------------------------------- events
    def cancel(self, ref, reason, when=None, sequence=1):
        cnpj = cnpj_value(self.certificate.cnpj) or ref.key[6:20]
        event = messages.cancel_event(ref.key, UF_CODES[self.uf], self.code, ref.key[6:20],
                                      local_iso(when or _now(), self.uf), ref.protocol, reason, sequence)
        signature.sign(event, event.find("infEvento"), self.certificate.private_key, self.certificate.der)
        signed = tobytes(event)
        batch_event = from_etree(parse(signed))
        batch_event.ns = None
        data = self._call("event", messages.event_batch(batch_event, _batch_id(ref.key)))
        answer = responses.event(data)
        code = answer["status"]
        if code in messages.EVENT_OK:
            return EventResult(status="registered", protocol=answer["protocol"], code=code, message=answer["reason"],
                               proc_xml=responses.event_proc(from_etree(parse(signed)), answer["element"]),
                               raw={"cnpj": cnpj})
        if code == "573":  # duplicidade de evento: already registered, take it from the consultation
            found = self.consult(ref)
            if found.status == "cancelled":
                return EventResult(status="registered", protocol=found.raw.get("cancel_protocol", ""), code=code,
                                   message=answer["reason"], proc_xml=found.raw.get("cancel_proc"))
        return EventResult(status="rejected", code=code or answer["batch_status"],
                           message=messages.status_message(code or answer["batch_status"],
                                                           answer["reason"] or answer["batch_reason"]))

    def inutilize(self, series, start, end, reason, year, cnpj):
        request = messages.inutilization_request(UF_CODES[self.uf], self.code, year, cnpj, series, start, end, reason)
        signature.sign(request, request.find("infInut"), self.certificate.private_key, self.certificate.der)
        signed = tobytes(request)
        body = from_etree(parse(signed))
        data = self._call("inutilize", body)
        answer = responses.inutilization(data)
        code = answer["status"]
        if code == messages.INUTILIZED:
            return EventResult(status="registered", protocol=answer["protocol"], code=code, message=answer["reason"],
                               proc_xml=responses.inutilization_proc(from_etree(parse(signed)), answer["element"]))
        return EventResult(status="rejected", code=code, message=messages.status_message(code, answer["reason"]))


# ======================================================================== Focus NFe


def _brl(cents):
    return money(cents)


def focus_payload(request):
    """JSON da API v2 da Focus NFe (NFC-e) a partir do motor (sem payload pronto do cliente)."""
    doc, issuer = request.doc, request.issuer
    if not doc.get("ready"):
        raise NfceError("Há produtos sem dados fiscais completos para a nota",
                        [f"Item {item['number']}: {issue['message']}" for item in doc["items"]
                         for issue in item.get("issues", []) if issue.get("severity") == "error"])
    items = []
    for item in doc["items"]:
        icms = item["icms"]
        row = {"numero_item": item["number"], "codigo_produto": item["code"] or item["product_id"],
               "descricao": item["description"][:120], "codigo_ncm": item["ncm"], "cfop": item["cfop"],
               "unidade_comercial": item["unit"], "quantidade_comercial": item["quantity"],
               "valor_unitario_comercial": item["unit_price"], "valor_bruto": _brl(item["gross_cents"]),
               "unidade_tributavel": item["tax_unit"], "quantidade_tributavel": item["tax_quantity"],
               "valor_unitario_tributavel": item["tax_unit_price"],
               "codigo_barras_comercial": item["gtin"], "codigo_barras_tributavel": item.get("tax_gtin") or item["gtin"],
               "icms_origem": icms["origin"], "icms_situacao_tributaria": icms["csosn"] or icms["cst"]}
        if item["discount_cents"]:
            row["valor_desconto"] = _brl(item["discount_cents"])
        if item.get("cest"):
            row["cest"] = item["cest"]
        if item.get("cbenef"):
            row["codigo_beneficio_fiscal"] = item["cbenef"]
        if icms["bc_mode"] is not None and icms["rate"] is not None:
            row.update(icms_modalidade_base_calculo=icms["bc_mode"], icms_base_calculo=_brl(icms["bc_cents"]),
                       icms_aliquota=icms["rate"], icms_valor=_brl(icms["value_cents"]))
            if icms.get("bc_reduction"):
                row["icms_reducao_base_calculo"] = icms["bc_reduction"]
        if icms.get("fcp_rate"):
            row.update(fcp_percentual=icms["fcp_rate"], fcp_valor=_brl(icms["fcp_cents"]))
        retained = icms.get("st_retained")
        if retained:
            row.update(icms_base_calculo_retido_st=_brl(retained["bc_cents"]), icms_aliquota_final=retained["rate"],
                       icms_valor_substituto=_brl(retained["substitute_cents"]),
                       icms_valor_retido_st=_brl(retained["value_cents"]))
        for tax, data in (("pis", item.get("pis")), ("cofins", item.get("cofins"))):
            if data:
                row[f"{tax}_situacao_tributaria"] = data["cst"]
                if data["group"] in ("Aliq", "Outr"):
                    row.update({f"{tax}_base_calculo": _brl(data["bc_cents"]), f"{tax}_aliquota_porcentual": data["rate"],
                                f"{tax}_valor": _brl(data["value_cents"])})
        if item["approx_tax"]["total_cents"]:
            row["valor_total_tributos"] = _brl(item["approx_tax"]["total_cents"])
        items.append(row)
    payments = []
    for payment in request.payments:
        code = tpag(payment["method"])
        line = {"forma_pagamento": code, "valor_pagamento": _brl(payment["amount_cents"])}
        if code in CARD_TPAG:
            line["tipo_integracao"] = 2
        if code == "99":
            line["descricao_pagamento"] = (request.payment_names or {}).get(payment.get("payment_method_id")) or "Outros"
        payments.append(line)
    totals = doc["totals"]
    body = {"natureza_operacao": "VENDA", "data_emissao": local_iso(request.issued_at, issuer["address_state"]),
            "presenca_comprador": 1, "modalidade_frete": 9, "local_destino": 1, "consumidor_final": 1,
            "cnpj_emitente": cnpj_value(issuer.get("document")), "valor_produtos": _brl(totals["products_cents"]),
            "valor_desconto": _brl(totals["discount_cents"]), "valor_total": _brl(totals["total_cents"]),
            "items": items, "formas_pagamento": payments}
    if request.change_cents:
        body["valor_troco"] = _brl(request.change_cents)
    if request.series:
        body["serie"] = request.series
    if request.number:
        body["numero"] = request.number
    customer = request.customer or {}
    document = cnpj_value(customer.get("document"))
    if len(document) == 11:
        body["cpf_destinatario"] = document
    elif len(document) == 14:
        body["cnpj_destinatario"] = document
    if document and customer.get("name"):
        body["nome_destinatario"] = customer["name"][:60]
    info = approx_text(totals, doc["items"])
    if info:
        body["informacoes_adicionais_contribuinte"] = info
    return body


def _focus_key(value):
    text = str(value or "")
    return text[3:] if text.upper().startswith("NFE") else text


class FocusBackend:
    name = "focusnfe"

    def __init__(self, client):
        self.client = client

    def status(self):
        try:
            self.client.get("pdv2-teste-de-conexao")
        except ProviderError as exc:
            if exc.status == 404:
                return {"online": True, "message": "Credencial aceita pela Focus NFe", "code": "404"}
            if exc.status in (401, 403):
                return {"online": False, "message": "A Focus NFe recusou a credencial (token)", "code": str(exc.status)}
            return {"online": False, "message": str(exc), "code": str(exc.status or "")}
        return {"online": True, "message": "Credencial aceita pela Focus NFe", "code": "200"}

    def _result(self, response):
        state = self.client.normalized_status(response)
        key = _focus_key(response.get("chave_nfe"))
        result = IssueResult(status="pending", key=key, code=str(response.get("status_sefaz") or ""),
                             message=str(response.get("mensagem_sefaz") or ""), protocol=str(response.get("numero_protocolo") or ""),
                             qr_url=str(response.get("qrcode_url") or ""), url_key=str(response.get("url_consulta_nfce") or ""),
                             xml_path=str(response.get("caminho_xml_nota_fiscal") or ""), raw=response)
        try:
            result.number = int(response["numero"]) if response.get("numero") else None
            result.series = int(response["serie"]) if response.get("serie") else None
        except (TypeError, ValueError):
            pass
        if state == "authorized" and len(key) == 44 and result.protocol:
            result.status = "authorized"
            result.authorized_at = str(response.get("data_autorizacao") or "")
        elif state == "authorized" and response.get("contingencia_offline") and not response.get("contingencia_offline_efetivada"):
            result.status = "contingency"
        elif state == "cancelled":
            result.status = "cancelled"
        elif state in ("rejected", "denied"):
            result.status = "rejected"
            result.message = ("Uso denegado: " if state == "denied" else "") + messages.status_message(
                result.code, result.message)
        return result

    def issue(self, request):
        try:
            response = self.client.submit(request.reference, focus_payload(request))
        except ProviderError as exc:
            if exc.outcome_unknown:
                return IssueResult(status="pending", message=str(exc))
            raise
        return self._result(response)

    def consult(self, ref):
        return self._result(self.client.get(ref.reference))

    def cancel(self, ref, reason, when=None, sequence=1):
        try:
            self.client.cancel(ref.reference, reason)
        except ProviderError as exc:
            if not exc.outcome_unknown:
                return EventResult(status="rejected", code=str(exc.status or ""), message=str(exc))
        response = self.client.get(ref.reference)
        if self.client.normalized_status(response) != "cancelled":
            return EventResult(status="pending", message="Cancelamento ainda não confirmado pela SEFAZ", raw=response)
        proc = None
        path = response.get("caminho_xml_cancelamento")
        if path:
            proc = self.client.download_event_xml(path, _focus_key(response.get("chave_nfe")))
        return EventResult(status="registered", protocol=str(response.get("protocolo_cancelamento") or ""),
                           message=str(response.get("mensagem_sefaz") or ""), proc_xml=proc, raw=response)

    def inutilize(self, series, start, end, reason, year, cnpj):
        response = self.client.inutilize(cnpj, series, start, end, reason)
        state = str(response.get("status") or "")
        if state == "autorizado":
            proc = None
            if response.get("caminho_xml"):
                proc = self.client.download_inutilization_xml(response["caminho_xml"])
            return EventResult(status="registered", protocol=str(response.get("protocolo_sefaz") or ""),
                               code=str(response.get("status_sefaz") or ""), message=str(response.get("mensagem_sefaz") or ""),
                               proc_xml=proc, raw=response)
        return EventResult(status="rejected", code=str(response.get("status_sefaz") or ""),
                           message=messages.status_message(response.get("status_sefaz"), response.get("mensagem_sefaz", "")),
                           raw=response)


__all__ = ["IssueRequest", "IssueResult", "EventResult", "Ref", "SefazBackend", "FocusBackend", "focus_payload",
           "NfceError", "TransportError", "E", "NFE_NS"]
