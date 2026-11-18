"""NFC-e na nuvem: configuração, numeração, emissão, contingência, cancelamento e inutilização.

Ponte entre os modelos (FiscalDocument, FiscalPayload, FiscalEvent, FiscalInutilization,
NfceReservation) e os backends (``core.nfce.backends``). Chamado pelas rotas do painel e do caixa
e pelos jobs de ``operations.services``. Ver docs/NFCE.md.
"""
import base64
import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from integrations.base import ProviderError
from shared.fiscal.validators import cnpj_value
from shared.nfce import keys
from shared.nfce.builder import CONTINGENCY_REASON, NfceError, approx_text
from shared.nfce.payments import default_print_rule
from shared.nfce.webservices import cancel_minutes
from shared.nfce.xml import XMLError, from_etree, parse, tobytes
from shared.secrets import decrypt_secret

from ..api_common import APIError
from ..fiscal import simulate, tech_responsible
from ..models import (FiscalConfig, FiscalDocument, FiscalEvent, FiscalInutilization, NfceReservation, PaymentMethod,
                      Sale, Tenant)
from .backends import FocusBackend, IssueRequest, IssueResult, Ref, SefazBackend
from .certificate import load_pfx
from .transport import SoapTransport, TransportError

logger = logging.getLogger(__name__)

ENVIRONMENTS = ("homologation", "production")
RESERVATION_SIZE = 10
RESERVATION_MAX_DAYS = 20
# Blocks with fewer days left than this are not handed out anymore (the register renews before).
RESERVATION_RENEW_DAYS = 3
# A new block is opened when fewer free numbers than this remain.
RESERVATION_LOW = 3
CONTINGENCY_ALERT_HOURS = 20
TERMINAL_TIMEOUT = (3, 5)

# Test hook: callable(certificate, timeout) -> transport with .call(url, service, body).
TRANSPORT_FACTORY = None
# Test hook: callable(token, environment) -> FocusNFe-like client.
FOCUS_FACTORY = None


class NotConfigured(APIError):
    def __init__(self, message):
        super().__init__(message, 409, code="fiscal_not_configured")


# ------------------------------------------------------------------ configuration

def config_for(tenant):
    """FiscalConfig of the company, created on first write (panel PATCH, certificate upload)."""
    config, _ = FiscalConfig.objects.get_or_create(tenant=tenant)
    return config


def peek_config(tenant):
    """FiscalConfig for reading: the saved one or an unsaved default. Never creates a row."""
    return FiscalConfig.objects.filter(tenant=tenant).first() or FiscalConfig(tenant=tenant)


def enabled(tenant):
    return tenant.fiscal_environment in ENVIRONMENTS


def live_allowed(environment):
    return environment != "production" or os.environ.get("PDV_ALLOW_LIVE_FISCAL") == "1"


def _focus_account(tenant):
    from operations.models import IntegrationAccount
    return IntegrationAccount.objects.filter(provider="focusnfe", tenant=tenant, enabled=True).first()


def readiness(tenant, config=None):
    """(ready, reason): may this company emit NFC-e now? Checked before any number is spent.

    Ready = fiscal environment on + production allowed by the platform + a saved configuration
    with its credential (Focus NFe token, or A1 certificate of the company's CNPJ) + company
    fiscal data complete. Merely opening the panel never turns emission on.
    """
    environment = tenant.fiscal_environment
    if environment not in ENVIRONMENTS:
        return False, "A emissão de NFC-e está desligada (ambiente: sem nota fiscal)"
    if not live_allowed(environment):
        return False, "Emissão em produção ainda não habilitada nesta plataforma (PDV_ALLOW_LIVE_FISCAL)"
    config = config or peek_config(tenant)
    if config._state.adding:
        return False, "Configure a nota fiscal em Minha empresa > Nota fiscal"
    if config.mode == "focusnfe":
        if not _focus_account(tenant):
            return False, "Configure o token da Focus NFe em Minha empresa > Nota fiscal"
    else:
        if not config.certificate_encrypted:
            return False, "Envie o certificado digital A1 em Minha empresa > Nota fiscal"
        if cnpj_value(config.certificate_cnpj) != cnpj_value(tenant.document):
            return False, "O CNPJ do certificado não é o mesmo da empresa"
    from ..fiscal import company_status
    if not company_status(tenant)["ready"]:
        return False, "Dados da empresa incompletos para a NFC-e"
    return True, ""


def ensure_ready(tenant, config=None):
    ready, reason = readiness(tenant, config)
    if ready:
        return
    if tenant.fiscal_environment in ENVIRONMENTS and not live_allowed(tenant.fiscal_environment):
        raise APIError(reason, 409, code="fiscal_live_disabled")
    raise NotConfigured(reason)


def csc_for(config, environment):
    if environment == "production":
        raw, csc_id = config.csc_production_encrypted, config.csc_production_id
    else:
        raw, csc_id = config.csc_homologation_encrypted, config.csc_homologation_id
    if not raw or not csc_id:
        return "", ""
    return csc_id, decrypt_secret(raw)


def certificate_for(config):
    if not config.certificate_encrypted:
        raise NotConfigured("Envie o certificado digital A1 em Minha empresa > Nota fiscal")
    data = base64.b64decode(decrypt_secret(config.certificate_encrypted))
    password = decrypt_secret(config.certificate_password_encrypted) if config.certificate_password_encrypted else ""
    return load_pfx(data, password)


def _focus_client(tenant, timeout=None, environment=None):
    """Focus NFe client. ``environment`` = the document's own environment (legacy notes keep theirs
    even after the company changed or switched off its fiscal environment)."""
    from integrations.fiscal import FocusNFe
    row = _focus_account(tenant)
    if not row:
        raise NotConfigured("Configure o token da Focus NFe em Minha empresa > Nota fiscal")
    token = decrypt_secret(row.token_encrypted)
    if environment not in ENVIRONMENTS:
        environment = tenant.fiscal_environment if tenant.fiscal_environment in ENVIRONMENTS else row.environment
    client = FOCUS_FACTORY(token, environment) if FOCUS_FACTORY else FocusNFe(token, environment)
    if timeout:
        client.timeout = timeout
    return client


