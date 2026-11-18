"""Fiscal rule fields shared by tax profiles and product overrides.

A tax profile stores a dict of these fields; a product may override any of
them (`fiscal_overrides`). Values are normalized on input:
- codes are strings of digits ("0102" never becomes 102);
- rates are percentages as decimal strings with up to 4 places ("18", "0.65");
- unit money references (ST retida) are reais as decimal strings, up to 4 places.
Clean values are JSON-safe and identical in the cloud, the terminal and tests.
"""
import re
from decimal import Decimal, InvalidOperation

from . import data
from .validators import digits, valid_cbenef, valid_cest, valid_cfop, valid_gtin, valid_ncm


class FiscalFieldError(ValueError):
    def __init__(self, field, message):
        super().__init__(message)
        self.field = field
        self.message = message


def _choice(options):
    def clean(value, label):
        value = str(value).strip()
        if value not in options:
            raise ValueError(f"{label} inválido: use um dos códigos {', '.join(sorted(options))}")
        return value
    return clean


def _int_range(low, high):
    def clean(value, label):
        if isinstance(value, bool):
            raise ValueError(f"{label} inválido")
        try:
            number = int(str(value).strip())
        except ValueError:
            raise ValueError(f"{label} inválido")
        if not low <= number <= high:
            raise ValueError(f"{label} deve ficar entre {low} e {high}")
        return number
    return clean


def _decimal(low, high, places=4):
    def clean(value, label):
        if isinstance(value, bool):
            raise ValueError(f"{label} inválido")
        raw = str(value).strip().replace(",", ".")
        try:
            number = Decimal(raw)
        except InvalidOperation:
            raise ValueError(f"{label} inválido")
        if not number.is_finite() or not Decimal(low) <= number <= Decimal(high):
            raise ValueError(f"{label} deve ficar entre {low} e {high}")
        if number != number.quantize(Decimal(1).scaleb(-places)):
            raise ValueError(f"{label} aceita no máximo {places} casas decimais")
        text = format(number.normalize(), "f")
        return text
    return clean


def _digits(sizes, check=None, message=None):
    def clean(value, label):
        value = str(value).strip()
        plain = digits(value)
        if plain != value.replace(".", "").replace("-", "") or len(plain) not in sizes or (check and not check(plain)):
            raise ValueError(message or f"{label} inválido")
        return plain
    return clean


def _bool(value, label):
    if type(value) is not bool:
        raise ValueError(f"{label} inválido")
    return value


def _unit(value, label):
    value = str(value).strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{1,6}", value):
        raise ValueError(f"{label}: use até 6 letras ou números, ex.: UN, KG, CX")
    return value


def _cbenef(value, label):
    value = str(value).strip().upper()
    if not valid_cbenef(value):
        raise ValueError("Código de benefício fiscal (cBenef) inválido: 2 letras da UF + 6 ou 8 caracteres")
    return value


def _gtin(value, label):
    value = str(value).strip()
    if value.upper() == "SEM GTIN":
        return "SEM GTIN"
    if not valid_gtin(value):
        raise ValueError("GTIN tributável inválido: informe um EAN com dígito verificador correto ou SEM GTIN")
    return value


RATE = _decimal(0, 100)
# name: (label, cleaner)
FIELDS = {
    "origin": ("Origem da mercadoria", _int_range(0, 8)),
    "ncm": ("NCM", _digits({8}, valid_ncm, "NCM deve ter 8 dígitos")),
    "ex_tipi": ("EX TIPI", _digits({1, 2, 3}, message="EX TIPI deve ter de 1 a 3 dígitos")),
    "cest": ("CEST", _digits({7}, valid_cest, "CEST deve ter 7 dígitos (segmento 01 a 28)")),
    "cfop": ("CFOP", _digits({4}, valid_cfop, "CFOP deve ter 4 dígitos, ex.: 5102")),
    "icms_cst": ("CST do ICMS", _choice(data.ICMS_CST)),
    "icms_csosn": ("CSOSN", _choice(data.ICMS_CSOSN)),
    "icms_bc_mode": ("Modalidade da base do ICMS", _int_range(0, 3)),
    "icms_rate": ("Alíquota do ICMS (%)", RATE),
    "icms_bc_reduction": ("Redução da base do ICMS (%)", RATE),
    "icms_deferral_rate": ("Percentual do diferimento (%)", RATE),
    "fcp_rate": ("Alíquota do FCP (%)", RATE),
    "st_bc_mode": ("Modalidade da base do ICMS ST", _int_range(0, 6)),
    "st_mva": ("MVA da ST (%)", _decimal(0, 999)),
    "st_rate": ("Alíquota do ICMS ST (%)", RATE),
    "st_bc_reduction": ("Redução da base do ICMS ST (%)", RATE),
    "st_fcp_rate": ("Alíquota do FCP ST (%)", RATE),
    "st_retained_bc_unit": ("Base da ST retida por unidade (R$)", _decimal(0, 999999999)),
    "st_retained_rate": ("Alíquota suportada pelo consumidor final (pST, %)", RATE),
    "st_retained_substitute_unit": ("ICMS próprio do substituto por unidade (R$)", _decimal(0, 999999999)),
    "st_retained_fcp_rate": ("Alíquota do FCP ST retido (%)", RATE),
    "cbenef": ("Código de benefício fiscal (cBenef)", _cbenef),
    "pis_cst": ("CST do PIS", _choice(data.PIS_COFINS_CST)),
    "pis_rate": ("Alíquota do PIS (%)", RATE),
    "cofins_cst": ("CST da COFINS", _choice(data.PIS_COFINS_CST)),
    "cofins_rate": ("Alíquota da COFINS (%)", RATE),
    "pis_cofins_exclude_icms": ("Excluir o ICMS da base do PIS/COFINS", _bool),
    "ipi_cst": ("CST do IPI", _choice(data.IPI_CST)),
    "ipi_rate": ("Alíquota do IPI (%)", RATE),
    "ipi_enquadramento": ("Código de enquadramento do IPI", _digits({3}, message="Enquadramento do IPI deve ter 3 dígitos (999 = sem enquadramento)")),
    "tax_unit": ("Unidade tributável", _unit),
    "tax_unit_factor": ("Fator de conversão da unidade tributável", _decimal("0.0001", 1000000)),
    "tax_gtin": ("GTIN da unidade tributável", _gtin),
    "ibs_cbs_cst": ("CST do IBS/CBS", _digits({3}, message="CST do IBS/CBS deve ter 3 dígitos")),
    "ibs_cbs_class": ("Classificação tributária do IBS/CBS (cClassTrib)", _digits({6}, message="cClassTrib deve ter 6 dígitos")),
    "ibs_state_rate": ("Alíquota do IBS estadual (%)", RATE),
    "ibs_city_rate": ("Alíquota do IBS municipal (%)", RATE),
    "cbs_rate": ("Alíquota da CBS (%)", RATE),
    "ibs_cbs_base_excludes_taxes": ("Excluir ICMS/PIS/COFINS da base do IBS/CBS", _bool),
}

