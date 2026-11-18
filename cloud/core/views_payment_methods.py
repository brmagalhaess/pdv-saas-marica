from django.db import transaction
from django.http import JsonResponse

from .api_common import APIError, audit, bps, endpoint, fields, membership_context, method, payload, scoped, text
from .models import PaymentMethod
from .payment_methods import ensure, method_data

MANAGE = {"owner", "admin"}


def _values(data, creating):
    fields(data, ("name", "fee_bps", "settlement_days", "active", "position", "print_rule"), ("name",) if creating else ())
    if not data:
        raise APIError("Informe ao menos um campo")
    values = {}
    if "print_rule" in data:
        if data["print_rule"] not in ("always", "ask", "never"):
            raise APIError("Impressão da nota: use sempre, perguntar ou não imprimir")
        values["print_rule"] = data["print_rule"]
    if "name" in data:
        values["name"] = text(data["name"], "Nome", 60)
    if "fee_bps" in data:
        values["fee_bps"] = bps(data["fee_bps"], "Taxa")
    for key, label, top in (("settlement_days", "Prazo de recebimento", 365), ("position", "Ordem", 1000)):
        if key in data:
            if type(data[key]) is not int or not 0 <= data[key] <= top:
                raise APIError(f"{label} inválido")
            values[key] = data[key]
    if "active" in data:
        if type(data["active"]) is not bool:
            raise APIError("Estado inválido")
        values["active"] = data["active"]
    return values


@endpoint
def payment_methods(request):
    method(request, "GET", "POST")
    member = membership_context(request, roles=None if request.method == "GET" else MANAGE,
                                write=request.method == "POST")
    if request.method == "GET":
        return JsonResponse({"results": [method_data(row) for row in ensure(member.tenant)]})
    values = _values(payload(request), creating=True)
    if PaymentMethod.objects.filter(tenant=member.tenant, kind="other").count() >= 20:
        raise APIError("Limite de 20 formas próprias", 409)
    with transaction.atomic():
        row = PaymentMethod.objects.create(tenant=member.tenant, kind="other", position=100, **values)
        audit(member.tenant, member.user, "payment_method.created", row)
    return JsonResponse(method_data(row), status=201)


@endpoint
def payment_method_detail(request, pk):
    method(request, "PATCH")
    member = membership_context(request, roles=MANAGE, write=True)
    row = scoped(PaymentMethod, member.tenant, pk)
    values = _values(payload(request), creating=False)
    if row.kind == "cash" and values.get("active") is False:
        raise APIError("Dinheiro não pode ser desativado: o caixa precisa dele para troco e conferência", 409)
    with transaction.atomic():
        for key, value in values.items():
            setattr(row, key, value)
        row.save(update_fields=list(values))
        audit(member.tenant, member.user, "payment_method.updated", row, {"fields": list(values)})
    return JsonResponse(method_data(row))
