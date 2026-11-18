"""Small public reference tables embedded in the code (no download at runtime).

Sources (all public):
- UF codes: IBGE, "Códigos dos estados" (also cUF of the NF-e).
- CRT, origem, CST/CSOSN, modBC, modBCST: Manual de Orientação do Contribuinte
  (MOC) da NF-e/NFC-e 7.0, layout 4.00, and Ajuste SINIEF 07/2005 annexes.
- PIS/COFINS CST: Instrução Normativa RFB 1.009/2010, tabela 4.3.3/4.3.4 (EFD).
- IPI CST: Instrução Normativa RFB 1.009/2010 (tabela de CST do IPI).
- CFOPs accepted in NFC-e (modelo 65): MOC NFC-e, regra de validação de CFOP
  (vendas presenciais a consumidor final dentro da UF).
- CEST segments: Convênio ICMS 142/2018 (anexos II a XXVI). The NCM prefixes
  below are a heuristic summary of the main annex items, used only to WARN that
  a product probably needs CEST. It is not the full convênio table.
- cBenef: UFs that publish a "tabela de código de benefício fiscal" and reject
  the NF-e without it for CSTs with benefit (NT 2019.001, adoção por UF).
"""

UF_CODES = {
    "RO": "11", "AC": "12", "AM": "13", "RR": "14", "PA": "15", "AP": "16", "TO": "17",
    "MA": "21", "PI": "22", "CE": "23", "RN": "24", "PB": "25", "PE": "26", "AL": "27", "SE": "28", "BA": "29",
    "MG": "31", "ES": "32", "RJ": "33", "SP": "35",
    "PR": "41", "SC": "42", "RS": "43",
    "MS": "50", "MT": "51", "GO": "52", "DF": "53",
}

CRT = {
    1: "Simples Nacional",
    2: "Simples Nacional — excesso de sublimite de receita bruta",
    3: "Regime Normal (Lucro Presumido ou Lucro Real)",
    4: "Simples Nacional — Microempreendedor Individual (MEI)",
}
# Legacy text column kept for old clients (tenant.tax_regime).
CRT_REGIME = {1: "simples_nacional", 2: "simples_excesso", 3: "regime_normal", 4: "mei"}
REGIME_CRT = {value: key for key, value in CRT_REGIME.items()}

# cRegTrib: regime especial de tributação do ISSQN (grupo ISSQNtot da NF-e).
SPECIAL_TAX_REGIMES = {
    "1": "Microempresa municipal", "2": "Estimativa", "3": "Sociedade de profissionais",
    "4": "Cooperativa", "5": "Microempreendedor individual (MEI)", "6": "Microempresa ou empresa de pequeno porte",
}

ORIGINS = {
    0: "Nacional, exceto as indicadas nos códigos 3, 4, 5 e 8",
    1: "Estrangeira — importação direta, exceto a indicada no código 6",
    2: "Estrangeira — adquirida no mercado interno, exceto a indicada no código 7",
    3: "Nacional, com conteúdo de importação superior a 40% e inferior ou igual a 70%",
    4: "Nacional, produzida conforme processos produtivos básicos",
    5: "Nacional, com conteúdo de importação inferior ou igual a 40%",
    6: "Estrangeira — importação direta, sem similar nacional (lista CAMEX) e gás natural",
    7: "Estrangeira — adquirida no mercado interno, sem similar nacional (lista CAMEX) e gás natural",
    8: "Nacional, com conteúdo de importação superior a 70%",
}
# Origins whose federal IBPT rate is the "importados" one.
IMPORTED_ORIGINS = {1, 2, 6, 7}

ICMS_CST = {
    "00": "Tributada integralmente",
    "10": "Tributada e com cobrança do ICMS por substituição tributária",
    "20": "Com redução de base de cálculo",
    "30": "Isenta ou não tributada e com cobrança do ICMS por substituição tributária",
    "40": "Isenta",
    "41": "Não tributada",
    "50": "Suspensão",
    "51": "Diferimento",
    "60": "ICMS cobrado anteriormente por substituição tributária",
    "70": "Com redução de base de cálculo e cobrança do ICMS por substituição tributária",
    "90": "Outras",
}
ICMS_CSOSN = {
    "101": "Tributada pelo Simples Nacional com permissão de crédito",
    "102": "Tributada pelo Simples Nacional sem permissão de crédito",
    "103": "Isenção do ICMS no Simples Nacional para faixa de receita bruta",
    "201": "Tributada com permissão de crédito e com cobrança do ICMS por ST",
    "202": "Tributada sem permissão de crédito e com cobrança do ICMS por ST",
    "203": "Isenção para faixa de receita bruta e com cobrança do ICMS por ST",
    "300": "Imune",
    "400": "Não tributada pelo Simples Nacional",
    "500": "ICMS cobrado anteriormente por substituição tributária (substituído) ou por antecipação",
    "900": "Outros",
}
# Codes that describe goods moving under substituição tributária.
ST_CST = {"10", "30", "60", "70"}
ST_CSOSN = {"201", "202", "203", "500"}
# ST calculated in this very sale (the seller is the substitute).
ST_CALCULATED = {"10", "30", "70", "201", "202", "203"}
ST_RETAINED = {"60", "500"}

ICMS_BC_MODES = {0: "Margem de valor agregado (%)", 1: "Pauta (valor)", 2: "Preço tabelado máximo (valor)",
                 3: "Valor da operação"}
ST_BC_MODES = {0: "Preço tabelado ou máximo sugerido", 1: "Lista negativa (valor)", 2: "Lista positiva (valor)",
               3: "Lista neutra (valor)", 4: "Margem de valor agregado (%)", 5: "Pauta (valor)",
               6: "Valor da operação"}

