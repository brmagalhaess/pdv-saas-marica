"""Cloud side of the fiscal rules: profiles, resolution per product, issuer data.

The rules themselves live in shared/fiscal (pure Python); this module only
reads the database and hands plain dicts to it.
"""
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction

from shared.fiscal import compute_document, company_issues, product_issues, resolve, summary
from shared.fiscal.data import CRT_REGIME
from shared.fiscal.presets import default_slug, preset_fields, preset_for
from shared.fiscal.validators import cnpj_value, digits

from .models import IbptRate, TaxProfile


def rate_text(value):
    if value is None:
        return None
    return format(Decimal(value).normalize(), "f")


def ensure_default_profile(tenant):
    """The company's default profile, created from its regime preset when missing."""
    profile = TaxProfile.objects.filter(tenant=tenant, is_default=True).first()
    if profile:
        return profile
    slug = default_slug(tenant.crt)
    preset = preset_for(slug, tenant.crt)
    try:
        with transaction.atomic():
            existing = TaxProfile.objects.filter(tenant=tenant, name=preset["name"]).first()
            if existing:
                existing.is_default = True
                existing.active = True
                existing.save(update_fields=["is_default", "active", "updated_at"])
                return existing
            return TaxProfile.objects.create(tenant=tenant, name=preset["name"], preset=slug,
                                             fields=preset_fields(slug, tenant.crt), is_default=True,
                                             description=preset["when"])
    except IntegrityError:
        return TaxProfile.objects.get(tenant=tenant, is_default=True)


def profile_data(row, usage=None):
    result = {"id": str(row.id), "name": row.name, "description": row.description, "preset": row.preset,
              "fields": row.fields or {}, "is_default": row.is_default, "active": row.active,
              "updated_at": row.updated_at.isoformat() if row.updated_at else None}
    if usage is not None:
        result["products_count"] = usage
    return result


def issuer(tenant):
    return {"crt": tenant.crt, "uf": tenant.address_state or "",
            "simples_credit_rate": rate_text(tenant.simples_credit_rate)}


class FiscalContext:
    """Per-request cache: one default-profile lookup for a whole product list."""

    def __init__(self, tenant):
        self.tenant = tenant
        self._default = None

    @property
    def default_profile(self):
        if self._default is None:
            self._default = ensure_default_profile(self.tenant)
        return self._default

    def profile_of(self, product):
        if product.tax_profile_id:
            return product.tax_profile
        return self.default_profile

    def resolved(self, product):
        profile = self.profile_of(product)
        return resolve(profile.fields if profile else {}, product.ncm, product.fiscal_overrides, product.tax_fields)

    def status(self, product, model=65):
        fields = self.resolved(product)
        issues = product_issues(fields, self.tenant.crt, self.tenant.address_state or "", model)
        profile = self.profile_of(product)
        return {**summary(issues), "issues": issues,
                "profile_id": str(profile.id) if profile else None, "profile_name": profile.name if profile else ""}

    def terminal_payload(self, product):
        """Compact resolved data synced to the terminal (key `fiscal` of each product)."""
        fields = self.resolved(product)
        issues = product_issues(fields, self.tenant.crt, self.tenant.address_state or "", 65)
        state = summary(issues)
        profile = self.profile_of(product)
        return {"v": 1, "profile_id": str(profile.id) if profile else None, "fields": fields,
                "ready": state["ready"], "missing": state["missing"][:5]}


def company_data(tenant):
    return {"crt": tenant.crt, "tax_regime": tenant.tax_regime, "cnae": tenant.cnae,
            "special_tax_regime": tenant.special_tax_regime, "city_ibge_code": tenant.city_ibge_code,
            "simples_credit_rate": rate_text(tenant.simples_credit_rate),
            "approx_tax_fallback_rate": rate_text(tenant.approx_tax_fallback_rate)}


def company_status(tenant):
    values = {"document": tenant.document, "state_registration": tenant.state_registration,
              "address_state": tenant.address_state, "address_zip": tenant.address_zip,
              "address_line": tenant.address_line, "address_number": tenant.address_number,
              "address_neighborhood": tenant.address_neighborhood, "address_city": tenant.address_city,
              "city_ibge_code": tenant.city_ibge_code, "crt": tenant.crt, "cnae": tenant.cnae}
    issues = company_issues(values)
    return {**summary(issues), "issues": issues}


def tech_responsible():
    """Responsável técnico (grupo infRespTec): set by the platform through the environment."""
    return {"cnpj": cnpj_value(getattr(settings, "PDV_FISCAL_TECH_CNPJ", "")),
            "contact": getattr(settings, "PDV_FISCAL_TECH_CONTACT", ""),
            "email": getattr(settings, "PDV_FISCAL_TECH_EMAIL", ""),
            "phone": digits(getattr(settings, "PDV_FISCAL_TECH_PHONE", ""))}


def issuer_profile_extras(tenant):
    """Fiscal identity added to the terminal's issuer profile."""
    return {"crt": tenant.crt, "cnae": tenant.cnae, "city_ibge_code": tenant.city_ibge_code,
            "special_tax_regime": tenant.special_tax_regime,
            "simples_credit_rate": rate_text(tenant.simples_credit_rate),
            "fiscal_ready": company_status(tenant)["ready"], "tech_responsible": tech_responsible()}


def approx_lookup(uf, ncms, today=None):
    """Engine callable over the IBPT rows of the UF for the given NCMs (one query)."""
    today = today or date.today()
    rows = {}
    for row in IbptRate.objects.filter(uf=uf, ncm__in=set(ncms)):
        if row.valid_until and row.valid_until < today:
            continue
        rows[(row.ncm, row.ex)] = {"federal_national": row.federal_national, "federal_imported": row.federal_imported,
                                   "state": row.state, "municipal": row.municipal, "version": row.version}

    def lookup(ncm, ex=""):
        return rows.get((ncm, ex or "")) or rows.get((ncm, ""))
    return lookup


def simulate(tenant, lines, model=65, discount_cents=None):
    """Run the engine for products of the company. lines: [(product, quantity_milli, unit_price_cents, discount_cents)]."""
    context = FiscalContext(tenant)
    items = []
    for product, quantity, price, discount in lines:
        items.append({"product_id": str(product.id), "code": product.sku, "description": product.name,
                      "barcode": product.barcode, "unit": product.unit, "quantity_milli": quantity,
                      "unit_price_cents": price, "discount_cents": discount, "fiscal": context.resolved(product)})
    lookup = approx_lookup(tenant.address_state or "", [item["fiscal"].get("ncm", "") for item in items])
    return compute_document(issuer(tenant), items, model=model, discount_cents=discount_cents, approx=lookup,
                            approx_fallback=rate_text(tenant.approx_tax_fallback_rate))


def regime_for(crt):
    return CRT_REGIME[crt]

