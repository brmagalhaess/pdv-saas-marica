"""Variation grids: generate one Product per attribute combination."""
from itertools import product as combinations

from django.db import IntegrityError, transaction
from django.http import JsonResponse

from .api_common import APIError, audit, endpoint, fields, membership_context, method, money, page, payload, scoped, text
from .models import Category, Product, ProductGroup
from .serializers import product as product_data
from .views_resources import CATALOG_WRITE, default_product_image, generated_product_codes

MAX_ATTRIBUTES, MAX_VALUES, MAX_VARIANTS = 2, 30, 200


def clean_attributes(value):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_ATTRIBUTES:
        raise APIError("Informe 1 ou 2 atributos (ex.: Cor e Tamanho)")
    result, names = [], set()
    for attribute in value:
        if not isinstance(attribute, dict) or set(attribute) != {"name", "values"}:
            raise APIError("Atributo inválido")
        name = text(attribute["name"], "Nome do atributo", 30)
        values = attribute["values"]
        if name.casefold() in names or not isinstance(values, list) or not 1 <= len(values) <= MAX_VALUES:
            raise APIError(f"Atributo {name}: informe de 1 a {MAX_VALUES} valores, sem repetir atributos")
        cleaned = [text(v, f"Valor de {name}", 30) for v in values]
        if len({v.casefold() for v in cleaned}) != len(cleaned):
            raise APIError(f"Atributo {name}: valores repetidos")
        names.add(name.casefold())
        result.append({"name": name, "values": cleaned})
    total = 1
    for attribute in result:
        total *= len(attribute["values"])
    if total > MAX_VARIANTS:
        raise APIError(f"A grade geraria {total} variações; o limite é {MAX_VARIANTS}")
    return result


def _combos(attributes):
    for values in combinations(*[a["values"] for a in attributes]):
        yield {a["name"]: v for a, v in zip(attributes, values)}


def _label(values, attributes):
    return " / ".join(values[a["name"]] for a in attributes)


def _key(values):
    return tuple(sorted((k.casefold(), v.casefold()) for k, v in values.items()))


def sync_variants(group, attributes, price_cents=None, defaults=None):
    """Create missing combinations, reactivate re-added ones, deactivate removed ones."""
    existing = {_key(p.variant_values): p for p in group.variants.all()}
    wanted = set()
    created = 0
    for values in _combos(attributes):
        key = _key(values)
        wanted.add(key)
        label = _label(values, attributes)
        row = existing.get(key)
        if row is None:
            if price_cents is None:
                raise APIError("Informe o preço para as novas variações")
            Product.objects.create(tenant=group.tenant, group=group, variant_values=values, variant=label,
                                   name=f"{group.name} - {label}"[:180], category=group.category,
                                   price_cents=price_cents, image_url=default_product_image(group.name,
                                   group.category.name if group.category else ""),
                                   **(defaults or {}), **generated_product_codes(group.tenant))
            created += 1
        elif not row.active or row.variant != label or row.name != f"{group.name} - {label}"[:180]:
            row.active, row.variant, row.name = True, label, f"{group.name} - {label}"[:180]
            row.save(update_fields=["active", "variant", "name", "updated_at"])
    removed = [p for key, p in existing.items() if key not in wanted and p.active]
    for row in removed:
        row.active = False
        row.save(update_fields=["active", "updated_at"])
    return created, len(removed)


def group_data(group, detail=False):
    variants = list(group.variants.order_by("variant", "id")) if detail else None
    result = {"id": str(group.id), "name": group.name,
              "category_id": str(group.category_id) if group.category_id else None,
              "attributes": group.attributes, "active": group.active,
              "variants_count": group.variants.filter(active=True).count()}
    if detail:
        result["variants"] = [product_data(p) for p in variants]
    return result


def _member(request, write=False):
    return membership_context(request, roles=CATALOG_WRITE if write else None, write=write)


@endpoint
def product_groups(request):
    method(request, "GET", "POST")
    member = _member(request, write=request.method == "POST")
    tenant = member.tenant
    if request.method == "GET":
        return page(request, ProductGroup.objects.filter(tenant=tenant).order_by("name", "id"), group_data)
    data = payload(request)
    fields(data, ("name", "category_id", "attributes", "price_cents", "ncm", "unit"), ("name", "attributes", "price_cents"))
    name = text(data["name"], "Nome da grade", 140)
    category = scoped(Category, tenant, data["category_id"]) if data.get("category_id") else None
    attributes = clean_attributes(data["attributes"])
    price = money(data["price_cents"], "Preço")
    ncm = text(data.get("ncm", ""), "NCM", 8, optional=True)
    if ncm and (not ncm.isdigit() or len(ncm) not in (7, 8)):
        raise APIError("NCM deve conter 7 ou 8 dígitos")
    unit = data.get("unit", "UN")
    if unit not in ("UN", "KG", "L"):
        raise APIError("Unidade inválida")
    try:
        with transaction.atomic():
            type(tenant).objects.select_for_update().get(pk=tenant.pk)
            group = ProductGroup.objects.create(tenant=tenant, name=name, category=category, attributes=attributes)
            created, _ = sync_variants(group, attributes, price, {"ncm": ncm, "unit": unit})
            audit(tenant, member.user, "product_group.created", group, {"variants": created})
    except IntegrityError:
        raise APIError("Já existe uma grade com esse nome", 409)
    return JsonResponse(group_data(group, detail=True), status=201)


@endpoint
def product_group_detail(request, pk):
    method(request, "GET", "PATCH")
    member = _member(request, write=request.method == "PATCH")
    group = scoped(ProductGroup, member.tenant, pk)
    if request.method == "GET":
        return JsonResponse(group_data(group, detail=True))
    data = payload(request)
    fields(data, ("name", "category_id", "attributes", "price_cents", "apply_price_to_all"))
    if not data:
        raise APIError("Informe ao menos um campo")
    price = money(data["price_cents"], "Preço") if "price_cents" in data else None
    try:
        with transaction.atomic():
            type(member.tenant).objects.select_for_update().get(pk=member.tenant_id)
            group = ProductGroup.objects.select_for_update().get(pk=group.pk)
            if "name" in data:
                group.name = text(data["name"], "Nome da grade", 140)
            if "category_id" in data:
                group.category = scoped(Category, member.tenant, data["category_id"]) if data["category_id"] else None
                group.variants.update(category=group.category)
            if "attributes" in data:
                group.attributes = clean_attributes(data["attributes"])
            group.save()
            template = group.variants.order_by("created_at").first()
            created, removed = sync_variants(group, group.attributes,
                                             price if price is not None else (template.price_cents if template else None),
                                             {"ncm": template.ncm, "unit": template.unit} if template else None)
            if price is not None and data.get("apply_price_to_all") is True:
                group.variants.update(price_cents=price)
            audit(member.tenant, member.user, "product_group.updated", group,
                  {"fields": list(data), "created": created, "deactivated": removed})
    except IntegrityError:
        raise APIError("Já existe uma grade com esse nome", 409)
    return JsonResponse(group_data(group, detail=True))