def document_environment(document):
    """Environment of an existing document; legacy Focus notes without one use the Focus account's."""
    if document.environment in ENVIRONMENTS:
        return document.environment
    account = _focus_account(document.tenant) if document.provider == "focusnfe" else None
    if account and account.environment in ENVIRONMENTS:
        return account.environment
    return document.tenant.fiscal_environment


def backend_for(tenant, config=None, environment=None, timeout=None, provider=None):
    """Backend for ``environment`` (default: the company's). ``provider`` (the document's) wins over
    the current mode: a note issued through Focus is consulted/cancelled through Focus."""
    config = config or peek_config(tenant)
    environment = environment or tenant.fiscal_environment
    if environment not in ENVIRONMENTS:
        raise NotConfigured("A emissão de NFC-e está desligada (ambiente: sem nota fiscal)")
    if not live_allowed(environment):
        raise APIError("Emissão em produção ainda não habilitada nesta plataforma (PDV_ALLOW_LIVE_FISCAL)", 409,
                       code="fiscal_live_disabled")
    if (provider or config.mode) == "focusnfe":
        return FocusBackend(_focus_client(tenant, timeout, environment))
    certificate = certificate_for(config)
    if cnpj_value(certificate.cnpj) != cnpj_value(tenant.document):
        raise NotConfigured("O CNPJ do certificado não é o mesmo da empresa")
    timeout = timeout or (4, 25)
    transport = TRANSPORT_FACTORY(certificate, timeout) if TRANSPORT_FACTORY else SoapTransport(certificate, timeout)
    return SefazBackend(certificate, transport, tenant.address_state, environment)


def document_backend(document, timeout=None):
    """Backend of an existing document: its own environment and provider (not the company's current ones)."""
    return backend_for(document.tenant, environment=document_environment(document), timeout=timeout,
                       provider="focusnfe" if document.provider == "focusnfe" else "sefaz")


def issuer_data(tenant):
    return {"document": tenant.document, "name": tenant.name, "trade_name": tenant.trade_name,
            "state_registration": tenant.state_registration, "address_line": tenant.address_line,
            "address_number": tenant.address_number, "address_complement": tenant.address_complement,
            "address_neighborhood": tenant.address_neighborhood, "address_city": tenant.address_city,
            "address_state": tenant.address_state, "address_zip": tenant.address_zip,
            "city_ibge_code": tenant.city_ibge_code, "crt": tenant.crt}


def print_rules(tenant):
    """{kind or payment_method_id: always|ask|never} sent to the register."""
    rules = {}
    for row in PaymentMethod.objects.filter(tenant=tenant):
        rule = row.print_rule or default_print_rule(row.kind)
        rules[str(row.id) if row.kind == "other" else row.kind] = rule
    return rules


def terminal_settings(tenant):
    """Non-secret NFC-e settings for the terminal bootstrap."""
    config = FiscalConfig.objects.filter(tenant=tenant).first()
    environment = tenant.fiscal_environment
    on = config is not None and readiness(tenant, config)[0]
    return {"enabled": bool(on), "environment": environment, "mode": config.mode if config else "",
            "contingency": bool(on and config.mode == "sefaz" and config.terminal_contingency
                                and csc_for(config, environment)[0]),
            "wait_ms": 8000, "cancel_minutes": cancel_minutes(tenant.address_state),
            "print_rules": print_rules(tenant)}


# ------------------------------------------------------------------ sale -> engine

def sale_lines(sale):
    return [(item.product, item.quantity_milli, item.unit_price_cents,
             item.promo_discount_cents + item.discount_cents)
            for item in sale.items.select_related("product", "product__tax_profile").order_by("line_number")]


def sale_document(sale):
    doc = simulate(sale.tenant, sale_lines(sale), model=65)
    # The register already split the discount; the engine must land on the same total.
    if doc["totals"]["total_cents"] != sale.total_cents:
        raise NfceError("Total calculado para a nota não confere com a venda", [
            f"Nota {doc['totals']['total_cents']} centavos x venda {sale.total_cents} centavos"])
    return doc


def sale_payments(sale):
    rows = list(sale.payments.select_related("payment_method").order_by("line_number"))
    payments = [{"method": row.method, "amount_cents": row.amount_cents,
                 "payment_method_id": str(row.payment_method_id) if row.payment_method_id else None} for row in rows]
    names = {str(row.payment_method_id): row.payment_method.name for row in rows if row.payment_method_id}
    return payments, names


def _request(document, sale, params):
    tenant = sale.tenant
    config = peek_config(tenant)
    csc_id, csc = csc_for(config, document.environment)
    payments, names = sale_payments(sale)
    issued = datetime.fromisoformat(params["issued_at"])
    contingency = None
    if params.get("emission_type") == 9:
        contingency = {"at": datetime.fromisoformat(params["contingency_at"]), "reason": params.get("contingency_reason")}
    customer = {"document": sale.customer_document_snapshot, "name": sale.customer_name_snapshot} \
        if sale.customer_document_snapshot else None
    return IssueRequest(issuer=issuer_data(tenant), doc=sale_document(sale), environment=document.environment,
                        reference=document.external_id, payments=payments, payment_names=names,
                        change_cents=sale.change_cents, issued_at=issued, series=document.series,
                        number=document.number, cnf=params.get("cnf"), emission_type=params.get("emission_type", 1),
                        contingency=contingency, customer=customer, tech=tech_responsible(),
                        app_version="PDV2 " + str(getattr(settings, "PDV_VERSION", ""))[:14],
                        qr_version=params.get("qr_version") or config.qr_version, csc_id=csc_id, csc=csc)


