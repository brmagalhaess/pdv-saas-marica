"""Existing companies get a CRT and a default tax profile; products are linked to it.

- crt comes from the old text column tax_regime;
- each company gets one default profile from the preset of its regime;
- every product points at that profile and keeps its NCM;
- the old free-form tax_fields is read (only recognized keys with valid values)
  into fiscal_overrides; tax_fields itself is left untouched.

The presets and the legacy key map are frozen here on purpose: later edits to
shared/fiscal must not change what this migration did.
"""
from decimal import Decimal, InvalidOperation

from django.db import migrations

REGIME_CRT = {"simples_nacional": 1, "simples_excesso": 2, "regime_normal": 3, "mei": 4}
PRESETS = {
    "simples_tributado": ("Simples — tributado sem ST (CSOSN 102, CFOP 5102)",
                          {"origin": 0, "cfop": "5102", "icms_csosn": "102", "pis_cst": "49", "cofins_cst": "49"}),
    "normal_tributado": ("Normal — tributado integralmente (CST 00, CFOP 5102) · Lucro Presumido",
                         {"origin": 0, "cfop": "5102", "icms_cst": "00", "icms_bc_mode": 3, "pis_cst": "01",
                          "pis_rate": "0.65", "cofins_cst": "01", "cofins_rate": "3"}),
}
CODE = lambda sizes: (lambda v: v if v.isdigit() and len(v) in sizes else None)  # noqa: E731


def _rate(value):
    try:
        number = Decimal(str(value).replace(",", "."))
    except InvalidOperation:
        return None
    return format(number.normalize(), "f") if 0 <= number <= 100 and number == number.quantize(Decimal("0.0001")) else None


def _origin(value):
    text = str(value).strip()
    return int(text) if text.isdigit() and 0 <= int(text) <= 8 else None


LEGACY = {
    "cest": ("cest", CODE({7})), "cfop": ("cfop", CODE({4})),
    "csosn": ("icms_csosn", lambda v: v if v in {"101", "102", "103", "201", "202", "203", "300", "400", "500", "900"} else None),
    "icms_csosn": ("icms_csosn", lambda v: v if v in {"101", "102", "103", "201", "202", "203", "300", "400", "500", "900"} else None),
    "cst": ("icms_cst", lambda v: v if v in {"00", "10", "20", "30", "40", "41", "50", "51", "60", "70", "90"} else None),
    "cst_icms": ("icms_cst", lambda v: v if v in {"00", "10", "20", "30", "40", "41", "50", "51", "60", "70", "90"} else None),
    "icms_cst": ("icms_cst", lambda v: v if v in {"00", "10", "20", "30", "40", "41", "50", "51", "60", "70", "90"} else None),
    "origem": ("origin", _origin), "origin": ("origin", _origin), "orig": ("origin", _origin),
    "aliquota_icms": ("icms_rate", _rate), "icms_aliquota": ("icms_rate", _rate), "icms_rate": ("icms_rate", _rate),
    "picms": ("icms_rate", _rate),
    "pis_cst": ("pis_cst", CODE({2})), "cst_pis": ("pis_cst", CODE({2})),
    "cofins_cst": ("cofins_cst", CODE({2})), "cst_cofins": ("cofins_cst", CODE({2})),
    "aliquota_pis": ("pis_rate", _rate), "pis_rate": ("pis_rate", _rate),
    "aliquota_cofins": ("cofins_rate", _rate), "cofins_rate": ("cofins_rate", _rate),
    "ex_tipi": ("ex_tipi", CODE({1, 2, 3})), "extipi": ("ex_tipi", CODE({1, 2, 3})),
}


def legacy(tax_fields):
    result = {}
    if not isinstance(tax_fields, dict):
        return result
    for key, value in tax_fields.items():
        entry = LEGACY.get(str(key).strip().lower())
        if not entry or entry[0] in result or value is None:
            continue
        clean = entry[1](str(value).strip())
        if clean is not None:
            result[entry[0]] = clean
    if result.get("icms_cst") and result.get("icms_csosn"):
        result.pop("icms_cst")
    return result


def forwards(apps, schema_editor):
    Tenant = apps.get_model("core", "Tenant")
    TaxProfile = apps.get_model("core", "TaxProfile")
    Product = apps.get_model("core", "Product")
    for tenant in Tenant.objects.all().iterator():
        crt = REGIME_CRT.get(tenant.tax_regime, 1)
        if tenant.crt != crt:
            tenant.crt = crt
            tenant.save(update_fields=["crt"])
        profile = TaxProfile.objects.filter(tenant=tenant, is_default=True).first()
        if profile is None:
            slug = "simples_tributado" if crt in (1, 4) else "normal_tributado"
            name, fields = PRESETS[slug]
            profile = TaxProfile.objects.create(tenant=tenant, name=name, preset=slug, fields=dict(fields),
                                                is_default=True,
                                                description="Perfil padrão criado na atualização fiscal; revise com o contador.")
        Product.objects.filter(tenant=tenant, tax_profile__isnull=True).update(tax_profile=profile)
        for product in Product.objects.filter(tenant=tenant).exclude(tax_fields={}).only("id", "tax_fields", "fiscal_overrides"):
            found = legacy(product.tax_fields)
            if found:
                product.fiscal_overrides = {**found, **(product.fiscal_overrides or {})}
                product.save(update_fields=["fiscal_overrides"])


class Migration(migrations.Migration):
    dependencies = [("core", "0042_fiscal_profiles")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
