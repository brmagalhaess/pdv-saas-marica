"""Deterministic tax calculation for NFC-e (modelo 65) and NF-e (modelo 55), layout 4.00.

Pure Python, no I/O. Money is integer cents, quantity is integer milliunits,
rates are percentages given as decimal strings ("18", "0.65").

Rounding (documented in docs/FISCAL.md): every monetary value of an item is
computed with Decimal from the exact operands and rounded ONCE to cents with
ROUND_HALF_UP (the SEFAZ checks vProd = qCom x vUnCom and vICMS = vBC x pICMS
with a R$ 0,01 tolerance, and HALF_UP is the commercial rounding used by the
PDV2 terminal for the line value). Document totals are the plain sums of the
already-rounded item values, so sum(items) == totals always holds — the SEFAZ
rejects totals that differ from the sum of the items.

Contract: compute_document(issuer, items, ...) -> dict with "schema": SCHEMA.
See docs/FISCAL.md for the field-by-field mapping to the NF-e XML tags.
"""
from decimal import ROUND_HALF_UP, Decimal

from . import data
from .readiness import has_errors, product_issues

SCHEMA = "pdv2.fiscal.v1"
HUNDRED = Decimal(100)
SIMPLES_CRTS = (1, 4)


def _round(value):
    return int(Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _dec(value, default=None):
    if value is None or value == "":
        return default
    return Decimal(str(value))


def pct(base_cents, rate):
    """Rate (percent) applied to a value in cents, rounded HALF_UP to the cent."""
    rate = _dec(rate, Decimal(0))
    return _round(Decimal(base_cents) * rate / HUNDRED)


def reduce_base(base_cents, reduction):
    reduction = _dec(reduction, Decimal(0))
    return _round(Decimal(base_cents) * (HUNDRED - reduction) / HUNDRED)


def line_gross(unit_price_cents, quantity_milli):
    """vProd: same formula as the terminal (terminal/store.py _line_gross)."""
    return _round(Decimal(unit_price_cents) * Decimal(quantity_milli) / Decimal(1000))


def allocate_discount(total_cents, values):
    """Split a document discount over the item values (largest remainder, ties to the first item).

    Mirrors the terminal: never takes an item below zero and always sums to `total_cents`.
    """
    base = sum(values)
    if total_cents < 0 or total_cents > base:
        raise ValueError("Desconto maior que o valor dos itens")
    if not total_cents:
        return [0 for _ in values]
    shares = [divmod(total_cents * value, base) for value in values]
    allocated = [share[0] for share in shares]
    remaining = total_cents - sum(allocated)
    for index in sorted(range(len(values)), key=lambda i: (-shares[i][1], i)):
        if not remaining:
            break
        if allocated[index] < values[index]:
            allocated[index] += 1
            remaining -= 1
    return allocated


def commercial_gtin(barcode):
    """cEAN: the factory GTIN, or "SEM GTIN" for internal/restricted codes (prefix 2)."""
    from .validators import valid_gtin
    code = str(barcode or "").strip()
    if not valid_gtin(code) or code[0] == "2" or (len(code) == 14 and code[1] == "2"):
        return "SEM GTIN"
    return code


def _rate_text(rate):
    if rate is None:
        return None
    rate = Decimal(str(rate))
    places = Decimal("0.01") if rate == rate.quantize(Decimal("0.01")) else Decimal("0.0001")
    return format(rate.quantize(places), "f")


def _qty_text(milli, factor=Decimal(1)):
    return format((Decimal(milli) / Decimal(1000) * factor).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP), "f")


def _money_text(cents):
    return format((Decimal(cents) / HUNDRED).quantize(Decimal("0.01")), "f")