def validate_sale(sale):
    """Everything that can be checked before spending a number; raises NfceError."""
    tenant = sale.tenant
    from ..fiscal import company_status
    status = company_status(tenant)
    if not status["ready"]:
        raise NfceError("Dados da empresa incompletos para a NFC-e", status["missing"])
    doc = sale_document(sale)
    if not doc["ready"]:
        raise NfceError("Há produtos sem dados fiscais completos para a nota",
                        [f"{item['description']}: {issue['message']}" for item in doc["items"]
                         for issue in item.get("issues", []) if issue.get("severity") == "error"])
    return doc


# ------------------------------------------------------------------ numbering

def _take_number(tenant_id):
    """Next NFC-e number of the company (row lock); never reused."""
    tenant = Tenant.objects.select_for_update().get(pk=tenant_id)
    number = tenant.nfce_next_number
    tenant.nfce_next_number = number + 1
    tenant.save(update_fields=["nfce_next_number"])
    return tenant.nfce_series, number


def current_document(sale):
    return (FiscalDocument.objects.filter(sale=sale, replaced_by__isnull=True).order_by("-created_at").first())


def _payload_model():
    from operations.models import FiscalPayload
    return FiscalPayload


def _params(document):
    row = _payload_model().objects.filter(document=document).first()
    return dict(row.request_payload or {}) if row else {}


def _save_params(document, params):
    FiscalPayload = _payload_model()
    FiscalPayload.objects.update_or_create(document=document, defaults={"request_payload": params})


def _sync_sale(document):
    if document.replaced_by_id:
        return
    Sale.objects.filter(pk=document.sale_id).update(fiscal_status=document.status)


def _store_xml(document, result):
    FiscalPayload = _payload_model()
    updates = {}
    if result.nfe_xml:
        updates["nfe_xml"] = result.nfe_xml
    if result.proc_xml:
        updates["xml_content"] = result.proc_xml
        updates["xml_sha256"] = hashlib.sha256(result.proc_xml).hexdigest()
    if result.raw and document.provider == "focusnfe":
        updates["provider_response"] = {k: v for k, v in result.raw.items() if isinstance(v, (str, int, bool, type(None)))}
    if updates:
        FiscalPayload.objects.filter(document=document).update(**updates)


def _parse_time(value):
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt_timezone.utc)


def apply_result(document, result):
    """Write a backend answer on the document (and the sale's fiscal_status)."""
    with transaction.atomic():
        document = FiscalDocument.objects.select_for_update().get(pk=document.pk)
        if result.key:
            document.access_key = result.key
        if result.number and not document.number:
            document.number = result.number
        if result.series is not None and document.series is None:
            document.series = result.series
        if result.qr_url:
            document.qr_url = result.qr_url
        if result.url_key:
            document.url_key = result.url_key[:300]
        if result.xml_path:
            document.xml_url = result.xml_path[:200]
        document.status_code = (result.code or "")[:10]
        if result.status == "authorized":
            document.status = "authorized"
            document.authorization = result.protocol
            document.authorized_at = _parse_time(result.authorized_at) or timezone.now()
            document.error_message = ""
        elif result.status == "cancelled":
            document.status = "cancelled"
            document.cancelled_at = document.cancelled_at or timezone.now()
        elif result.status == "rejected":
            document.status = "rejected"
            document.error_message = result.message[:1000]
        elif result.status == "contingency":
            document.status = "contingency"
            document.emission_type = 9
            document.error_message = ""
        else:
            document.error_message = result.message[:1000]
        document.save()
        _store_xml(document, result)
        _sync_sale(document)
    if document.status == "authorized" and document.provider == "focusnfe" and document.xml_url:
        _enqueue("fiscal.archive", document.tenant, "fiscal.archive:" + str(document.pk), {"document_id": str(document.pk)})
    if document.status == "authorized":
        params = _params(document)
        if params.get("cancel_requested"):
            request_cancel(document, params["cancel_requested"], automatic=True)
    return document


def _enqueue(kind, tenant, key, payload, delay=0):
    from operations.services import JobConflict, enqueue
    try:
        return enqueue(kind, tenant, key, payload, delay=delay)
    except JobConflict:
        return None


# ------------------------------------------------------------------ issuing

def create_document(sale, terminal=None):
    """Idempotent by sale: the current document, or a new pending one with its number."""
    tenant = sale.tenant
    if not enabled(tenant):
        raise NotConfigured("A emissão de NFC-e está desligada (ambiente: sem nota fiscal)")
    config = peek_config(tenant)
    with transaction.atomic():
        Sale.objects.select_for_update().get(pk=sale.pk)
        existing = current_document(sale)
        if existing:
            return existing, False
        if sale.status != "completed":
            raise APIError("Venda cancelada não emite nota", 409)
        # Production allowed, credential in place and company data complete BEFORE spending a number.
        ensure_ready(tenant, config)
        provider = "focusnfe" if config.mode == "focusnfe" else "sefaz"
        series, number = _take_number(tenant.pk)
        document = FiscalDocument.objects.create(
            tenant=tenant, shop=sale.shop, sale=sale, terminal=terminal, provider=provider,
            external_id="pdv2-" + str(sale.pk), environment=tenant.fiscal_environment, series=series,
            number=number, status="pending", issued_at=timezone.now())
        params = {"cnf": keys.random_cnf(number), "issued_at": document.issued_at.isoformat(), "emission_type": 1,
                  "qr_version": config.qr_version, "sent": False}
        _save_params(document, params)
        _sync_sale(document)
    return document, True


def _danfe_extra(document, doc):
    totals = doc["totals"]
    items = doc["items"]
    return {"taxes_cents": totals["approx_tax_cents"],
            "taxes_federal_cents": sum(i["approx_tax"].get("federal_cents") or 0 for i in items) or None,
            "taxes_state_cents": sum(i["approx_tax"].get("state_cents") or 0 for i in items) or None,
            "taxes_municipal_cents": sum(i["approx_tax"].get("municipal_cents") or 0 for i in items) or None,
            "taxes_source": "IBPT" if any(i["approx_tax"]["source"] == "ibpt" for i in items) else
            ("Estimativa" if totals["approx_tax_cents"] else ""),
            "infcpl": approx_text(totals, items)}


