"""Tax profiles, fiscal reference tables, readiness, tax simulation and the product CSV import."""
import csv
import io
import unicodedata
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import Count
from django.http import JsonResponse

from shared.fiscal import FiscalFieldError, clean_fields
from shared.fiscal import data as fiscal_data
from shared.fiscal.presets import preset_for, preset_fields, public as public_presets
from shared.fiscal.schema import FIELDS, PRODUCT_ONLY

from .api_common import APIError, audit, endpoint, fields as check_fields, membership_context, method, page, payload, scoped, text
from .fiscal import FiscalContext, company_status, ensure_default_profile, profile_data, simulate
from .models import Category, Product, TaxProfile
from .nfe import valid_gtin

TAX_READ = {"owner", "admin", "manager", "inventory", "finance", "viewer"}
TAX_WRITE = {"owner", "admin", "manager", "finance"}
CATALOG_WRITE = {"owner", "admin", "manager", "inventory"}
PROFILE_FIELDS = set(FIELDS) - PRODUCT_ONLY
# Product overrides: everything but the NCM, which is a product column.
OVERRIDE_FIELDS = set(FIELDS) - {"ncm"}


def clean_profile_fields(value):
    try:
        return clean_fields(value, PROFILE_FIELDS)
    except FiscalFieldError as exc:
        raise APIError(exc.message, extra={"field": exc.field})


def clean_overrides(value):
    try:
        return clean_fields(value, OVERRIDE_FIELDS)
    except FiscalFieldError as exc:
        raise APIError(exc.message, extra={"field": exc.field})


def _usage(tenant):
    rows = (Product.objects.filter(tenant=tenant, active=True, tax_profile__isnull=False)
            .values("tax_profile").annotate(total=Count("id")))
    return {row["tax_profile"]: row["total"] for row in rows}


@endpoint
def tax_profiles(request):
    method(request, "GET", "POST")
    member = membership_context(request, TAX_READ if request.method == "GET" else TAX_WRITE,
                                write=request.method == "POST")
    tenant = member.tenant
    if request.method == "GET":
        ensure_default_profile(tenant)
        rows = TaxProfile.objects.filter(tenant=tenant)
        if request.GET.get("include_inactive") != "1":
            rows = rows.filter(active=True)
        usage = _usage(tenant)
        return page(request, rows.order_by("-is_default", "name", "id"), lambda row: profile_data(row, usage.get(row.id, 0)))
    data = payload(request)
    check_fields(data, ("name", "description", "preset", "fields", "is_default"))
    preset = text(data.get("preset"), "Modelo", 40, optional=True)
    values = {}
    name = ""
    if preset:
        try:
            chosen = preset_for(preset, tenant.crt)
        except KeyError:
            raise APIError("Modelo de perfil tributário desconhecido")
        values = preset_fields(preset, tenant.crt)
        name = chosen["name"]
        preset = chosen["slug"]
    if "fields" in data:
        if not isinstance(data["fields"], dict) or len(str(data["fields"])) > 8000:
            raise APIError("Dados fiscais inválidos")
        values.update(clean_profile_fields(data["fields"]))
        if "icms_cst" in data["fields"] and data["fields"]["icms_cst"]:
            values.pop("icms_csosn", None)
        if "icms_csosn" in data["fields"] and data["fields"]["icms_csosn"]:
            values.pop("icms_cst", None)
    name = text(data.get("name") or name, "Nome do perfil", 120)
    description = text(data.get("description"), "Descrição", 500, optional=True) or (chosen["when"] if preset else "")
    make_default = data.get("is_default", False)
    if type(make_default) is not bool:
        raise APIError("Indicação de perfil padrão inválida")
    try:
        with transaction.atomic():
            type(tenant).objects.select_for_update().get(pk=tenant.pk)
            if make_default:
                TaxProfile.objects.filter(tenant=tenant, is_default=True).update(is_default=False)
            row = TaxProfile.objects.create(tenant=tenant, name=name, description=description, preset=preset,
                                            fields=values, is_default=make_default)
            audit(tenant, member.user, "tax_profile.created", row, {"preset": preset})
    except IntegrityError:
        raise APIError("Já existe um perfil tributário com este nome", 409)
    return JsonResponse(profile_data(row, 0), status=201)