def _st(fields, base_cents, own_icms_cents, own_fcp_cents, issues):
    mode = int(fields.get("st_bc_mode", 4))
    raw = Decimal(base_cents)
    if mode == 4:
        raw = raw * (HUNDRED + _dec(fields.get("st_mva"), Decimal(0))) / HUNDRED
    elif mode != 6:
        issues.append({"code": "st_mode_unsupported", "field": "st_bc_mode", "severity": "error",
                       "message": "Modalidade da base do ICMS ST por pauta ou preço tabelado ainda não é calculada; use MVA (4) ou valor da operação (6)"})
    raw = raw * (HUNDRED - _dec(fields.get("st_bc_reduction"), Decimal(0))) / HUNDRED
    bc = _round(raw)
    value = max(0, pct(bc, fields.get("st_rate")) - own_icms_cents)
    fcp_rate = fields.get("st_fcp_rate")
    fcp = max(0, pct(bc, fcp_rate) - own_fcp_cents) if fcp_rate else 0
    return {"bc_mode": mode, "mva": _rate_text(fields.get("st_mva")),
            "bc_reduction": _rate_text(fields.get("st_bc_reduction")),
            "bc_cents": bc, "rate": _rate_text(fields.get("st_rate") or 0), "value_cents": value,
            "fcp_bc_cents": bc if fcp_rate else 0, "fcp_rate": _rate_text(fcp_rate), "fcp_cents": fcp}


def _st_retained(fields, quantity_milli):
    unit_bc = _dec(fields.get("st_retained_bc_unit"))
    if unit_bc is None:
        return None
    qty = Decimal(quantity_milli) / Decimal(1000)
    bc = _round(unit_bc * qty * HUNDRED)
    substitute = _round(_dec(fields.get("st_retained_substitute_unit"), Decimal(0)) * qty * HUNDRED)
    rate = fields.get("st_retained_rate")
    value = max(0, pct(bc, rate) - substitute) if rate else 0
    fcp_rate = fields.get("st_retained_fcp_rate")
    return {"bc_cents": bc, "rate": _rate_text(rate), "substitute_cents": substitute, "value_cents": value,
            "fcp_bc_cents": bc if fcp_rate else 0, "fcp_rate": _rate_text(fcp_rate),
            "fcp_cents": pct(bc, fcp_rate) if fcp_rate else 0}


def _icms(fields, crt, base_cents, quantity_milli, credit_rate, issues):
    simples = crt in SIMPLES_CRTS
    code = (fields.get("icms_csosn") if simples else fields.get("icms_cst")) or ""
    result = {"cst": None if simples else code or None, "csosn": (code or None) if simples else None,
              "origin": int(fields.get("origin", 0)), "group": "", "bc_mode": None, "bc_cents": 0,
              "rate": None, "bc_reduction": None, "value_cents": 0, "fcp_rate": None, "fcp_cents": 0,
              "operation_cents": None, "deferral_rate": None, "deferred_cents": None,
              "st": None, "st_retained": None, "sn_credit_rate": None, "sn_credit_cents": 0,
              "cbenef": fields.get("cbenef") or None}
    if not code:
        return result

    def own(reduction=None):
        bc = reduce_base(base_cents, reduction) if reduction else base_cents
        result.update(bc_mode=int(fields.get("icms_bc_mode", 3)), bc_cents=bc,
                      rate=_rate_text(fields.get("icms_rate") or 0), value_cents=pct(bc, fields.get("icms_rate")))
        if reduction:
            result["bc_reduction"] = _rate_text(reduction)
        if fields.get("fcp_rate"):
            result.update(fcp_rate=_rate_text(fields["fcp_rate"]), fcp_cents=pct(bc, fields["fcp_rate"]))

    if not simples:
        result["group"] = "ICMS" + code
        if code == "00":
            own()
        elif code == "10":
            own()
            result["st"] = _st(fields, base_cents, result["value_cents"], result["fcp_cents"], issues)
        elif code == "20":
            own(fields.get("icms_bc_reduction"))
        elif code == "30":
            hypothetical = pct(reduce_base(base_cents, fields.get("icms_bc_reduction")), fields.get("icms_rate"))
            result["st"] = _st(fields, base_cents, hypothetical, 0, issues)
        elif code == "51":
            bc = reduce_base(base_cents, fields.get("icms_bc_reduction"))
            operation = pct(bc, fields.get("icms_rate"))
            deferral = _dec(fields.get("icms_deferral_rate"), HUNDRED)
            deferred = pct(operation, deferral)
            result.update(bc_mode=int(fields.get("icms_bc_mode", 3)), bc_cents=bc,
                          rate=_rate_text(fields.get("icms_rate") or 0), operation_cents=operation,
                          deferral_rate=_rate_text(deferral), deferred_cents=deferred,
                          value_cents=operation - deferred)
        elif code == "60":
            result["st_retained"] = _st_retained(fields, quantity_milli)
        elif code == "70":
            own(fields.get("icms_bc_reduction"))
            result["st"] = _st(fields, base_cents, result["value_cents"], result["fcp_cents"], issues)
        elif code == "90":
            if fields.get("icms_rate"):
                own(fields.get("icms_bc_reduction"))
            if fields.get("st_rate"):
                result["st"] = _st(fields, base_cents, result["value_cents"], result["fcp_cents"], issues)
        # 40, 41, 50: no values (group ICMS40)
        if code in ("41", "50"):
            result["group"] = "ICMS40"
        return result

    result["group"] = "ICMSSN" + {"103": "102", "300": "102", "400": "102", "203": "202"}.get(code, code)
    credit = code in ("101", "201")
    if credit:
        result.update(sn_credit_rate=_rate_text(credit_rate or 0), sn_credit_cents=pct(base_cents, credit_rate))
    if code in ("201", "202", "203"):
        hypothetical = pct(base_cents, fields.get("icms_rate"))
        result["st"] = _st(fields, base_cents, hypothetical, 0, issues)
    elif code == "500":
        result["st_retained"] = _st_retained(fields, quantity_milli)
    elif code == "900":
        if fields.get("icms_rate"):
            own(fields.get("icms_bc_reduction"))
        if fields.get("st_rate"):
            result["st"] = _st(fields, base_cents, result["value_cents"], result["fcp_cents"], issues)
    return result


