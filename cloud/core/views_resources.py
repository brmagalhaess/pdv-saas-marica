"""Tenant-scoped catalog, shops and contacts."""
import secrets

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import JsonResponse

from .api_common import APIError, audit, bps, endpoint, fields, membership_context, method, money, page, payload, scoped, text, uuid_value
from .models import Category, Customer, Product, SaleItem, Shop, StockMovement, StockReceiptItem, Supplier, Terminal
from .nfe import valid_gtin
from .serializers import category as category_data, contact as contact_data, product as product_data, shop as shop_data


OWNER = {"owner", "admin"}
CATALOG_WRITE = OWNER | {"manager", "inventory"}
CONTACT_WRITE = OWNER | {"manager"}
# The fiado limit is a credit decision: the cashier registers customers, but the
# limit and deactivation stay with management; finance may only set the limit.
CREDIT_LIMIT_ROLES = CONTACT_WRITE | {"finance"}
RESOURCE = {
    "shops": (Shop, shop_data, OWNER, {"owner", "admin", "manager", "inventory", "finance", "cashier", "viewer"}),
    "categories": (Category, category_data, CATALOG_WRITE, {"owner", "admin", "manager", "inventory", "finance", "cashier", "viewer"}),
    "products": (Product, product_data, CATALOG_WRITE, {"owner", "admin", "manager", "inventory", "finance", "cashier", "viewer"}),
    "customers": (Customer, contact_data, CONTACT_WRITE | {"cashier", "finance"}, {"owner", "admin", "manager", "cashier", "finance"}),
    "suppliers": (Supplier, contact_data, CONTACT_WRITE | {"inventory"}, {"owner", "admin", "manager", "inventory", "finance"}),
}


def response_for(row, serializer, member):
    result = serializer(row)
    if isinstance(row, Product) and member.role == "cashier":
        result.pop("cost_cents", None)
    return result


def _ean13_check_digit(first_twelve):
    total = sum(int(digit) * (1 if index % 2 == 0 else 3)
                for index, digit in enumerate(first_twelve))
    return str((-total) % 10)


def generated_product_codes(tenant):
    """Generate tenant-unique SKU and internal-use EAN-13 barcode.

    The tenant row is locked by the caller, and unique database constraints
    remain the final guard against collisions.
    """
    while True:
        sku = f"P{secrets.randbelow(1_000_000_000):09d}"
        if not Product.objects.filter(tenant=tenant, sku=sku).exists():
            break
    while True:
        # Prefix 2 marks this as an internal/restricted-circulation code;
        # it is not a GS1-assigned GTIN for sale across unrelated businesses.
        body = f"2{secrets.randbelow(100_000_000_000):011d}"
        barcode = body + _ean13_check_digit(body)
        if not Product.objects.filter(tenant=tenant, barcode=barcode).exists():
            break
    return {"sku": sku, "barcode": barcode}


def default_product_image(name, category=""):
    """Choose a small bundled image from the local product image bank.

    The path is served by the web application, so a product remains useful
    without relying on an external image host. A custom URL can still be
    supplied by the company when a catalog photo is available.
    """
    text_value = f"{name} {category}".casefold()
    groups = {
        "beverages": ("bebida", "água", "agua", "refrigerante", "suco", "cerveja", "vinho", "café", "cafe", "leite"),
        "bakery": ("pão", "pao", "bolo", "biscoito", "padaria", "massa"),
        "cleaning": ("limpeza", "sabão", "sabao", "detergente", "desinfetante", "higiene", "papel"),
        "produce": ("fruta", "verdura", "legume", "hortifruti", "banana", "maçã", "maca", "tomate"),
    }
    key = next((key for key, words in groups.items() if any(word in text_value for word in words)), "grocery")
    return f"/assets/product-images/{key}.svg"