PIS_COFINS_CST = {
    "01": "Operação tributável — alíquota básica",
    "02": "Operação tributável — alíquota diferenciada",
    "03": "Operação tributável — alíquota por unidade de medida",
    "04": "Operação tributável monofásica — revenda a alíquota zero",
    "05": "Operação tributável por substituição tributária",
    "06": "Operação tributável a alíquota zero",
    "07": "Operação isenta da contribuição",
    "08": "Operação sem incidência da contribuição",
    "09": "Operação com suspensão da contribuição",
    "49": "Outras operações de saída",
    "99": "Outras operações",
}
PIS_COFINS_RATED = {"01", "02"}
PIS_COFINS_NO_VALUE = {"04", "05", "06", "07", "08", "09"}
PIS_COFINS_OTHER = {"49", "99"}

IPI_CST = {
    "50": "Saída tributada", "51": "Saída tributável com alíquota zero", "52": "Saída isenta",
    "53": "Saída não tributada", "54": "Saída imune", "55": "Saída com suspensão", "99": "Outras saídas",
}

# CFOPs accepted in NFC-e (modelo 65).
NFCE_CFOPS = {
    "5101": "Venda de produção do estabelecimento",
    "5102": "Venda de mercadoria adquirida ou recebida de terceiros",
    "5103": "Venda de produção do estabelecimento, efetuada fora do estabelecimento",
    "5104": "Venda de mercadoria adquirida de terceiros, efetuada fora do estabelecimento",
    "5115": "Venda de mercadoria recebida anteriormente em consignação mercantil",
    "5405": "Venda de mercadoria adquirida com ST, na condição de contribuinte substituído",
    "5656": "Venda de combustível ou lubrificante adquirido de terceiros, destinado a consumidor final",
    "5667": "Venda de combustível ou lubrificante a consumidor ou usuário final estabelecido em outra UF",
}
# CFOPs of goods sold under ST (with the tax already collected).
ST_CFOPS = {"5405", "5656", "5667", "5403", "6403", "6404"}

# UFs that require cBenef for CSTs that carry a benefit (isenção, redução, diferimento...).
CBENEF_UFS = {"PR", "RJ", "RS", "DF", "GO", "SC", "ES"}
CBENEF_CSTS = {"20", "30", "40", "41", "50", "51", "70", "90"}

# Heuristic: NCM prefix -> CEST segment of Convênio ICMS 142/2018. Longest prefix wins.
CEST_SEGMENTS = {
    "01": "Autopeças", "02": "Bebidas alcoólicas, exceto cerveja e chope", "03": "Cervejas, chopes, refrigerantes, águas e outras bebidas",
    "04": "Cigarros e outros produtos derivados do fumo", "05": "Cimentos", "06": "Combustíveis e lubrificantes",
    "07": "Energia elétrica", "08": "Ferramentas", "09": "Lâmpadas, reatores e starter",
    "10": "Materiais de construção e congêneres", "11": "Materiais de limpeza", "12": "Materiais elétricos",
    "13": "Medicamentos de uso humano e outros produtos farmacêuticos", "14": "Papéis, plásticos, produtos cerâmicos e vidros",
    "15": "Pneumáticos, câmaras de ar e protetores de borracha", "16": "Produtos alimentícios",
    "17": "Produtos alimentícios", "18": "Produtos de papelaria", "19": "Produtos de perfumaria e de higiene pessoal e cosméticos",
    "20": "Produtos de perfumaria e de higiene pessoal e cosméticos", "21": "Produtos eletrônicos, eletroeletrônicos e eletrodomésticos",
    "22": "Rações para animais domésticos", "23": "Sorvetes e preparados para fabricação de sorvetes em máquinas",
    "24": "Tintas e vernizes", "25": "Veículos automotores", "26": "Veículos de duas e três rodas motorizados",
    "27": "Venda de mercadorias pelo sistema porta a porta", "28": "Venda de mercadorias pelo sistema porta a porta",
}
ST_NCM_PREFIXES = {
    "2201": "03", "2202": "03", "2203": "03", "2106.90.10": "03",
    "2204": "02", "2205": "02", "2206": "02", "2207": "02", "2208": "02",
    "2402": "04", "2403": "04",
    "2523": "05",
    "2710": "06", "2711": "06", "2207.10": "06",
    "3303": "20", "3304": "20", "3305": "20", "3306": "20", "3307": "20", "3401": "20", "9603.21": "20",
    "3004": "13", "3003": "13", "3005": "13", "3006": "13",
    "2309": "22",
    "2105": "23",
    "3208": "24", "3209": "24", "3210": "24",
    "4011": "15", "4013": "15",
    "8539": "09", "8504.10": "09",
    "1806": "17", "1704": "17", "1905": "17", "1902": "17", "2103": "17", "0402": "17", "1901": "17",
    "8506": "21", "8516": "21", "8517": "21", "8528": "21",
    "3402": "11", "3808": "11",
    "6907": "10", "6908": "10", "7214": "10", "7308": "10", "3917": "10", "3922": "10",
    "8536": "12", "8544": "12",
}


def st_segment_for_ncm(ncm):
    """(segment, description) when the NCM usually needs CEST, else None."""
    code = "".join(c for c in str(ncm or "") if c.isdigit())
    if len(code) != 8:
        return None
    best = None
    for prefix, segment in ST_NCM_PREFIXES.items():
        plain = prefix.replace(".", "")
        if code.startswith(plain) and (best is None or len(plain) > len(best[0])):
            best = (plain, segment)
    return (best[1], CEST_SEGMENTS[best[1]]) if best else None