def _contribution(fields, tax, base_cents):
    cst = fields.get(f"{tax}_cst") or ""
    rate = fields.get(f"{tax}_rate")
    if not cst:
        return None
    if cst in data.PIS_COFINS_NO_VALUE or cst == "03":
        return {"cst": cst, "group": "NT" if cst != "03" else "Qtde", "bc_cents": 0, "rate": None, "value_cents": 0}
    if cst in data.PIS_COFINS_RATED:
        return {"cst": cst, "group": "Aliq", "bc_cents": base_cents, "rate": _rate_text(rate or 0),
                "value_cents": pct(base_cents, rate)}
    # 49 / 99: "Outras" group; computed only when a rate was configured.
    if rate and Decimal(str(rate)):
        return {"cst": cst, "group": "Outr", "bc_cents": base_cents, "rate": _rate_text(rate),
                "value_cents": pct(base_cents, rate)}
    return {"cst": cst, "group": "Outr", "bc_cents": 0, "rate": "0.00", "value_cents": 0}


def _approx(fields, base_cents, approx, fallback):
    ncm = fields.get("ncm") or ""
    found = approx(ncm, fields.get("ex_tipi") or "") if (approx and ncm) else None
    if found:
        origin = int(fields.get("origin", 0))
        federal_rate = found["federal_imported"] if origin in data.IMPORTED_ORIGINS else found["federal_national"]
        federal, state, city = pct(base_cents, federal_rate), pct(base_cents, found["state"]), pct(base_cents, found["municipal"])
        return {"source": "ibpt", "version": found.get("version") or "", "federal_cents": federal,
                "state_cents": state, "municipal_cents": city, "total_cents": federal + state + city,
                "federal_rate": _rate_text(federal_rate), "state_rate": _rate_text(found["state"]),
                "municipal_rate": _rate_text(found["municipal"])}
    if fallback not in (None, ""):
        total = pct(base_cents, fallback)
        return {"source": "fallback", "version": "", "federal_cents": None, "state_cents": None,
                "municipal_cents": None, "total_cents": total, "rate": _rate_text(fallback)}
    return {"source": "none", "version": "", "federal_cents": None, "state_cents": None,
            "municipal_cents": None, "total_cents": 0}