@endpoint
def tax_profile_detail(request, pk):
    method(request, "GET", "PATCH", "DELETE")
    member = membership_context(request, TAX_READ if request.method == "GET" else TAX_WRITE,
                                write=request.method != "GET")
    tenant = member.tenant
    row = scoped(TaxProfile, tenant, pk)
    if request.method == "GET":
        return JsonResponse(profile_data(row, _usage(tenant).get(row.id, 0)))
    data = {"active": False} if request.method == "DELETE" else payload(request)
    check_fields(data, ("name", "description", "fields", "is_default", "active"))
    if not data:
        raise APIError("Informe ao menos um campo")
    try:
        with transaction.atomic():
            type(tenant).objects.select_for_update().get(pk=tenant.pk)
            row = TaxProfile.objects.select_for_update().get(pk=row.pk)
            changed = []
            if "name" in data:
                row.name = text(data["name"], "Nome do perfil", 120); changed.append("name")
            if "description" in data:
                row.description = text(data["description"], "Descrição", 500, optional=True); changed.append("description")
            if "fields" in data:
                if not isinstance(data["fields"], dict) or len(str(data["fields"])) > 8000:
                    raise APIError("Dados fiscais inválidos")
                row.fields = clean_profile_fields(data["fields"]); changed.append("fields")
            if "is_default" in data:
                if data["is_default"] is not True:
                    raise APIError("Para trocar o perfil padrão, marque outro perfil como padrão", 409)
                if not row.active and data.get("active") is not True:
                    raise APIError("Reative o perfil antes de torná-lo padrão", 409)
                TaxProfile.objects.filter(tenant=tenant, is_default=True).exclude(pk=row.pk).update(is_default=False)
                row.is_default = True; changed.append("is_default")
            if "active" in data:
                if type(data["active"]) is not bool:
                    raise APIError("Estado inválido")
                if not data["active"]:
                    if row.is_default:
                        raise APIError("O perfil padrão não pode ser desativado; escolha outro padrão antes", 409)
                    in_use = Product.objects.filter(tax_profile=row, active=True).count()
                    if in_use:
                        raise APIError(f"Perfil em uso por {in_use} produto(s) ativo(s); mude o perfil desses produtos antes", 409)
                row.active = data["active"]; changed.append("active")
            row.save(update_fields=[*changed, "updated_at"])
            audit(tenant, member.user, "tax_profile.updated", row, {"fields": changed})
    except IntegrityError:
        raise APIError("Já existe um perfil tributário com este nome", 409)
    return JsonResponse(profile_data(row, _usage(tenant).get(row.id, 0)))


@endpoint
def tax_presets(request):
    method(request, "GET")
    member = membership_context(request, TAX_READ)
    return JsonResponse({"crt": member.tenant.crt, "results": public_presets(member.tenant.crt)})


def _table(mapping):
    return [{"code": str(code), "label": label} for code, label in mapping.items()]


@endpoint
def reference(request):
    method(request, "GET")
    membership_context(request)
    return JsonResponse({
        "crt": _table(fiscal_data.CRT), "origins": _table(fiscal_data.ORIGINS),
        "icms_cst": _table(fiscal_data.ICMS_CST), "icms_csosn": _table(fiscal_data.ICMS_CSOSN),
        "icms_bc_modes": _table(fiscal_data.ICMS_BC_MODES), "st_bc_modes": _table(fiscal_data.ST_BC_MODES),
        "pis_cofins_cst": _table(fiscal_data.PIS_COFINS_CST), "ipi_cst": _table(fiscal_data.IPI_CST),
        "nfce_cfops": _table(fiscal_data.NFCE_CFOPS), "special_tax_regimes": _table(fiscal_data.SPECIAL_TAX_REGIMES),
        "ufs": [{"code": uf, "ibge": code} for uf, code in fiscal_data.UF_CODES.items()],
        "fields": [{"key": key, "label": label} for key, (label, _) in FIELDS.items()],
    })