def customer_guard(member, data, row=None):
    """Role limits inside the customer form; `row` is None on creation.

    A restricted field sent with its current value is dropped, so a form that
    always posts every field keeps working for the cashier.
    """
    if member.role == "finance" and (row is None or set(data) - {"credit_limit_cents"}):
        raise APIError("Financeiro altera apenas o limite de fiado do cliente", 403)
    if member.role not in CREDIT_LIMIT_ROLES and "credit_limit_cents" in data:
        if data["credit_limit_cents"] != (row.credit_limit_cents if row else 0):
            raise APIError("Somente proprietário, administrador, gerente ou financeiro definem o limite de fiado", 403)
        del data["credit_limit_cents"]
    if member.role not in CONTACT_WRITE and "active" in data:
        if data["active"] is not (row.active if row else True):
            raise APIError("Somente proprietário, administrador ou gerente ativam ou desativam clientes", 403)
        del data["active"]


def barcode_value(value, tenant, row=None):
    """Factory barcode (EAN-8/12/13/14 with a valid check digit), unique per company.

    Empty means there is no factory code; the caller generates the internal one.
    """
    value = "" if value is None else value
    if not isinstance(value, str):
        raise APIError("Código de barras inválido")
    value = value.strip()
    if not value:
        return ""
    if not value.isascii() or not valid_gtin(value):
        raise APIError("Código de barras inválido: informe um EAN de 8, 12, 13 ou 14 dígitos com o dígito verificador correto")
    clash = Product.objects.filter(tenant=tenant, barcode=value)
    if row is not None:
        clash = clash.exclude(pk=row.pk)
    if clash.exists():
        raise APIError("Código de barras já cadastrado em outro produto", 409)
    return value


def product_guard(row, values):
    """Checks of a product edit that depend on its history. Caller holds the row lock."""
    if "barcode" in values:
        code = barcode_value(values["barcode"], row.tenant, row)
        if code:
            values["barcode"] = code
        elif len(row.barcode) == 13 and row.barcode.startswith("2"):
            # Cleared field on a product that already uses the internal code:
            # keep it, printed labels stay valid.
            del values["barcode"]
        else:
            values["barcode"] = generated_product_codes(row.tenant)["barcode"]
    if "unit" in values and values["unit"] != row.unit and (
            SaleItem.objects.filter(product=row).exists() or StockMovement.objects.filter(product=row).exists()):
        raise APIError("Produto com vendas ou movimentações não pode trocar de unidade; cadastre um novo produto", 409)
    if ("cost_cents" in values and values["cost_cents"] != row.cost_cents
            and StockReceiptItem.objects.filter(product=row).exists()):
        raise APIError("Produto com entrada de mercadoria tem o custo calculado pelas entradas", 409)