# Values every resolution starts from (a profile only needs what differs).
DEFAULTS = {
    "origin": 0, "icms_bc_mode": 3, "st_bc_mode": 4, "ipi_enquadramento": "999",
    "pis_cofins_exclude_icms": False, "ibs_cbs_base_excludes_taxes": True,
}

# Product-level fields that never come from a profile.
PRODUCT_ONLY = {"tax_gtin"}

# Keys found in the free-form tax_fields of older versions -> field.
LEGACY_KEYS = {
    "cest": "cest", "cfop": "cfop", "csosn": "icms_csosn", "icms_csosn": "icms_csosn",
    "cst": "icms_cst", "cst_icms": "icms_cst", "icms_cst": "icms_cst",
    "origem": "origin", "origin": "origin", "orig": "origin",
    "aliquota_icms": "icms_rate", "icms_aliquota": "icms_rate", "icms_rate": "icms_rate", "picms": "icms_rate",
    "pis_cst": "pis_cst", "cst_pis": "pis_cst", "cofins_cst": "cofins_cst", "cst_cofins": "cofins_cst",
    "aliquota_pis": "pis_rate", "pis_rate": "pis_rate", "aliquota_cofins": "cofins_rate", "cofins_rate": "cofins_rate",
    "cbenef": "cbenef", "ex_tipi": "ex_tipi", "extipi": "ex_tipi",
    "unidade_tributavel": "tax_unit", "utrib": "tax_unit",
}


def clean_fields(raw, allow=None):
    """Validate a dict of fiscal fields. Empty strings / None remove the key.

    Raises FiscalFieldError with a Portuguese message naming the field.
    """
    if not isinstance(raw, dict):
        raise FiscalFieldError("", "Dados fiscais inválidos")
    allowed = set(FIELDS) if allow is None else set(allow)
    result = {}
    for key, value in raw.items():
        if key not in allowed:
            raise FiscalFieldError(key, f"Campo fiscal desconhecido: {key}")
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        label, cleaner = FIELDS[key]
        try:
            result[key] = cleaner(value, label)
        except ValueError as exc:
            raise FiscalFieldError(key, str(exc))
    if result.get("icms_cst") and result.get("icms_csosn"):
        raise FiscalFieldError("icms_csosn", "Informe CST (regime normal) ou CSOSN (Simples Nacional), não os dois")
    return result


def legacy_fields(tax_fields):
    """Best-effort reading of the old free-form tax_fields; invalid values are skipped."""
    if not isinstance(tax_fields, dict):
        return {}
    result = {}
    for key, value in tax_fields.items():
        field = LEGACY_KEYS.get(str(key).strip().lower())
        if not field or field in result:
            continue
        try:
            result.update(clean_fields({field: value}))
        except FiscalFieldError:
            continue
    if result.get("icms_cst") and result.get("icms_csosn"):
        result.pop("icms_cst")
    return result


def resolve(profile_fields=None, product_ncm="", overrides=None, legacy=None):
    """Layering: defaults < profile < legacy tax_fields < product overrides < product NCM column."""
    result = dict(DEFAULTS)
    for layer in (profile_fields or {}, legacy_fields(legacy), overrides or {}):
        for key, value in layer.items():
            if key in ("icms_cst", "icms_csosn"):
                # Picking one regime code in a layer replaces the other one below it.
                result.pop("icms_csosn" if key == "icms_cst" else "icms_cst", None)
            result[key] = value
    if product_ncm:
        result["ncm"] = product_ncm
    return result