@endpoint
def status(request):
    """Company readiness plus how many active products can already go on a NFC-e."""
    method(request, "GET")
    member = membership_context(request, TAX_READ)
    tenant = member.tenant
    context = FiscalContext(tenant)
    ready = pending = 0
    reasons = {}
    for product in Product.objects.filter(tenant=tenant, active=True).select_related("tax_profile").iterator():
        state = context.status(product)
        if state["ready"]:
            ready += 1
        else:
            pending += 1
            for message in state["missing"]:
                reasons[message] = reasons.get(message, 0) + 1
    top = sorted(reasons.items(), key=lambda item: (-item[1], item[0]))[:8]
    return JsonResponse({"company": company_status(tenant), "crt": tenant.crt,
                         "fiscal_environment": tenant.fiscal_environment,
                         "products": {"ready": ready, "pending": pending,
                                      "top_missing": [{"message": m, "count": c} for m, c in top]}})


@endpoint
def simulate_view(request):
    method(request, "POST")
    member = membership_context(request, TAX_READ)
    tenant = member.tenant
    data = payload(request)
    check_fields(data, ("items", "discount_cents", "model"), ("items",))
    model = data.get("model", 65)
    if model not in (55, 65):
        raise APIError("Modelo de nota inválido: use 65 (NFC-e) ou 55 (NF-e)")
    items = data["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= 990:
        raise APIError("Informe de 1 a 990 itens")
    discount = data.get("discount_cents")
    if discount is not None and (type(discount) is not int or discount < 0):
        raise APIError("Desconto inválido")
    lines = []
    for item in items:
        if not isinstance(item, dict):
            raise APIError("Item inválido")
        check_fields(item, ("product_id", "quantity_milli", "unit_price_cents", "discount_cents"), ("product_id", "quantity_milli"))
        product = scoped(Product, tenant, item["product_id"])
        quantity = item["quantity_milli"]
        price = item.get("unit_price_cents", product.price_cents)
        item_discount = item.get("discount_cents", 0)
        if type(quantity) is not int or not 0 < quantity <= 10**9:
            raise APIError("Quantidade inválida")
        if type(price) is not int or not 0 <= price <= 10**12 or type(item_discount) is not int or item_discount < 0:
            raise APIError("Valor ou desconto do item inválido")
        lines.append((product, quantity, price, item_discount))
    try:
        result = simulate(tenant, lines, model, discount)
    except ValueError as exc:
        raise APIError(str(exc))
    return JsonResponse(result)


# ------------------------------------------------------------------ CSV import

def _key(header):
    plain = unicodedata.normalize("NFKD", str(header or "")).encode("ascii", "ignore").decode().strip().lower()
    return "".join(c if c.isalnum() else "_" for c in plain).strip("_")


COLUMN_ALIASES = {
    "nome": "name", "produto": "name", "descricao": "name", "name": "name",
    "sku": "sku", "codigo": "sku", "codigo_interno": "sku",
    "ean": "barcode", "gtin": "barcode", "codigo_de_barras": "barcode", "codigo_barras": "barcode", "barcode": "barcode",
    "preco": "price", "preco_venda": "price", "preco_de_venda": "price", "valor": "price",
    "custo": "cost", "unidade": "unit", "un": "unit", "categoria": "category", "ativo": "active",
    "ncm": "ncm", "perfil": "tax_profile", "perfil_tributario": "tax_profile", "grupo_tributario": "tax_profile",
    "cest": "cest", "cfop": "cfop", "origem": "origin", "cst": "icms_cst", "cst_icms": "icms_cst", "csosn": "icms_csosn",
    "aliquota_icms": "icms_rate", "icms": "icms_rate", "reducao_bc": "icms_bc_reduction", "reducao_base": "icms_bc_reduction",
    "fcp": "fcp_rate", "aliquota_fcp": "fcp_rate", "cbenef": "cbenef", "ex_tipi": "ex_tipi",
    "cst_pis": "pis_cst", "pis_cst": "pis_cst", "aliquota_pis": "pis_rate", "pis": "pis_rate",
    "cst_cofins": "cofins_cst", "cofins_cst": "cofins_cst", "aliquota_cofins": "cofins_rate", "cofins": "cofins_rate",
    "unidade_tributavel": "tax_unit", "fator_tributavel": "tax_unit_factor", "fator_conversao": "tax_unit_factor",
    "gtin_tributavel": "tax_gtin", "ean_tributavel": "tax_gtin",
    "bc_st_retido_unitario": "st_retained_bc_unit", "pst": "st_retained_rate", "icms_substituto_unitario": "st_retained_substitute_unit",
    "cclasstrib": "ibs_cbs_class", "cst_ibs_cbs": "ibs_cbs_cst",
}
FISCAL_COLUMNS = {value for value in COLUMN_ALIASES.values() if value in OVERRIDE_FIELDS}
MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ROWS = 5000


def _money_cents(value, label):
    raw = str(value).strip().replace("R$", "").replace(" ", "")
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        number = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"{label} inválido: {value}")
    if not number.is_finite() or number < 0 or number != number.quantize(Decimal("0.01")) or number > 10**10:
        raise ValueError(f"{label} inválido: {value}")
    return int(number * 100)


