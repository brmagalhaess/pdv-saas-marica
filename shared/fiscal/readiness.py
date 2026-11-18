"""What is missing before a product (or the company) can go on a fiscal note.

Each issue is {"code", "field", "severity", "message"}; severity "error"
blocks the note, "warning" is advice that the accountant should confirm.
Messages are written for the shopkeeper, in Portuguese.
"""
from decimal import Decimal

from . import data
from .validators import (cnpj_value, digits, ie_value, valid_cep, valid_cnae, valid_cnpj, valid_ibge_city,
                         valid_ie, valid_ncm)

SIMPLES_CRTS = (1, 4)


def _issue(code, field, message, severity="error"):
    return {"code": code, "field": field, "severity": severity, "message": message}


def _rate(fields, key):
    try:
        return Decimal(str(fields.get(key))) if fields.get(key) not in (None, "") else None
    except Exception:
        return None


def product_issues(fields, crt, uf="", model=65, ncm_exists=None):
    """Issues of a resolved fiscal field dict (see schema.resolve).

    `ncm_exists`: optional callable(ncm) -> bool, used when an NCM table is loaded.
    """
    issues = []
    crt = int(crt or 1)
    simples = crt in SIMPLES_CRTS
    ncm = fields.get("ncm") or ""
    cest = fields.get("cest") or ""
    cfop = fields.get("cfop") or ""
    cst = fields.get("icms_cst") or ""
    csosn = fields.get("icms_csosn") or ""
    code = csosn if simples else cst

    if not ncm:
        issues.append(_issue("ncm_missing", "ncm", "Falta NCM: informe a classificação fiscal de 8 dígitos do produto"))
    elif not valid_ncm(ncm):
        issues.append(_issue("ncm_invalid", "ncm", "NCM inválido: use 8 dígitos"))
    elif ncm_exists is not None and not ncm_exists(ncm):
        issues.append(_issue("ncm_unknown", "ncm", f"NCM {ncm} não existe na tabela NCM vigente"))

    if fields.get("origin") is None:
        issues.append(_issue("origin_missing", "origin", "Falta a origem da mercadoria (0 a 8)"))

    if not cfop:
        issues.append(_issue("cfop_missing", "cfop", "Falta CFOP: para venda no balcão, normalmente 5102 ou 5405"))
    elif model == 65 and cfop not in data.NFCE_CFOPS:
        issues.append(_issue("cfop_nfce", "cfop", f"CFOP {cfop} não é aceito na NFC-e; use 5101, 5102, 5405 ou outro de venda a consumidor"))
    elif model == 55 and cfop[0] not in "567":
        issues.append(_issue("cfop_entry", "cfop", f"CFOP {cfop} é de entrada; a nota de venda usa CFOP iniciado por 5, 6 ou 7"))

    # ICMS
    if simples:
        if cst and not csosn:
            issues.append(_issue("csosn_regime", "icms_csosn", f"Empresa do Simples Nacional usa CSOSN, não CST {cst}; escolha um perfil do Simples"))
        elif not csosn:
            issues.append(_issue("csosn_missing", "icms_csosn", "Falta CSOSN do ICMS (ex.: 102 ou 500)"))
    else:
        if csosn and not cst:
            issues.append(_issue("cst_regime", "icms_cst", f"Empresa do regime normal usa CST, não CSOSN {csosn}; escolha um perfil do regime normal"))
        elif not cst:
            issues.append(_issue("cst_missing", "icms_cst", "Falta CST do ICMS (ex.: 00 ou 60)"))

    if code:
        icms_rate = _rate(fields, "icms_rate")
        if code in ("00", "10", "20", "70") and not icms_rate:
            issues.append(_issue("icms_rate_missing", "icms_rate", f"Falta a alíquota de ICMS para o CST {code}"))
        if code in ("20", "70") and not _rate(fields, "icms_bc_reduction"):
            issues.append(_issue("icms_reduction_missing", "icms_bc_reduction", f"Falta o percentual de redução da base para o CST {code}"))
        if code == "51" and _rate(fields, "icms_rate") is None:
            issues.append(_issue("icms_rate_missing", "icms_rate", "Falta a alíquota de ICMS da operação diferida (CST 51)"))
        if code in data.ST_CALCULATED:
            if not _rate(fields, "st_rate"):
                issues.append(_issue("st_rate_missing", "st_rate", f"Falta a alíquota do ICMS ST para o código {code}"))
            if fields.get("st_bc_mode", 4) == 4 and _rate(fields, "st_mva") is None:
                issues.append(_issue("st_mva_missing", "st_mva", f"Falta a MVA da substituição tributária para o código {code}"))
            if model == 65:
                issues.append(_issue("st_in_nfce", "icms_cst" if not simples else "icms_csosn",
                                     "ICMS ST calculado na própria venda não é usual na NFC-e; se o fornecedor já recolheu a ST, use CST 60 / CSOSN 500",
                                     "warning"))
        if code == "101" and model == 65:
            issues.append(_issue("csosn_101_nfce", "icms_csosn", "CSOSN 101 dá crédito ao comprador; na venda a consumidor final (NFC-e) normalmente se usa 102", "warning"))
        st_code = code in data.ST_CST or code in data.ST_CSOSN
        if st_code and not cest:
            issues.append(_issue("cest_missing", "cest", "Falta CEST: este produto está na substituição tributária (CST/CSOSN de ST)"))
        if cfop in data.ST_CFOPS and not st_code:
            issues.append(_issue("cfop_st_mismatch", "cfop", f"CFOP {cfop} é de mercadoria com ST, mas o código do ICMS ({code}) não é de ST; use CST 60 / CSOSN 500 ou CFOP 5102", "warning"))
        if code in data.ST_RETAINED and cfop in ("5102", "5101"):
            issues.append(_issue("cfop_st_expected", "cfop", f"Mercadoria com ST já recolhida ({code}) normalmente sai com CFOP 5405", "warning"))
        if not simples and uf in data.CBENEF_UFS and cst in data.CBENEF_CSTS and not fields.get("cbenef"):
            issues.append(_issue("cbenef_missing", "cbenef", f"{uf} exige o código de benefício fiscal (cBenef) para o CST {cst}; confirme com o contador", "warning"))
    if fields.get("cbenef") and uf and not fields["cbenef"].startswith(uf):
        issues.append(_issue("cbenef_uf", "cbenef", f"cBenef {fields['cbenef']} não começa com a UF da empresa ({uf})", "warning"))

    if ncm and not cest:
        segment = data.st_segment_for_ncm(ncm)
        if segment and not any(i["code"] == "cest_missing" for i in issues):
            issues.append(_issue("cest_suggested", "cest",
                                 f"NCM {ncm} costuma estar na substituição tributária (segmento {segment[0]} — {segment[1]}); informe o CEST",
                                 "warning"))

    # PIS / COFINS
    for tax, label in (("pis", "PIS"), ("cofins", "COFINS")):
        tax_cst = fields.get(f"{tax}_cst") or ""
        if not tax_cst:
            issues.append(_issue(f"{tax}_cst_missing", f"{tax}_cst", f"Falta CST do {label} (Simples: normalmente 49 ou 99)"))
            continue
        if tax_cst == "03":
            issues.append(_issue(f"{tax}_cst_unsupported", f"{tax}_cst", f"CST 03 do {label} (alíquota por unidade) ainda não é calculado pelo PDV2"))
        if tax_cst in data.PIS_COFINS_RATED and not _rate(fields, f"{tax}_rate"):
            issues.append(_issue(f"{tax}_rate_missing", f"{tax}_rate", f"Falta a alíquota do {label} para o CST {tax_cst}"))
        if simples and tax_cst in data.PIS_COFINS_RATED:
            issues.append(_issue(f"{tax}_simples", f"{tax}_cst", f"No Simples Nacional o {label} é pago no DAS; normalmente se usa CST 49, 99, 04 ou 07", "warning"))

    if fields.get("ipi_cst") and model == 65:
        issues.append(_issue("ipi_nfce", "ipi_cst", "A NFC-e não tem IPI; os dados de IPI serão usados apenas na NF-e", "warning"))
    if fields.get("ipi_cst") == "50" and not _rate(fields, "ipi_rate"):
        issues.append(_issue("ipi_rate_missing", "ipi_rate", "Falta a alíquota do IPI para o CST 50"))

    ibs_keys = ("ibs_cbs_cst", "ibs_cbs_class")
    if any(fields.get(key) for key in ibs_keys + ("cbs_rate", "ibs_state_rate", "ibs_city_rate")):
        for key, label in (("ibs_cbs_cst", "CST do IBS/CBS"), ("ibs_cbs_class", "cClassTrib do IBS/CBS")):
            if not fields.get(key):
                issues.append(_issue(f"{key}_missing", key, f"Falta {label} (Reforma Tributária)"))
    if fields.get("tax_unit") and not fields.get("tax_unit_factor"):
        issues.append(_issue("tax_unit_factor_missing", "tax_unit_factor", "Informe o fator de conversão da unidade tributável"))
    return issues