def compute_item(issuer, item, model=65, approx=None, approx_fallback=None, ncm_exists=None):
    crt = int(issuer.get("crt") or 1)
    fields = dict(item.get("fiscal") or {})
    quantity = item.get("quantity_milli")
    price = item.get("unit_price_cents")
    if type(quantity) is not int or quantity <= 0:
        raise ValueError("Quantidade do item inválida")
    if type(price) is not int or price < 0:
        raise ValueError("Valor unitário do item inválido")
    gross = item.get("gross_cents")
    if gross is None:
        gross = line_gross(price, quantity)
    discount = int(item.get("discount_cents") or 0)
    freight = int(item.get("freight_cents") or 0)
    other = int(item.get("other_cents") or 0)
    if not 0 <= discount <= gross or freight < 0 or other < 0:
        raise ValueError("Desconto, frete ou outras despesas do item inválidos")
    issues = product_issues(fields, crt, issuer.get("uf", ""), model, ncm_exists)
    value = gross - discount + freight + other

    ipi = None
    if model == 55 and fields.get("ipi_cst"):
        ipi_cst = fields["ipi_cst"]
        taxed = ipi_cst in ("50", "99")
        ipi = {"cst": ipi_cst, "enquadramento": fields.get("ipi_enquadramento", "999"),
               "group": "IPITrib" if taxed else "IPINT",
               "bc_cents": value if taxed else 0, "rate": _rate_text(fields.get("ipi_rate") or 0) if taxed else None,
               "value_cents": pct(value, fields.get("ipi_rate")) if taxed else 0}
    ipi_value = ipi["value_cents"] if ipi else 0
    # Consumer-final sale: the IPI is part of the ICMS base (art. 155, §2º, XI, CF).
    icms = _icms(fields, crt, value + ipi_value, quantity, issuer.get("simples_credit_rate"), issues)
    pis_cofins_base = value - (icms["value_cents"] if fields.get("pis_cofins_exclude_icms") else 0)
    pis = _contribution(fields, "pis", pis_cofins_base)
    cofins = _contribution(fields, "cofins", pis_cofins_base)

    ibs_cbs = None
    if fields.get("ibs_cbs_cst"):
        base = value
        if fields.get("ibs_cbs_base_excludes_taxes", True):
            base -= icms["value_cents"] + icms["fcp_cents"] + (pis or {}).get("value_cents", 0) + (cofins or {}).get("value_cents", 0)
        base = max(0, base)
        state = pct(base, fields.get("ibs_state_rate"))
        city = pct(base, fields.get("ibs_city_rate"))
        ibs_cbs = {"cst": fields["ibs_cbs_cst"], "class": fields.get("ibs_cbs_class") or None, "bc_cents": base,
                   "ibs_state_rate": _rate_text(fields.get("ibs_state_rate") or 0), "ibs_state_cents": state,
                   "ibs_city_rate": _rate_text(fields.get("ibs_city_rate") or 0), "ibs_city_cents": city,
                   "ibs_cents": state + city,
                   "cbs_rate": _rate_text(fields.get("cbs_rate") or 0), "cbs_cents": pct(base, fields.get("cbs_rate"))}

    st = icms["st"] or {}
    total = value + st.get("value_cents", 0) + st.get("fcp_cents", 0) + ipi_value
    factor = _dec(fields.get("tax_unit_factor"), Decimal(1))
    tax_quantity = Decimal(quantity) / Decimal(1000) * factor
    gtin = commercial_gtin(item.get("barcode"))
    return {
        "number": item.get("number"), "product_id": item.get("product_id"), "code": item.get("code") or "",
        "description": item.get("description") or "", "gtin": gtin,
        "ncm": fields.get("ncm") or "", "ex_tipi": fields.get("ex_tipi") or None, "cest": fields.get("cest") or None,
        "cfop": fields.get("cfop") or "", "cbenef": fields.get("cbenef") or None,
        "origin": int(fields.get("origin", 0)),
        "unit": item.get("unit") or "UN", "quantity_milli": quantity, "quantity": _qty_text(quantity),
        "unit_price_cents": price, "unit_price": _money_text(price),
        "tax_unit": fields.get("tax_unit") or item.get("unit") or "UN",
        "tax_quantity": _qty_text(quantity, factor),
        "tax_unit_price": format((Decimal(gross) / HUNDRED / tax_quantity).quantize(Decimal("0.0000000001"), rounding=ROUND_HALF_UP).normalize(), "f") if tax_quantity else "0",
        "tax_gtin": fields.get("tax_gtin") or gtin,
        "gross_cents": gross, "discount_cents": discount, "freight_cents": freight, "other_cents": other,
        "total_cents": total,
        "icms": icms, "pis": pis, "cofins": cofins, "ipi": ipi, "ibs_cbs": ibs_cbs,
        "approx_tax": _approx(fields, gross - discount, approx, approx_fallback),
        "issues": issues, "ready": not has_errors(issues),
    }