def is_legacy(document, params=None):
    """Focus NFe note created by the release before NFC-e (core 0041): its request_payload is the
    Focus JSON itself, without the cNF/dhEmi this module stores, and Focus numbers it."""
    params = _params(document) if params is None else params
    return document.provider == "focusnfe" and "cnf" not in params and "issued_at" not in params


def _send_legacy(document, params, timeout=None):
    """Old pending Focus note: ask Focus first (it may already have it); submit the stored payload
    only when Focus does not know the reference, exactly as the previous release did."""
    backend = document_backend(document, timeout=timeout)
    try:
        result = backend.consult(Ref(reference=document.external_id, key=document.access_key,
                                     environment=document_environment(document)))
    except ProviderError as exc:
        if exc.status != 404:
            raise
        payload = {k: v for k, v in params.items() if k not in ("sent", "danfe", "cancel_requested")}
        try:
            result = backend._result(backend.client.submit(document.external_id, payload))
        except ProviderError as error:
            if not error.outcome_unknown:
                return apply_result(document, IssueResult(status="rejected", message=str(error)))
            result = IssueResult(status="pending", message=str(error))
    document = apply_result(document, result)
    if document.status == "pending":
        _enqueue("fiscal.poll", document.tenant, "fiscal.poll:" + str(document.pk), {"document_id": str(document.pk)}, 15)
    return document


def send(document, timeout=None):
    """Transmit the pending document now. Network errors are handled here (contingency/replacement)."""
    document = FiscalDocument.objects.select_related("sale", "tenant", "sale__tenant").get(pk=document.pk)
    if document.status in ("authorized", "cancelled"):
        return document
    if document.status == "contingency":
        return transmit(document)
    sale = document.sale
    params = _params(document)
    if is_legacy(document, params):
        return _send_legacy(document, params, timeout)
    if params.get("sent") and document.access_key:
        return poll(document, timeout=timeout)
    try:
        backend = document_backend(document, timeout=timeout)
        request = _request(document, sale, params)
    except NfceError as exc:
        return apply_result(document, IssueResult(status="rejected", message=_issues_text(exc)))
    params["danfe"] = _danfe_extra(document, request.doc)
    params["sent"] = True
    _save_params(document, params)
    try:
        result = backend.issue(request)
    except NfceError as exc:
        params["sent"] = False
        _save_params(document, params)
        return apply_result(document, IssueResult(status="rejected", message=_issues_text(exc)))
    except TransportError as exc:
        if exc.unreachable:
            # Nothing reached the SEFAZ: the same number goes out in offline contingency (tpEmis 9).
            return _cloud_contingency(document, backend, request, params)
        # The request may have arrived: never reuse this number for a contingency note.
        result = IssueResult(status="pending", message=str(exc))
    except ProviderError as exc:
        params["sent"] = exc.outcome_unknown
        _save_params(document, params)
        if exc.outcome_unknown:
            _enqueue("fiscal.poll", document.tenant, "fiscal.poll:" + str(document.pk), {"document_id": str(document.pk)}, 15)
            return document
        return apply_result(document, IssueResult(status="rejected", message=str(exc)))
    document = apply_result(document, result)
    if result.status == "pending":
        if document.provider == "sefaz" and timeout:
            # Answer lost while the customer waits: issue a contingency note with a NEW number.
            return _replace_with_contingency(document, backend, request)
        _enqueue("fiscal.poll", document.tenant, "fiscal.poll:" + str(document.pk), {"document_id": str(document.pk)}, 15)
    return document


def _issues_text(exc):
    issues = getattr(exc, "issues", None) or []
    return (str(exc) + (": " + "; ".join(issues[:5]) if issues else ""))[:1000]


def _cloud_contingency(document, backend, request, params):
    if not hasattr(backend, "issue_offline"):
        _enqueue("fiscal.submit", document.tenant, "fiscal.retry:" + str(document.pk), {"document_id": str(document.pk)}, 60)
        return document
    now = timezone.now()
    params.update(emission_type=9, contingency_at=now.isoformat(), contingency_reason=CONTINGENCY_REASON)
    request.emission_type = 9
    request.contingency = {"at": now, "reason": CONTINGENCY_REASON}
    params["sent"] = False
    _save_params(document, params)
    result = backend.issue_offline(request)
    FiscalDocument.objects.filter(pk=document.pk).update(emission_type=9)
    document = apply_result(document, result)
    _enqueue("fiscal.transmit", document.tenant, "fiscal.transmit:" + str(document.pk),
             {"document_id": str(document.pk)}, 60)
    return document


def _replace_with_contingency(old, backend, request):
    with transaction.atomic():
        series, number = _take_number(old.tenant_id)
        new = FiscalDocument.objects.create(
            tenant=old.tenant, shop=old.shop, sale=old.sale, terminal=old.terminal, provider=old.provider,
            external_id=old.external_id + "-c", environment=old.environment, series=series, number=number,
            status="pending", issued_at=timezone.now())
        FiscalDocument.objects.filter(pk=old.pk).update(replaced_by=new,
                                                        error_message="Sem resposta da SEFAZ; substituída por contingência")
        params = {"cnf": keys.random_cnf(number), "issued_at": new.issued_at.isoformat(), "emission_type": 9,
                  "qr_version": request.qr_version, "sent": False}
        _save_params(new, params)
    request.series, request.number, request.cnf = series, number, params["cnf"]
    request.issued_at = new.issued_at
    params["danfe"] = _params(old).get("danfe")
    _enqueue("fiscal.reconcile_replaced", old.tenant, "fiscal.reconcile_replaced:" + str(old.pk),
             {"document_id": str(old.pk)}, 120)
    return _cloud_contingency(new, backend, request, params)