def company_issues(company):
    """Issues of the issuer data (dict with the Tenant fiscal fields)."""
    issues = []
    document = cnpj_value(company.get("document"))
    uf = str(company.get("address_state") or "").upper()
    crt = company.get("crt")
    if not document:
        issues.append(_issue("cnpj_missing", "document", "Falta o CNPJ da empresa"))
    elif not valid_cnpj(document):
        issues.append(_issue("cnpj_invalid", "document", "CNPJ inválido: confira os dígitos" if len(document) == 14 else "A NFC-e exige o CNPJ do emitente"))
    if not uf:
        issues.append(_issue("uf_missing", "address_state", "Falta a UF do endereço"))
    elif uf not in data.UF_CODES:
        issues.append(_issue("uf_invalid", "address_state", "UF inválida"))
    ie = ie_value(company.get("state_registration"))
    if not ie:
        issues.append(_issue("ie_missing", "state_registration", "Falta a inscrição estadual: a NFC-e exige IE do emitente"))
    elif ie == "ISENTO":
        issues.append(_issue("ie_exempt", "state_registration", "Emitente sem inscrição estadual não emite NFC-e de mercadorias"))
    elif uf in data.UF_CODES and not valid_ie(ie, uf):
        issues.append(_issue("ie_invalid", "state_registration", f"Inscrição estadual inválida para {uf}: confira os dígitos"))
    city = digits(company.get("city_ibge_code"))
    if not city:
        issues.append(_issue("ibge_missing", "city_ibge_code", "Falta o código IBGE do município (7 dígitos)"))
    elif not valid_ibge_city(city, uf or None):
        issues.append(_issue("ibge_invalid", "city_ibge_code", "Código IBGE do município inválido ou de outra UF"))
    if not valid_cep(company.get("address_zip")):
        issues.append(_issue("cep_invalid", "address_zip", "Falta o CEP (8 dígitos)"))
    for field, label in (("address_line", "o logradouro"), ("address_number", "o número do endereço"),
                         ("address_neighborhood", "o bairro"), ("address_city", "o município")):
        if not str(company.get(field) or "").strip():
            issues.append(_issue(f"{field}_missing", field, f"Falta {label}"))
    if crt not in data.CRT:
        issues.append(_issue("crt_missing", "crt", "Escolha o regime tributário (CRT)"))
    if company.get("cnae") and not valid_cnae(company["cnae"]):
        issues.append(_issue("cnae_invalid", "cnae", "CNAE deve ter 7 dígitos", "warning"))
    if not company.get("cnae"):
        issues.append(_issue("cnae_missing", "cnae", "Informe o CNAE principal (recomendado)", "warning"))
    return issues


def has_errors(issues):
    return any(issue["severity"] == "error" for issue in issues)


def summary(issues):
    """Compact form for lists and for the terminal: ready flag and error messages."""
    errors = [issue["message"] for issue in issues if issue["severity"] == "error"]
    warnings = [issue["message"] for issue in issues if issue["severity"] == "warning"]
    return {"ready": not errors, "missing": errors, "warnings": warnings}
