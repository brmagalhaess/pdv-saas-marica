"""Fiado statement and receipt, available on every plan (core retail need)."""
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone

from .api_common import APIError, audit, endpoint, fields, iso, membership_context, method, payload, scoped, uuid_value
from .credit_logic import balances
from .models import Customer, CustomerCredit, FinanceEntry, SaleReturn

READ = {"owner", "admin", "manager", "finance", "cashier"}
RECEIVE = {"owner", "admin", "manager", "finance"}


@endpoint
def credit_summary(request):
    method(request, "GET")
    member = membership_context(request, roles=READ)
    totals = balances(member.tenant)
    limits = dict(Customer.objects.filter(tenant=member.tenant, pk__in=list(totals)).values_list("id", "credit_limit_cents"))
    return JsonResponse({"results": [{"customer_id": str(pk), "open_credit_cents": open_, "store_credit_cents": credit,
                                      "credit_limit_cents": limits.get(pk, 0)}
                                     for pk, (open_, credit) in totals.items() if open_ or credit]})


@endpoint
def statement(request, pk):
    method(request, "GET")
    member = membership_context(request, roles=READ)
    customer = scoped(Customer, member.tenant, pk)
    open_, credit = balances(member.tenant, [customer.id]).get(customer.id, (0, 0))
    entries = FinanceEntry.objects.filter(tenant=member.tenant, customer=customer, kind="receivable",
                                          sale__isnull=False).order_by("-created_at")[:100]
    ledger = CustomerCredit.objects.filter(tenant=member.tenant, customer=customer).order_by("-created_at")[:100]
    returns = (SaleReturn.objects.filter(tenant=member.tenant, sale__customer=customer)
               .order_by("-created_at")[:50])
    return JsonResponse({
        "customer": {"id": str(customer.id), "name": customer.name, "credit_limit_cents": customer.credit_limit_cents},
        "open_credit_cents": open_, "store_credit_cents": credit,
        "available_credit_cents": max(0, customer.credit_limit_cents - open_),
        "entries": [{"id": str(e.id), "sale_id": str(e.sale_id), "description": e.description,
                     "amount_cents": e.amount_cents, "due_date": e.due_date.isoformat(), "status": e.status,
                     "overdue": e.status == "open" and e.due_date < timezone.localdate(),
                     "settled_at": iso(e.settled_at)} for e in entries],
        "credits": [{"id": str(c.id), "delta_cents": c.delta_cents, "source": c.source, "description": c.description,
                     "created_at": iso(c.created_at)} for c in ledger],
        "returns": [{"id": str(r.id), "sale_id": str(r.sale_id), "total_cents": r.total_cents,
                     "refund_method": r.refund_method, "reason": r.reason, "created_at": iso(r.created_at)}
                    for r in returns]})


@endpoint
def receive(request, pk):
    method(request, "POST")
    member = membership_context(request, roles=RECEIVE, write=True)
    customer = scoped(Customer, member.tenant, pk)
    data = payload(request)
    fields(data, ("entry_ids",), ("entry_ids",))
    ids = data["entry_ids"]
    if not isinstance(ids, list) or not 1 <= len(ids) <= 200:
        raise APIError("Informe as parcelas a receber")
    wanted = {uuid_value(value, "Parcela") for value in ids}
    with transaction.atomic():
        rows = list(FinanceEntry.objects.select_for_update().filter(
            tenant=member.tenant, customer=customer, kind="receivable", sale__isnull=False, pk__in=wanted))
        if len(rows) != len(wanted):
            raise APIError("Parcela não encontrada para este cliente", 404)
        if any(row.status != "open" for row in rows):
            raise APIError("Há parcela que não está em aberto", 409)
        now = timezone.now()
        for row in rows:
            row.status, row.settled_at = "settled", now
            row.save(update_fields=["status", "settled_at"])
        total = sum(row.amount_cents for row in rows)
        audit(member.tenant, member.user, "customer.credit_received", customer, {"entries": len(rows), "amount_cents": total})
    return JsonResponse({"received_cents": total, "count": len(rows)})