def poll(document, timeout=None):
    params = _params(document)
    if is_legacy(document, params):
        if document.status != "pending":
            return document
        return _send_legacy(document, params, timeout)
    if not document.access_key and document.provider == "sefaz":
        return document
    backend = document_backend(document, timeout=timeout)
    nfe_xml = _payload_model().objects.filter(document=document).values_list("nfe_xml", flat=True).first()
    result = backend.consult(Ref(reference=document.external_id, key=document.access_key, protocol=document.authorization,
                                 nfe_xml=bytes(nfe_xml) if nfe_xml else None, environment=document.environment))
    if result.status == "not_found":
        if params.get("emission_type") == 9 or document.emission_type == 9:
            return transmit(document)
        params["sent"] = False
        _save_params(document, params)
        return send(document, timeout)
    return apply_result(document, result)


def transmit(document):
    """Contingency note (signed in the cloud) sent to the SEFAZ when it is back."""
    FiscalPayload = _payload_model()
    row = FiscalPayload.objects.get(document=document)
    backend = document_backend(document)
    if not row.nfe_xml:
        raise ValueError("Nota de contingência sem XML assinado")
    result = backend.transmit(bytes(row.nfe_xml))
    if result.status == "pending":
        from operations.services import StillPending
        raise StillPending("Aguardando a SEFAZ autorizar a nota de contingência")
    params = _params(document)
    params["sent"] = True
    _save_params(document, params)
    return apply_result(document, result)


def issue_for_sale(sale, terminal=None, timeout=None):
    """Create (idempotently) and send the NFC-e of a synced sale."""
    try:
        validate_sale(sale)
    except NfceError as exc:
        existing = current_document(sale)
        if existing and existing.status in ("authorized", "contingency", "cancelled"):
            return existing
        raise APIError(_issues_text(exc), 422, code="fiscal_data_incomplete",
                       extra={"issues": list(getattr(exc, "issues", []))[:20]})
    document, _created = create_document(sale, terminal)
    if document.status in ("pending", "rejected") and not _params(document).get("sent"):
        if document.status == "rejected":
            return document
        return send(document, timeout)
    if document.status == "pending":
        try:
            return poll(document, timeout)
        except ProviderError:
            return document
    return document


def retry(document):
    """Panel "Corrigir e reenviar": same number (never authorized), fresh data from the catalog."""
    if document.status != "rejected":
        raise APIError("Somente notas rejeitadas podem ser reenviadas", 409)
    if is_legacy(document):
        raise APIError("Nota da versão anterior (numerada pela Focus NFe): corrija o cadastro e reenvie pelo painel "
                       "da Focus NFe, ou fale com o suporte.", 409, code="fiscal_legacy_document")
    validate_sale(document.sale)
    params = _params(document)
    if params.get("emission_type", 1) == 1:
        params["issued_at"] = timezone.now().isoformat()
    params["sent"] = False
    _save_params(document, params)
    FiscalDocument.objects.filter(pk=document.pk).update(
        status="contingency" if params.get("emission_type") == 9 else "pending", error_message="")
    document.refresh_from_db()
    if document.status == "contingency":
        rebuild_contingency(document)
        _enqueue("fiscal.transmit", document.tenant, f"fiscal.transmit:{document.pk}:{timezone.now().timestamp()}",
                 {"document_id": str(document.pk)})
    else:
        _enqueue("fiscal.submit", document.tenant, f"fiscal.submit:{document.pk}:{timezone.now().timestamp()}",
                 {"document_id": str(document.pk)})
    _sync_sale(document)
    return document


def rebuild_contingency(document):
    """Rebuild a rejected contingency note with the same key data (number, cNF, dhEmi, dhCont)."""
    params = _params(document)
    backend = document_backend(document)
    request = _request(document, document.sale, params)
    request.emission_type = 9
    result = backend.issue_offline(request)
    _store_xml(document, result)
    if result.key != document.access_key:
        FiscalDocument.objects.filter(pk=document.pk).update(access_key=result.key, qr_url=result.qr_url)


# ------------------------------------------------------------------ cancellation

def cancel_deadline(document):
    start = document.authorized_at or document.issued_at
    if not start:
        return None
    return start + timedelta(minutes=cancel_minutes(document.tenant.address_state))


def request_cancel(document, reason, actor=None, automatic=False):
    reason = " ".join(str(reason or "").split())
    if len(reason) < 15 or len(reason) > 255:
        raise APIError("Justificativa deve ter de 15 a 255 caracteres")
    if document.status in ("pending", "contingency"):
        # Not authorized yet: cancel as soon as the SEFAZ authorizes it.
        params = _params(document)
        params["cancel_requested"] = reason
        _save_params(document, params)
        return None
    if document.status != "authorized":
        raise APIError("Somente notas autorizadas podem ser canceladas", 409)
    deadline = cancel_deadline(document)
    if deadline and timezone.now() > deadline and not automatic:
        raise APIError(f"Prazo de cancelamento encerrado ({cancel_minutes(document.tenant.address_state)} minutos após a "
                       "autorização). Fale com o contador sobre o cancelamento extemporâneo.", 409,
                       code="fiscal_cancel_expired")
    with transaction.atomic():
        event = FiscalEvent.objects.filter(document=document, kind="cancel", status="pending").first()
        if not event:
            sequence = FiscalEvent.objects.filter(document=document, kind="cancel").count() + 1
            event = FiscalEvent.objects.create(tenant=document.tenant, document=document, reason=reason, sequence=sequence)
        job = _enqueue("fiscal.cancel", document.tenant, "fiscal.cancel:" + str(event.pk),
                       {"document_id": str(document.pk), "event_id": str(event.pk), "reason": reason})
    return job