def _code_text(value):
    # Spreadsheets drop leading zeros of codes stored as numbers ("5102" is fine, "060" may become "60").
    return str(value).strip().replace(".", "") if value is not None else ""


def parse_import(text_value):
    if not isinstance(text_value, str) or not text_value.strip():
        raise APIError("Envie o conteúdo do arquivo CSV")
    if len(text_value.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise APIError("Arquivo maior que 2 MB")
    text_value = text_value.lstrip("﻿")
    first = text_value.splitlines()[0]
    delimiter = ";" if first.count(";") >= first.count(",") else ","
    reader = csv.reader(io.StringIO(text_value), delimiter=delimiter)
    header = next(reader, [])
    columns = [COLUMN_ALIASES.get(_key(name), "") for name in header]
    unknown = [name for name, column in zip(header, columns) if name.strip() and not column]
    if "name" not in columns and "sku" not in columns and "barcode" not in columns:
        raise APIError("O CSV precisa de uma coluna nome, sku ou ean")
    rows = []
    for line_number, cells in enumerate(reader, start=2):
        if not any(cell.strip() for cell in cells):
            continue
        rows.append((line_number, {column: cell.strip() for column, cell in zip(columns, cells) if column}))
        if len(rows) > MAX_IMPORT_ROWS:
            raise APIError(f"O CSV pode ter no máximo {MAX_IMPORT_ROWS} produtos por vez")
    return rows, unknown


def _row_values(row, tenant, profiles, categories):
    values, overrides = {}, {}
    if row.get("name"):
        if len(row["name"]) > 180:
            raise ValueError("Nome com mais de 180 caracteres")
        values["name"] = row["name"]
    if row.get("price"):
        values["price_cents"] = _money_cents(row["price"], "Preço")
    if row.get("cost"):
        values["cost_cents"] = _money_cents(row["cost"], "Custo")
    if row.get("unit"):
        unit = row["unit"].upper()
        if unit not in ("UN", "KG", "L"):
            raise ValueError("Unidade deve ser UN, KG ou L")
        values["unit"] = unit
    if row.get("barcode"):
        if not valid_gtin(row["barcode"]):
            raise ValueError(f"EAN inválido: {row['barcode']}")
        values["barcode"] = row["barcode"]
    if row.get("ncm"):
        ncm = _code_text(row["ncm"])
        if not (ncm.isdigit() and len(ncm) == 8):
            raise ValueError(f"NCM inválido: {row['ncm']} (8 dígitos)")
        values["ncm"] = ncm
    if row.get("category"):
        category = categories.get(row["category"].casefold())
        if category is None:
            raise ValueError(f"Categoria não encontrada: {row['category']}")
        values["category"] = category
    if row.get("active"):
        flag = _key(row["active"])
        if flag not in ("sim", "s", "1", "true", "ativo", "nao", "n", "0", "false", "inativo"):
            raise ValueError("Ativo deve ser sim ou não")
        values["active"] = flag in ("sim", "s", "1", "true", "ativo")
    if row.get("tax_profile"):
        profile = profiles.get(row["tax_profile"].casefold())
        if profile is None:
            raise ValueError(f"Perfil tributário não encontrado: {row['tax_profile']}")
        values["tax_profile"] = profile
    raw = {key: _code_text(row[key]) if key in ("cest", "cfop", "ex_tipi", "pis_cst", "cofins_cst", "ibs_cbs_cst", "ibs_cbs_class") else row[key]
           for key in FISCAL_COLUMNS if row.get(key)}
    if raw.get("icms_cst") and len(raw["icms_cst"]) == 1:
        raw["icms_cst"] = raw["icms_cst"].zfill(2)
    for key in ("pis_cst", "cofins_cst"):
        if raw.get(key) and len(raw[key]) == 1:
            raw[key] = raw[key].zfill(2)
    if raw:
        try:
            overrides = clean_fields(raw, OVERRIDE_FIELDS)
        except FiscalFieldError as exc:
            raise ValueError(exc.message)
    return values, overrides


@endpoint
def products_import(request):
    """Create or update products from a CSV (catalog + fiscal columns). Match: SKU, then EAN.

    {"csv": "...", "dry_run": true} previews; without dry_run, nothing is written when a line has errors.
    """
    method(request, "POST")
    member = membership_context(request, CATALOG_WRITE, write=True)
    tenant = member.tenant
    data = payload(request)
    check_fields(data, ("csv", "dry_run"), ("csv",))
    dry_run = data.get("dry_run", False)
    if type(dry_run) is not bool:
        raise APIError("Indicação de simulação inválida")
    rows, unknown = parse_import(data["csv"])
    if not rows:
        raise APIError("O CSV não tem produtos")
    profiles = {p.name.casefold(): p for p in TaxProfile.objects.filter(tenant=tenant, active=True)}
    categories = {c.name.casefold(): c for c in Category.objects.filter(tenant=tenant, active=True)}
    default_profile = ensure_default_profile(tenant)
    plan, errors, seen = [], [], set()
    for line, row in rows:
        try:
            values, overrides = _row_values(row, tenant, profiles, categories)
            product = None
            if row.get("sku"):
                product = Product.objects.filter(tenant=tenant, sku=row["sku"].upper()).first()
            if product is None and values.get("barcode"):
                product = Product.objects.filter(tenant=tenant, barcode=values["barcode"]).first()
            if product is None and row.get("sku"):
                raise ValueError(f"SKU {row['sku']} não encontrado; para cadastrar, deixe o SKU vazio")
            key = product.pk if product else ("new", values.get("barcode") or line)
            if key in seen:
                raise ValueError("Produto repetido no arquivo")
            seen.add(key)
            if product is None:
                if "name" not in values or "price_cents" not in values:
                    raise ValueError("Produto novo precisa de nome e preço")
            elif values.get("barcode"):
                clash = Product.objects.filter(tenant=tenant, barcode=values["barcode"]).exclude(pk=product.pk).exists()
                if clash:
                    raise ValueError("EAN já cadastrado em outro produto")
            if "unit" in values and product is not None and values["unit"] != product.unit:
                raise ValueError("A unidade de um produto existente não é alterada pela importação")
            if "cost_cents" in values and product is not None:
                values.pop("cost_cents")  # cost of existing products comes from stock receipts
            plan.append((line, product, values, overrides))
        except ValueError as exc:
            errors.append({"line": line, "message": str(exc)})
    created = sum(1 for _, product, _, _ in plan if product is None)
    summary_body = {"dry_run": dry_run, "rows": len(rows), "created": created, "updated": len(plan) - created,
                    "errors": errors[:200], "ignored_columns": unknown}
    if dry_run or errors:
        return JsonResponse(summary_body, status=200 if dry_run else 400)
    from .views_resources import default_product_image, generated_product_codes
    try:
        with transaction.atomic():
            type(tenant).objects.select_for_update().get(pk=tenant.pk)
            for line, product, values, overrides in plan:
                if product is None:
                    codes = generated_product_codes(tenant)
                    values.setdefault("tax_profile", default_profile)
                    category = values.get("category")
                    Product.objects.create(tenant=tenant, sku=codes["sku"], barcode=values.pop("barcode", "") or codes["barcode"],
                                           image_url=default_product_image(values["name"], category.name if category else ""),
                                           fiscal_overrides=overrides, **values)
                else:
                    product = Product.objects.select_for_update().get(pk=product.pk)
                    for field, value in values.items():
                        setattr(product, field, value)
                    if overrides:
                        product.fiscal_overrides = {**(product.fiscal_overrides or {}), **overrides}
                        if overrides.get("icms_cst"):
                            product.fiscal_overrides.pop("icms_csosn", None)
                        if overrides.get("icms_csosn"):
                            product.fiscal_overrides.pop("icms_cst", None)
                    product.save()
            audit(tenant, member.user, "products.imported", None,
                  {"created": created, "updated": len(plan) - created})
    except IntegrityError:
        raise APIError("Conflito de código ao importar; tente novamente", 409)
    return JsonResponse(summary_body)
