"""Ready-made tax profiles for the most common retail situations.

They are starting points written for the shopkeeper; the accountant confirms
rates and codes. Rates that depend on the UF or on the product (ICMS internal
rate, MVA, ST retida) are left empty on purpose: the readiness check then
says exactly what is missing instead of inventing a number.
"""
from .schema import clean_fields

SIMPLES = (1, 4)
NORMAL = (2, 3)

PRESETS = [
    {
        "slug": "simples_tributado",
        "name": "Simples — tributado sem ST (CSOSN 102, CFOP 5102)",
        "crts": SIMPLES,
        "when": "Para a maioria das mercadorias revendidas por empresa do Simples Nacional ou MEI, quando o "
                "ICMS é pago no DAS e o produto não está na substituição tributária.",
        "fields": {"origin": 0, "cfop": "5102", "icms_csosn": "102",
                   "pis_cst": "49", "cofins_cst": "49"},
        "notes": "PIS e COFINS do Simples são recolhidos no DAS: CST 49 (ou 99) sem alíquota. Produtos "
                 "monofásicos (ex.: bebidas frias, cosméticos, autopeças) costumam usar CST 04; confirme com o contador.",
    },
    {
        "slug": "simples_st",
        "name": "Simples — com ST já recolhida (CSOSN 500, CFOP 5405)",
        "crts": SIMPLES,
        "when": "Para produtos comprados com o ICMS da substituição tributária já pago pelo fornecedor "
                "(ex.: refrigerantes, cervejas, cigarros, higiene). Exige CEST no produto.",
        "fields": {"origin": 0, "cfop": "5405", "icms_csosn": "500",
                   "pis_cst": "49", "cofins_cst": "49"},
        "notes": "Os valores da ST retida (base e pST) vêm da nota de compra e podem ser informados no produto; "
                 "algumas UFs os exigem na NFC-e.",
    },
    {
        "slug": "simples_isento",
        "name": "Simples — isento ou não tributado (CSOSN 400, CFOP 5102)",
        "crts": SIMPLES,
        "when": "Para mercadorias isentas ou fora do campo do ICMS na sua UF (ex.: hortifrutigranjeiros in natura "
                "em muitas UFs), vendidas por empresa do Simples Nacional.",
        "fields": {"origin": 0, "cfop": "5102", "icms_csosn": "400",
                   "pis_cst": "49", "cofins_cst": "49"},
        "notes": "Confirme com o contador se o caso é isenção (400) ou imunidade (300).",
    },
    {
        "slug": "normal_tributado",
        "name": "Normal — tributado integralmente (CST 00, CFOP 5102) · Lucro Presumido",
        "crts": NORMAL,
        "when": "Para mercadorias tributadas normalmente pelo ICMS, sem substituição tributária, em empresa do "
                "Lucro Presumido. Informe a alíquota interna de ICMS da sua UF (e o FCP, se houver).",
        "fields": {"origin": 0, "cfop": "5102", "icms_cst": "00", "icms_bc_mode": 3,
                   "pis_cst": "01", "pis_rate": "0.65", "cofins_cst": "01", "cofins_rate": "3"},
        "notes": "PIS 0,65% e COFINS 3% (regime cumulativo do Lucro Presumido).",
    },
    {
        "slug": "normal_tributado_real",
        "name": "Normal — tributado integralmente (CST 00, CFOP 5102) · Lucro Real",
        "crts": NORMAL,
        "when": "Igual ao anterior, para empresa do Lucro Real (PIS/COFINS não cumulativos).",
        "fields": {"origin": 0, "cfop": "5102", "icms_cst": "00", "icms_bc_mode": 3,
                   "pis_cst": "01", "pis_rate": "1.65", "cofins_cst": "01", "cofins_rate": "7.6"},
        "notes": "PIS 1,65% e COFINS 7,6% (regime não cumulativo do Lucro Real).",
    },
    {
        "slug": "normal_reducao",
        "name": "Normal — com redução de base (CST 20, CFOP 5102)",
        "crts": NORMAL,
        "when": "Para mercadorias com benefício de redução da base de cálculo do ICMS (ex.: itens da cesta básica "
                "em várias UFs). Informe a alíquota, o percentual de redução e o cBenef se a UF exigir.",
        "fields": {"origin": 0, "cfop": "5102", "icms_cst": "20", "icms_bc_mode": 3,
                   "pis_cst": "01", "pis_rate": "0.65", "cofins_cst": "01", "cofins_rate": "3"},
        "notes": "PIS/COFINS do Lucro Presumido; ajuste para o Lucro Real se for o caso.",
    },
    {
        "slug": "normal_st",
        "name": "Normal — ST já recolhida (CST 60, CFOP 5405)",
        "crts": NORMAL,
        "when": "Para produtos comprados com ICMS-ST já pago pelo fornecedor, em empresa do regime normal. "
                "Exige CEST no produto.",
        "fields": {"origin": 0, "cfop": "5405", "icms_cst": "60",
                   "pis_cst": "01", "pis_rate": "0.65", "cofins_cst": "01", "cofins_rate": "3"},
        "notes": "Informe no produto a base e o valor da ST retida da nota de compra quando a UF exigir.",
    },
    {
        "slug": "normal_isento",
        "name": "Normal — isento ou não tributado (CST 40, CFOP 5102)",
        "crts": NORMAL,
        "when": "Para mercadorias isentas de ICMS no regime normal. Use CST 41 para não tributadas. "
                "Várias UFs exigem o cBenef da isenção.",
        "fields": {"origin": 0, "cfop": "5102", "icms_cst": "40",
                   "pis_cst": "01", "pis_rate": "0.65", "cofins_cst": "01", "cofins_rate": "3"},
        "notes": "Se o produto também for isento de PIS/COFINS, use CST 07 para ambos.",
    },
]

BY_SLUG = {preset["slug"]: preset for preset in PRESETS}
# Shown under one name to the shopkeeper ("Isento/Não tributado"): picks the regime's variant.
ALIASES = {"isento": {1: "simples_isento", 4: "simples_isento", 2: "normal_isento", 3: "normal_isento"}}


def preset_for(slug, crt):
    slug = ALIASES.get(slug, {}).get(crt, slug)
    preset = BY_SLUG.get(slug)
    if not preset:
        raise KeyError(slug)
    return preset


def default_slug(crt):
    return "simples_tributado" if crt in SIMPLES else "normal_tributado"


def preset_fields(slug, crt):
    """Clean field dict of a preset (raises KeyError for an unknown slug)."""
    return clean_fields(dict(preset_for(slug, crt)["fields"]))


def public(crt=None):
    rows = []
    for preset in PRESETS:
        rows.append({"slug": preset["slug"], "name": preset["name"], "when": preset["when"],
                     "notes": preset["notes"], "fields": dict(preset["fields"]),
                     "applies": crt is None or crt in preset["crts"],
                     "regime": "simples" if preset["crts"] == SIMPLES else "normal"})
    return rows