def run_cancel(document, event):
    backend = document_backend(document)
    result = backend.cancel(Ref(reference=document.external_id, key=document.access_key, protocol=document.authorization,
                                environment=document.environment), event.reason, sequence=event.sequence)
    with transaction.atomic():
        event = FiscalEvent.objects.select_for_update().get(pk=event.pk)
        event.status_code = (result.code or "")[:10]
        if result.status == "registered":
            event.status = "registered"
            event.protocol = result.protocol[:60]
            event.registered_at = timezone.now()
            if result.proc_xml:
                event.xml_content = result.proc_xml
                event.xml_sha256 = hashlib.sha256(result.proc_xml).hexdigest()
            event.save()
            FiscalDocument.objects.filter(pk=document.pk).update(status="cancelled", cancelled_at=timezone.now())
            document.refresh_from_db()
            _sync_sale(document)
        elif result.status == "rejected":
            event.status = "rejected"
            event.error_message = result.message[:1000]
            event.save()
        else:
            event.save()
    if result.status == "pending":
        from operations.services import StillPending
        raise StillPending("Cancelamento ainda não confirmado; consultar documento")
    if result.status == "rejected":
        raise ValueError("Cancelamento recusado: " + result.message[:300])
    return result


# ------------------------------------------------------------------ inutilização

def request_inutilization(tenant, environment, series, start, end, reason):
    reason = " ".join(str(reason or "").split())
    if len(reason) < 15:
        raise APIError("Justificativa deve ter pelo menos 15 caracteres")
    if not 1 <= int(start) <= int(end) <= 999999999 or int(end) - int(start) > 9999:
        raise APIError("Faixa de numeração inválida")
    used = FiscalDocument.objects.filter(tenant=tenant, environment=environment, series=series,
                                         number__gte=start, number__lte=end).exclude(status="rejected", access_key="")
    if used.filter(status__in=("authorized", "contingency", "cancelled", "pending")).exists():
        raise APIError("A faixa inclui números de notas emitidas", 409)
    row = FiscalInutilization.objects.create(tenant=tenant, environment=environment, series=series, start=start,
                                             end=end, reason=reason)
    _enqueue("fiscal.inutilize", tenant, "fiscal.inutilize:" + str(row.pk), {"inutilization_id": str(row.pk)})
    return row


def run_inutilization(row):
    tenant = row.tenant
    backend = backend_for(tenant, environment=row.environment)
    result = backend.inutilize(row.series, row.start, row.end, row.reason, timezone.now().year, cnpj_value(tenant.document))
    row.status_code = (result.code or "")[:10]
    if result.status == "registered":
        row.status, row.protocol, row.done_at = "done", result.protocol[:60], timezone.now()
        if result.proc_xml:
            row.xml_content = result.proc_xml
            row.xml_sha256 = hashlib.sha256(result.proc_xml).hexdigest()
    else:
        row.status, row.error_message = "rejected", result.message[:1000]
    row.save()
    if row.status == "rejected":
        raise ValueError("Inutilização recusada: " + row.error_message[:300])
    return row


def reconcile_replaced(document):
    """Old attempt replaced by contingency: cancel it if the SEFAZ authorized it, else inutilize the number."""
    backend = document_backend(document)
    nfe_xml = _payload_model().objects.filter(document=document).values_list("nfe_xml", flat=True).first()
    result = backend.consult(Ref(reference=document.external_id, key=document.access_key,
                                 nfe_xml=bytes(nfe_xml) if nfe_xml else None, environment=document.environment))
    if result.status == "authorized":
        document = apply_result(document, result)
        return request_cancel(document, "Nota substituida por emissao em contingencia por falha de comunicacao",
                              automatic=True)
    if result.status == "not_found":
        FiscalDocument.objects.filter(pk=document.pk).update(status="rejected",
                                                             error_message="Número substituído e inutilizado")
        return request_inutilization(document.tenant, document.environment, document.series, document.number,
                                     document.number, "Numero nao utilizado: falha de comunicacao com a SEFAZ")
    if result.status == "pending":
        from operations.services import StillPending
        raise StillPending("Aguardando a SEFAZ responder a consulta")
    return apply_result(document, result)


# ------------------------------------------------------------------ terminal contingency

def _client_numbers(values):
    """Numbers the register says it already used offline (not uploaded yet); bounded and typed."""
    if not isinstance(values, list):
        return set()
    return {n for n in values[:2000] if type(n) is int and 0 < n <= 999999999}