TOTAL_KEYS = ("products_cents", "discount_cents", "freight_cents", "other_cents", "icms_bc_cents", "icms_cents",
              "fcp_cents", "st_bc_cents", "st_cents", "fcp_st_cents", "st_retained_cents", "fcp_st_retained_cents",
              "ipi_cents", "pis_cents", "cofins_cents", "sn_credit_cents", "approx_tax_cents", "total_cents",
              "ibs_state_cents", "ibs_city_cents", "ibs_cents", "cbs_cents", "ibs_cbs_bc_cents")


def totals_of(items):
    totals = dict.fromkeys(TOTAL_KEYS, 0)
    for item in items:
        icms, st, retained = item["icms"], item["icms"]["st"] or {}, item["icms"]["st_retained"] or {}
        totals["products_cents"] += item["gross_cents"]
        totals["discount_cents"] += item["discount_cents"]
        totals["freight_cents"] += item["freight_cents"]
        totals["other_cents"] += item["other_cents"]
        totals["icms_bc_cents"] += icms["bc_cents"]
        totals["icms_cents"] += icms["value_cents"]
        totals["fcp_cents"] += icms["fcp_cents"]
        totals["st_bc_cents"] += st.get("bc_cents", 0)
        totals["st_cents"] += st.get("value_cents", 0)
        totals["fcp_st_cents"] += st.get("fcp_cents", 0)
        totals["st_retained_cents"] += retained.get("value_cents", 0)
        totals["fcp_st_retained_cents"] += retained.get("fcp_cents", 0)
        totals["sn_credit_cents"] += icms["sn_credit_cents"]
        totals["ipi_cents"] += (item["ipi"] or {}).get("value_cents", 0)
        totals["pis_cents"] += (item["pis"] or {}).get("value_cents", 0)
        totals["cofins_cents"] += (item["cofins"] or {}).get("value_cents", 0)
        totals["approx_tax_cents"] += item["approx_tax"]["total_cents"]
        totals["total_cents"] += item["total_cents"]
        ibs = item["ibs_cbs"] or {}
        for key, source in (("ibs_state_cents", "ibs_state_cents"), ("ibs_city_cents", "ibs_city_cents"),
                            ("ibs_cents", "ibs_cents"), ("cbs_cents", "cbs_cents"), ("ibs_cbs_bc_cents", "bc_cents")):
            totals[key] += ibs.get(source, 0)
    return totals


def compute_document(issuer, items, model=65, discount_cents=None, approx=None, approx_fallback=None,
                     ncm_exists=None):
    """Taxes of every item plus the note totals.

    issuer: {"crt": 1..4, "uf": "SP", "simples_credit_rate": "1.25" | None}
    items: [{"product_id", "code", "description", "barcode", "unit", "quantity_milli",
             "unit_price_cents", "discount_cents"?, "freight_cents"?, "other_cents"?, "fiscal": {...}}]
    discount_cents: optional document discount, apportioned over the items (largest remainder)
      when the items carry no discount of their own.
    approx: optional callable(ncm, ex_tipi) -> {"federal_national", "federal_imported", "state",
      "municipal", "version"} (IBPT row); approx_fallback: percent applied when no row exists.
    """
    if model not in (55, 65):
        raise ValueError("Modelo de nota inválido")
    if not items:
        raise ValueError("Nota sem itens")
    items = [dict(item, number=index + 1) for index, item in enumerate(items)]
    if discount_cents:
        if any(item.get("discount_cents") for item in items):
            raise ValueError("Informe o desconto da nota ou o desconto de cada item, não os dois")
        grosses = [line_gross(item["unit_price_cents"], item["quantity_milli"]) for item in items]
        for item, gross, share in zip(items, grosses, allocate_discount(discount_cents, grosses)):
            item["gross_cents"], item["discount_cents"] = gross, share
    computed = [compute_item(issuer, item, model, approx, approx_fallback, ncm_exists) for item in items]
    totals = totals_of(computed)
    return {"schema": SCHEMA, "model": model,
            "issuer": {"crt": int(issuer.get("crt") or 1), "uf": issuer.get("uf", "")},
            "items": computed, "totals": totals,
            "ready": all(item["ready"] for item in computed)}