def form_values(kind, data, tenant, creating):
    if kind == "shops":
        allowed, required = {"name", "address", "active"}, {"name"}
    elif kind == "categories":
        allowed, required = {"name", "active", "commission_bps"}, {"name"}
    elif kind == "products":
        allowed = {"name", "category_id", "variant", "unit", "scale_code", "ncm", "barcode", "cost_cents",
                   "price_cents", "stock_minimum_milli", "stock_controlled", "image_url", "tax_fields", "active"}
        required = {"name", "price_cents"}
    else:
        allowed, required = {"name", "document", "email", "phone", "active"}, {"name"}
        if kind == "customers":
            allowed = allowed | {"credit_limit_cents"}
    fields(data, allowed, required if creating else ())
    if not data:
        raise APIError("Informe ao menos um campo")
    result = {}
    for key, value in data.items():
        if key == "active":
            if type(value) is not bool:
                raise APIError("Estado inválido")
            result[key] = value
        elif key == "commission_bps":
            result[key] = None if value is None or value == "" else bps(value, "Comissão da categoria")
        elif key == "stock_controlled":
            if type(value) is not bool:
                raise APIError("Configuração de estoque inválida")
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
        elif key in ("price_cents", "stock_minimum_milli", "credit_limit_cents"):
            result[key] = money(value, key)
        elif key == "cost_cents":
            result[key] = money(value, "Custo")
        elif key == "barcode":
            # Validated by barcode_value, which needs to know the row being edited.
            result[key] = value
        elif key == "tax_fields":
            if not isinstance(value, dict) or len(str(value)) > 16000:
                raise APIError("Dados tributários inválidos")
            result[key] = value
        elif key == "image_url":
            value = text(value, "Imagem", 500, optional=True)
            if value and not (value.startswith("/") or value.startswith("https://") or value.startswith("http://")):
                raise APIError("Imagem deve ser uma URL http(s) ou um caminho local")
            result[key] = value
        elif key == "unit":
            if value not in ("UN", "KG", "L"):
                raise APIError("Unidade inválida")
            result[key] = value
        elif key == "scale_code":
            if value is None:
                value = ""
            if not isinstance(value, str) or value.strip() and (not value.strip().isdigit() or not 1 <= len(value.strip()) <= 6):
                raise APIError("Código PLU da balança inválido")
            result[key] = value.strip()
        elif key == "ncm":
            value = text(value, "NCM", 8, optional=True)
            if value and (not value.isdigit() or len(value) not in (7, 8)):
                raise APIError("NCM deve conter 7 ou 8 dígitos")
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
            maximum = {"variant": 120, "document": 30, "phone": 30}.get(key, 160)
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
        q = request.GET.get("q", "").strip()
        if len(q) > 120:
            raise APIError("Busca muito longa")
        if q:
            if kind == "products":
                rows = rows.filter(Q(name__icontains=q) | Q(sku__icontains=q) | Q(barcode__icontains=q))
            elif kind in ("customers", "suppliers"):
                rows = rows.filter(Q(name__icontains=q) | Q(document__icontains=q) | Q(phone__icontains=q))
            else:
                rows = rows.filter(name__icontains=q)
        if member.role in ("cashier", "viewer") or request.GET.get("include_inactive") != "1":
            rows = rows.filter(active=True)
        return page(request, rows.order_by("name", "id"), lambda row: response_for(row, serializer, member))
    data = payload(request)
    if kind == "customers":
        customer_guard(member, data)
    values = form_values(kind, data, tenant, True)
    if kind == "products":
        values["barcode"] = barcode_value(values.get("barcode"), tenant)
    if kind == "products" and not values.get("image_url"):
        category = values.get("category")
        values["image_url"] = default_product_image(values["name"], category.name if category else "")
    try:
        with transaction.atomic():
            if kind == "shops":
                Tenant = type(tenant)
                Tenant.objects.select_for_update().get(pk=tenant.pk)
                active = Shop.objects.filter(tenant=tenant, active=True).count()
                if active >= tenant.subscription.plan.shop_limit:
                    raise APIError("Limite de lojas do plano atingido", 409)
            elif kind == "products":
                # Serialize generation for this company; database uniqueness
                # constraints protect the identifiers if another writer races.
                type(tenant).objects.select_for_update().get(pk=tenant.pk)
                codes = generated_product_codes(tenant)
                values["sku"] = codes["sku"]
                values["barcode"] = values["barcode"] or codes["barcode"]
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
    data = {"active": False} if request.method == "DELETE" else payload(request)
    if kind == "customers":
        customer_guard(member, data, row)
    if request.method == "DELETE":
        values = dict(data)
    else:
        values = form_values(kind, data, member.tenant, False)
        if isinstance(row, Product) and "image_url" in values and not values["image_url"]:
            category = values.get("category", row.category)
            values["image_url"] = default_product_image(row.name, category.name if category else "")
    try:
        with transaction.atomic():
            if isinstance(row, Product):
                # Stock receipts write the cost under this same row lock.
                row = Product.objects.select_for_update().get(pk=row.pk)
                product_guard(row, values)
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
            changes = {"fields": list(values)}
            if "credit_limit_cents" in values:
                changes["credit_limit_cents"] = {"from": row.credit_limit_cents, "to": values["credit_limit_cents"]}
            for field, value in values.items():
                setattr(row, field, value)
            # Only the edited columns: a full save could undo a cost written
            # by a stock receipt between the read and this write.
            row.save(update_fields=[*values, "updated_at"] if isinstance(row, Product) else list(values))
            audit(member.tenant, member.user, f"{kind}.updated", row, changes)
    except IntegrityError:
        raise APIError("Registro duplicado", 409)
    return JsonResponse(response_for(row, serializer, member))