def reserve_numbers(terminal, client_used=None):
    """Contingency numbers for the register: every still-fresh open block of this terminal.

    A new block of RESERVATION_SIZE numbers is opened only when fewer than RESERVATION_LOW free
    numbers remain (counting the ones the register used offline and did not upload yet) or the
    open blocks are close to expiring (RESERVATION_RENEW_DAYS). ``created`` says whether a block
    was opened (only then the call is audited). Blocks near expiry are no longer handed out but
    still accept the uploads of numbers already used, until the sweep releases them.
    """
    tenant = terminal.tenant
    config = peek_config(tenant)
    environment = tenant.fiscal_environment
    if not (config.mode == "sefaz" and config.terminal_contingency and readiness(tenant, config)[0]):
        raise NotConfigured("Contingência no caixa desligada para esta empresa")
    csc_id, csc = csc_for(config, environment)
    if not csc_id:
        raise NotConfigured("Contingência no caixa exige o CSC do ambiente (QR Code versão 2)")
    used_here = _client_numbers(client_used)
    now = timezone.now()
    stale = now - timedelta(days=RESERVATION_MAX_DAYS - RESERVATION_RENEW_DAYS)
    created = False
    with transaction.atomic():
        rows = (NfceReservation.objects.select_for_update()
                .filter(terminal=terminal, environment=environment, released_at__isnull=True).order_by("created_at"))
        usable = []
        for row in rows:
            if row.series != tenant.nfce_series or row.created_at < now - timedelta(days=RESERVATION_MAX_DAYS):
                release(row)
                continue
            if len(set(row.used)) >= row.end - row.start + 1:
                # Every number came back to the cloud: nothing left to inutilize.
                row.released_at = now
                row.save(update_fields=["released_at"])
                continue
            free = [n for n in range(row.start, row.end + 1) if n not in row.used and n not in used_here]
            if free and row.created_at >= stale:
                usable.append((row, free))
        if sum(len(free) for _row, free in usable) < RESERVATION_LOW:
            locked = Tenant.objects.select_for_update().get(pk=tenant.pk)
            start = locked.nfce_next_number
            locked.nfce_next_number = start + RESERVATION_SIZE
            locked.save(update_fields=["nfce_next_number"])
            row = NfceReservation.objects.create(tenant=tenant, terminal=terminal, environment=environment,
                                                 series=locked.nfce_series, start=start, end=start + RESERVATION_SIZE - 1)
            usable.append((row, list(range(row.start, row.end + 1))))
            created = True
    tenant.refresh_from_db()
    newest = max((row for row, _free in usable), key=lambda row: row.created_at)
    expires = min(row.created_at for row, _free in usable) + timedelta(days=RESERVATION_MAX_DAYS)
    return {"id": str(newest.pk), "environment": environment, "series": newest.series,
            "numbers": sorted(n for _row, free in usable for n in free),
            "ranges": [[row.start, row.end] for row, _free in usable],
            "csc_id": csc_id, "csc": csc, "qr_version": 2, "issuer": issuer_data(tenant), "tech": tech_responsible(),
            "approx_fallback": str(tenant.approx_tax_fallback_rate) if tenant.approx_tax_fallback_rate is not None else None,
            "expires_at": expires.isoformat(), "created": created}


def release(reservation):
    """Close a block; its unused numbers become gaps and are inutilized.

    ``released_at`` is written in the same transaction that records (and enqueues) the
    inutilizations: if any of them fails, the block stays open and the sweep tries again.
    A company that is no longer emitting in the block's environment cannot inutilize: the block
    is closed without it and the numbers stay as gaps for the accountant (docs/NFCE.md).
    """
    tenant = reservation.tenant
    with transaction.atomic():
        row = NfceReservation.objects.select_for_update().get(pk=reservation.pk)
        if row.released_at:
            reservation.released_at = row.released_at
            return []
        free = [n for n in range(row.start, row.end + 1) if n not in row.used]
        taken = set(FiscalDocument.objects.filter(tenant=tenant, environment=row.environment, series=row.series,
                                                  number__in=free).values_list("number", flat=True))
        free = [n for n in free if n not in taken]
        ranges, start = [], None
        for index, number in enumerate(free):
            if start is None:
                start = number
            if index + 1 == len(free) or free[index + 1] != number + 1:
                ranges.append((start, number))
                start = None
        if ranges and tenant.fiscal_environment != row.environment:
            logger.warning("Reservation %s released without inutilization (company environment %s, block %s): %s",
                           row.pk, tenant.fiscal_environment, row.environment, ranges)
        else:
            for first, last in ranges:
                request_inutilization(tenant, row.environment, row.series, first, last,
                                      "Numeracao reservada para contingencia e nao utilizada")
        row.released_at = timezone.now()
        row.save(update_fields=["released_at"])
        reservation.released_at = row.released_at
    return ranges


def _cents(text):
    try:
        return int((Decimal(str(text)) * 100).to_integral_value())
    except (InvalidOperation, TypeError):
        return None


