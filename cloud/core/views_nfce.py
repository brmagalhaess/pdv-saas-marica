"""Rotas do caixa para a NFC-e (token do terminal). Ver docs/NFCE.md, "Fluxo com o caixa".

- POST nfce                 {sale_id}: emite (idempotente pela venda) e espera até ~8 s;
- GET  nfce/<sale_id>       estado atual (consulta a SEFAZ/provedor se ainda pendente);
- POST nfce/reserve         faixa de números + CSC para contingência off-line no caixa;
- POST nfce/contingency     {sale_id, xml}: nota emitida no caixa sem internet (tpEmis 9);
- POST nfce/<sale_id>/cancel {reason, manager_id}: cancelamento com PIN de gerente no caixa.
"""
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from integrations.base import ProviderError
from .api_common import APIError, audit, endpoint, fields, method, payload, text, uuid_value
from .models import Membership, Sale
from .nfce import service
from .views_terminal import _bearer_terminal

TERMINAL_TIMEOUT = (3, 5)


def _sale(terminal, sale_id):
    sale = (Sale.objects.select_related("tenant", "shop")
            .filter(pk=uuid_value(sale_id, "Venda"), tenant=terminal.tenant, terminal=terminal).first())
    if not sale:
        raise APIError("Venda ainda não sincronizada com a nuvem", 409, code="sale_not_synced")
    return sale


def _answer(document):
    data = service.danfe_data(document)
    return JsonResponse({"status": document.status if document else "none", "fiscal": data})


@csrf_exempt
@endpoint
def issue(request):
    method(request, "POST")
    terminal = _bearer_terminal(request)
    data = payload(request)
    fields(data, ("sale_id",), ("sale_id",))
    sale = _sale(terminal, data["sale_id"])
    if not service.enabled(sale.tenant):
        return JsonResponse({"status": "disabled", "fiscal": None})
    try:
        document = service.issue_for_sale(sale, terminal, timeout=TERMINAL_TIMEOUT)
    except ProviderError as exc:
        document = service.current_document(sale)
        if document is None:
            raise APIError("Provedor fiscal indisponível: " + str(exc), 503, code="fiscal_unavailable")
    return _answer(document)


@csrf_exempt
@endpoint
def detail(request, sale_id):
    method(request, "GET")
    terminal = _bearer_terminal(request)
    sale = _sale(terminal, sale_id)
    document = service.current_document(sale)
    if document and document.status == "pending":
        try:
            document = service.poll(document, timeout=TERMINAL_TIMEOUT)
        except (ProviderError, APIError, ValueError):
            pass
    return _answer(document)


@csrf_exempt
@endpoint
def reserve(request):
    method(request, "POST")
    terminal = _bearer_terminal(request)
    result = service.reserve_numbers(terminal)
    audit(terminal.tenant, None, "fiscal.numbers_reserved", terminal,
          {"series": result["series"], "numbers": [result["numbers"][0], result["numbers"][-1]] if result["numbers"] else []})
    return JsonResponse(result)


@csrf_exempt
@endpoint
def contingency(request):
    method(request, "POST")
    terminal = _bearer_terminal(request, send_only_ok=True)
    data = payload(request)
    fields(data, ("sale_id", "xml"), ("sale_id", "xml"))
    sale = _sale(terminal, data["sale_id"])
    xml = text(data["xml"], "XML", 500000)
    document = service.register_terminal_contingency(terminal, sale, xml)
    return _answer(document)


@csrf_exempt
@endpoint
def cancel(request, sale_id):
    method(request, "POST")
    terminal = _bearer_terminal(request)
    data = payload(request)
    fields(data, ("reason", "manager_id"), ("reason", "manager_id"))
    sale = _sale(terminal, sale_id)
    manager = Membership.objects.filter(pk=uuid_value(data["manager_id"], "Gerente"), tenant=terminal.tenant,
                                        active=True, role__in=("owner", "admin", "manager")).first()
    if not manager:
        raise APIError("Cancelamento exige PIN de gerente", 403)
    document = service.current_document(sale)
    if not document:
        raise APIError("Venda sem NFC-e", 404)
    job = service.request_cancel(document, text(data["reason"], "Justificativa", 255))
    audit(terminal.tenant, None, "fiscal.cancel_requested", document, {"manager_id": str(manager.pk), "terminal": True})
    return JsonResponse({"status": document.status, "job_id": str(job.pk) if job else None,
                         "fiscal": service.danfe_data(document)}, status=202)
