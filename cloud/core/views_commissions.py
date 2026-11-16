from datetime import date

from django.db.models import Count, Q, Sum
from django.http import JsonResponse

from .api_common import APIError, endpoint, fields, iso, membership_context, method, page, payload, require_feature, scoped
from .commission_logic import pay
from .models import Commission, Membership

READ = {"owner", "admin", "manager", "finance"}
PAY = {"owner", "admin", "finance"}


def commission_data(row):
    return {"id": str(row.id), "sale_id": str(row.sale_id), "seller_id": str(row.seller_id),
            "seller_name": row.seller.name, "base_cents": row.base_cents, "amount_cents": row.amount_cents,
            "status": row.status, "reversal": row.reversal, "sale_created_at": iso(row.sale.created_at),
            "paid_at": iso(row.paid_at),
            "finance_entry_id": str(row.finance_entry_id) if row.finance_entry_id else None}


def _filtered(request, tenant):
    rows = Commission.objects.select_related("seller", "sale").filter(tenant=tenant)
    if request.GET.get("seller_id"):
        rows = rows.filter(seller=scoped(Membership, tenant, request.GET["seller_id"]))
    if request.GET.get("status"):
        if request.GET["status"] not in ("pending", "paid", "cancelled"):
            raise APIError("Filtro de estado inválido")
        rows = rows.filter(status=request.GET["status"])
    for key, lookup in (("from", "sale__created_at__date__gte"), ("to", "sale__created_at__date__lte")):
        if request.GET.get(key):
            try:
                rows = rows.filter(**{lookup: date.fromisoformat(request.GET[key])})
            except ValueError:
                raise APIError("Data inválida")
    return rows


@endpoint
def commissions(request):
    method(request, "GET")
    member = membership_context(request, roles=READ)
    require_feature(member, "finance")
    return page(request, _filtered(request, member.tenant).order_by("-created_at", "-id"), commission_data)


@endpoint
def commission_summary(request):
    method(request, "GET")
    member = membership_context(request, roles=READ)
    require_feature(member, "finance")
    rows = (_filtered(request, member.tenant).values("seller_id", "seller__name", "seller__commission_bps")
            .annotate(sales=Count("id", filter=Q(reversal=False) & ~Q(status="cancelled")),
                      pending=Sum("amount_cents", filter=Q(status="pending")),
                      paid=Sum("amount_cents", filter=Q(status="paid")),
                      base=Sum("base_cents", filter=~Q(status="cancelled")))
            .order_by("seller__name"))
    return JsonResponse({"results": [{"seller_id": str(r["seller_id"]), "seller_name": r["seller__name"],
                                      "commission_bps": r["seller__commission_bps"], "sales": r["sales"],
                                      "base_cents": r["base"] or 0, "pending_cents": r["pending"] or 0,
                                      "paid_cents": r["paid"] or 0} for r in rows]})


@endpoint
def commission_pay(request):
    method(request, "POST")
    member = membership_context(request, roles=PAY, write=True)
    require_feature(member, "finance")
    data = payload(request)
    fields(data, ("seller_id", "due_date"), ("seller_id", "due_date"))
    seller = scoped(Membership, member.tenant, data["seller_id"])
    try:
        due = date.fromisoformat(data["due_date"])
    except (TypeError, ValueError):
        raise APIError("Vencimento inválido")
    entry, rows = pay(member.tenant, seller, member.user, due)
    return JsonResponse({"finance_entry_id": str(entry.id), "amount_cents": entry.amount_cents,
                         "count": len(rows)}, status=201)