def register_terminal_contingency(terminal, sale, xml_text):
    """XML (unsigned, with infNFeSupl) of a contingency note made by the register while offline."""
    tenant = terminal.tenant
    try:
        nfe = from_etree(parse(xml_text))
    except XMLError as exc:
        raise APIError("XML da contingência inválido: " + str(exc))
    infnfe = nfe.find("infNFe")
    if nfe.tag != "NFe" or infnfe is None or nfe.find("Signature") is not None:
        raise APIError("XML da contingência inválido")
    key = infnfe.attrs.get("Id", "")[3:]
    if not keys.valid_key(key):
        raise APIError("Chave de acesso inválida")
    parts = keys.split_key(key)
    environment = "production" if infnfe.findtext("ide/tpAmb") == "1" else "homologation"
    checks = [
        (parts["cnpj"] == cnpj_value(tenant.document) and infnfe.findtext("emit/CNPJ") == parts["cnpj"],
         "Emitente difere da empresa"),
        (parts["model"] == "65" and infnfe.findtext("ide/mod") == "65", "Modelo deve ser 65"),
        (parts["emission_type"] == "9" and infnfe.findtext("ide/tpEmis") == "9", "Nota deve estar em contingência"),
        (int(infnfe.findtext("ide/nNF") or 0) == parts["number"] and int(infnfe.findtext("ide/serie") or -1) == parts["series"],
         "Número ou série não conferem com a chave"),
        (environment == tenant.fiscal_environment, "Ambiente da nota difere do ambiente da empresa"),
        (_cents(infnfe.findtext("total/ICMSTot/vNF")) == sale.total_cents, "Total difere da venda"),
    ]
    for ok, message in checks:
        if not ok:
            raise APIError(message, 422)
    qr_url = (nfe.findtext("infNFeSupl/qrCode") or "").strip()
    url_key = (nfe.findtext("infNFeSupl/urlChave") or "").strip()
    if not qr_url or not url_key:
        raise APIError("XML da contingência sem QR Code ou URL de consulta (infNFeSupl)", 422)
    # Signed BEFORE anything is written: a failure here (certificate missing, expired...) leaves no
    # orphan document behind; the register keeps the note and uploads it again later.
    signed = tobytes(backend_for(tenant, environment=environment, provider="sefaz").sign_received(nfe))
    with transaction.atomic():
        Sale.objects.select_for_update().get(pk=sale.pk)
        same = FiscalDocument.objects.filter(tenant=tenant, access_key=key).first()
        if same:
            if same.sale_id != sale.id:
                raise APIError("Chave já usada por outra venda", 409)
            if same.status == "contingency":
                # Upload repeated (the register lost the answer, or a previous attempt failed half way):
                # make sure it is signed and on its way to the SEFAZ.
                FiscalPayload = _payload_model()
                if not FiscalPayload.objects.filter(document=same).exclude(nfe_xml=None).exists():
                    FiscalPayload.objects.filter(document=same).update(nfe_xml=signed)
                _enqueue("fiscal.transmit", tenant, "fiscal.transmit:" + str(same.pk), {"document_id": str(same.pk)})
            return same
        reservation = (NfceReservation.objects.select_for_update()
                       .filter(terminal=terminal, environment=environment, series=parts["series"],
                               start__lte=parts["number"], end__gte=parts["number"]).first())
        if not reservation or parts["number"] in reservation.used:
            raise APIError("Número fora da faixa reservada para este caixa", 409, code="reservation_unknown")
        if (reservation.released_at
                or reservation.created_at < timezone.now() - timedelta(days=RESERVATION_MAX_DAYS)):
            # The block was closed and its free numbers inutilized (or are about to be): this note can
            # never be authorized. The register asks the cloud for a normal NFC-e instead.
            raise APIError("Faixa de contingência já liberada; a nota será emitida de novo com outro número", 409,
                           code="reservation_released")
        if FiscalDocument.objects.filter(tenant=tenant, environment=environment, series=parts["series"],
                                         number=parts["number"]).exists():
            raise APIError("Número já usado", 409, code="reservation_unknown")
        previous = current_document(sale)
        document = FiscalDocument.objects.create(
            tenant=tenant, shop=sale.shop, sale=sale, terminal=terminal,
            provider="sefaz", external_id="pdv2-" + str(sale.pk) + "-t", environment=environment,
            series=parts["series"], number=parts["number"], access_key=key, emission_type=9, status="contingency",
            issued_at=_parse_time(infnfe.findtext("ide/dhEmi")) or timezone.now(),
            qr_url=qr_url, url_key=url_key[:300])
        reservation.used = sorted(set(reservation.used) | {parts["number"]})
        reservation.save(update_fields=["used"])
        if previous:
            FiscalDocument.objects.filter(pk=previous.pk).update(replaced_by=document)
        _save_params(document, {"cnf": parts["cnf"], "issued_at": infnfe.findtext("ide/dhEmi"), "emission_type": 9,
                                "contingency_at": infnfe.findtext("ide/dhCont"),
                                "contingency_reason": infnfe.findtext("ide/xJust"), "qr_version": 2,
                                "sent": False, "origin": "terminal"})
        _payload_model().objects.filter(document=document).update(nfe_xml=signed)
        _sync_sale(document)
        # Same transaction: the note is never committed without its transmission job.
        if previous:
            _enqueue("fiscal.reconcile_replaced", tenant, "fiscal.reconcile_replaced:" + str(previous.pk),
                     {"document_id": str(previous.pk)}, 30)
        _enqueue("fiscal.transmit", tenant, "fiscal.transmit:" + str(document.pk), {"document_id": str(document.pk)})
    return document


def sweep():
    """Periodic housekeeping: stale reservations are released (gaps inutilized)."""
    limit = timezone.now() - timedelta(days=RESERVATION_MAX_DAYS)
    released = 0
    for reservation in NfceReservation.objects.select_related("tenant").filter(released_at__isnull=True,
                                                                               created_at__lt=limit):
        try:
            release(reservation)
            released += 1
        except Exception:  # noqa: BLE001 - one bad block must not stop the others; it stays open for the next run
            logger.exception("Reservation %s could not be released", reservation.pk)
    return released


# ------------------------------------------------------------------ views data

def danfe_data(document):
    """Fiscal block of the terminal receipt (terminal/printers.py receipt_layout)."""
    if document is None:
        return None
    params = _params(document)
    state = {"authorized": "authorized", "contingency": "contingency"}.get(document.status, "none")
    data = {"status": document.status, "state": state, "access_key": document.access_key,
            "protocol": document.authorization, "number": document.number, "series": document.series,
            "issued_at": int(document.issued_at.timestamp() * 1000) if document.issued_at else None,
            "authorized_at": int(document.authorized_at.timestamp() * 1000) if document.authorized_at else None,
            "url_consulta": document.url_key, "qr_url": document.qr_url, "environment": document.environment,
            "emission_type": document.emission_type, "error": document.error_message,
            "document_id": str(document.pk)}
    data.update({k: v for k, v in (params.get("danfe") or {}).items() if v is not None})
    return data


def contingency_alerts(tenant):
    limit = timezone.now() - timedelta(hours=CONTINGENCY_ALERT_HOURS)
    rows = FiscalDocument.objects.filter(tenant=tenant, status="contingency", replaced_by__isnull=True)
    return {"pending": rows.count(), "overdue": rows.filter(issued_at__lt=limit).count()}


def document_data(row):
    deadline = cancel_deadline(row) if row.status == "authorized" else None
    return {"id": str(row.pk), "sale_id": str(row.sale_id), "status": row.status, "provider": row.provider,
            "environment": row.environment, "series": row.series, "number": row.number, "access_key": row.access_key,
            "emission_type": row.emission_type, "authorization": row.authorization, "status_code": row.status_code,
            "error_message": row.error_message, "issued_at": row.issued_at.isoformat() if row.issued_at else None,
            "authorized_at": row.authorized_at.isoformat() if row.authorized_at else None,
            "cancelled_at": row.cancelled_at.isoformat() if row.cancelled_at else None,
            "cancel_until": deadline.isoformat() if deadline else None,
            "can_cancel": bool(deadline and timezone.now() <= deadline),
            "replaced": bool(row.replaced_by_id), "qr_url": row.qr_url, "url_key": row.url_key,
            "created_at": row.created_at.isoformat()}
