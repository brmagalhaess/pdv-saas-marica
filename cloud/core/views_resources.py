"""Tenant-scoped catalog, shops and contacts."""
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import JsonResponse

from .api_common import APIError, audit, endpoint, fields, membership_context, method, money, page, payload, scoped, text, uuid_value
from .models import Category, Customer, Product, Shop, Supplier, Terminal
from .serializers import category as category_data, contact as contact_data, product as product_data, shop as shop_data


OWNER = {"owner", "admin"}
CATALOG_WRITE = OWNER | {"manager", "inventory"}
CONTACT_WRITE = OWNER | {"manager"}
RESOURCE = {
    "shops": (Shop, shop_data, OWNER, {"owner", "admin", "manager", "inventory", "finance", "cashier", "viewer"}),
    "categories": (Category, category_data, CATALOG_WRITE, {"owner", "admin", "manager", "inventory", "finance", "cashier", "viewer"}),
    "products": (Product, product_data, CATALOG_WRITE, {"owner", "admin", "manager", "inventory", "finance", "cashier", "viewer"}),
    "customers": (Customer, contact_data, CONTACT_WRITE | {"cashier"}, {"owner", "admin", "manager", "cashier", "finance"}),
    "suppliers": (Supplier, contact_data, CONTACT_WRITE | {"inventory"}, {"owner", "admin", "manager", "inventory", "finance"}),
}


def response_for(row, serializer, member):
    result = serializer(row)
    if isinstance(row, Product) and member.role == "cashier":
        result.pop("cost_cents", None)
    return result


def form_values(kind, data, tenant, creating):
    if kind == "shops":
        allowed, required = {"name", "address", "active"}, {"name"}
    elif kind == "categories":
        allowed, required = {"name", "active"}, {"name"}
    elif kind == "products":
        allowed = {"sku", "barcode", "name", "category_id", "variant", "unit",
                   "price_cents", "cost_cents", "stock_minimum_milli", "tax_fields", "active"}
        required = {"sku", "name", "price_cents"}
    else:
        allowed, required = {"name", "document", "email", "phone", "active"}, {"name"}
    fields(data, allowed, required if creating else ())
    if not data:
        raise APIError("Informe ao menos um campo")
    result = {}
    for key, value in data.items():
        if key == "active":
            if type(value) is not bool:
                raise APIError("Estado inválido")
            result[key] = value
        elif key == "address":
            if not isinstance(value, dict) or len(str(value)) > 2000:
                raise APIError("Endereço inválido")
            result[key] = value
        elif key == "category_id":
            if value is None or value == "":
                result["category"] = None
            else:
                result["category"] = scoped(Category, tenant, value)
        elif key in ("price_cents", "cost_cents", "stock_minimum_milli"):
            result[key] = money(value, key)
        elif key == "tax_fields":
            if not isinstance(value, dict) or len(str(value)) > 16000:
                raise APIError("Dados tributários inválidos")
            result[key] = value
        elif key == "unit":
            if value not in ("UN", "KG", "L"):
                raise APIError("Unidade inválida")
            result[key] = value
        elif key == "email":
            value = text(value, "E-mail", 254, optional=True).lower()
            if value:
                try:
                    validate_email(value)
                except ValidationError:
                    raise APIError("E-mail inválido")
            result[key] = value
        elif key == "sku":
            result[key] = text(value, "SKU", 80).upper()
        elif key == "name":
            result[key] = text(value, "Nome", 120 if kind == "categories" else 160 if kind == "shops" else 180)
        else:
            maximum = {"barcode": 40, "variant": 120, "document": 30, "phone": 30}.get(key, 160)
            result[key] = text(value, key, maximum, optional=True)
    return result


@endpoint
def collection(request, kind):
    if kind not in RESOURCE:
        raise APIError("Recurso não encontrado", 404)
    model, serializer, write_roles, read_roles = RESOURCE[kind]
    method(request, "GET", "POST")
    member = membership_context(request, roles=read_roles if request.method == "GET" else write_roles,
                                write=request.method == "POST")
    tenant = member.tenant
    if request.method == "GET":
        rows = model.objects.filter(tenant=tenant)
        if kind in ("categories", "products", "customers", "suppliers"):
            q = request.GET.get("q", "").strip()
            if len(q) > 120:
                raise APIError("Busca muito longa")
            if q:
                if kind == "products":
                    rows = rows.filter(Q(name__icontains=q) | Q(sku__icontains=q) | Q(barcode__icontains=q))
                else:
                    rows = rows.filter(name__icontains=q)
        if member.role in ("cashier", "viewer") or request.GET.get("include_inactive") != "1":
            rows = rows.filter(active=True)
        return page(request, rows.order_by("id"), lambda row: response_for(row, serializer, member))
    values = form_values(kind, payload(request), tenant, True)
    try:
        with transaction.atomic():
            if kind == "shops":
                Tenant = type(tenant)
                Tenant.objects.select_for_update().get(pk=tenant.pk)
                active = Shop.objects.filter(tenant=tenant, active=True).count()
                if active >= tenant.subscription.plan.shop_limit:
                    raise APIError("Limite de lojas do plano atingido", 409)
            row = model.objects.create(tenant=tenant, **values)
            audit(tenant, member.user, f"{kind}.created", row)
    except IntegrityError:
        raise APIError("Registro duplicado", 409)
    return JsonResponse(response_for(row, serializer, member), status=201)


@endpoint
def detail(request, kind, pk):
    if kind not in RESOURCE:
        raise APIError("Recurso não encontrado", 404)
    model, serializer, write_roles, read_roles = RESOURCE[kind]
    method(request, "GET", "PATCH", "DELETE")
    member = membership_context(request, roles=read_roles if request.method == "GET" else write_roles,
                                write=request.method != "GET")
    row = scoped(model, member.tenant, pk)
    if request.method == "GET":
        if not row.active and member.role in ("cashier", "viewer"):
            raise APIError("Registro não encontrado", 404)
        return JsonResponse(response_for(row, serializer, member))
    if request.method == "DELETE":
        values = {"active": False}
    else:
        values = form_values(kind, payload(request), member.tenant, False)
    try:
        with transaction.atomic():
            if isinstance(row, Shop) and "active" in values:
                type(member.tenant).objects.select_for_update().get(pk=member.tenant_id)
                row = scoped(Shop, member.tenant, pk)
                active_count = Shop.objects.filter(tenant=member.tenant, active=True).count()
                if values["active"] and not row.active and active_count >= member.tenant.subscription.plan.shop_limit:
                    raise APIError("Limite de lojas do plano atingido", 409)
                if not values["active"] and row.active:
                    if active_count <= 1:
                        raise APIError("Não é possível desativar a última loja", 409)
                    if Terminal.objects.filter(shop=row, revoked=False).exists():
                        raise APIError("Revogue os terminais da loja antes de desativá-la", 409)
            for field, value in values.items():
                setattr(row, field, value)
            row.save()
            audit(member.tenant, member.user, f"{kind}.updated", row, {"fields": list(values)})
    except IntegrityError:
        raise APIError("Registro duplicado", 409)
    return JsonResponse(response_for(row, serializer, member))
