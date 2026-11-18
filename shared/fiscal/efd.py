"""SPED Fiscal — EFD ICMS/IPI a partir das NFC-e do mês (Python puro).

Leiaute: código de versão 020 (Ato COTEPE/ICMS 79/2025, Nota Técnica 2025.001), vigente para os
períodos de 01/01/2026 a 31/12/2026, conforme o Guia Prático da EFD ICMS/IPI 3.2.x
(3.1.9 publicado com o leiaute 020; 3.2.2 de 25/03/2026; 3.2.3 de 06/05/2026). Ver docs/PACOTE-CONTADOR.md.

Escopo: só saídas por NFC-e (modelo 65) de emissão própria. Entradas (compras), CT-e, inventário (H),
produção (K) e ajustes da apuração ficam com o contador. O arquivo **precisa** ser importado e validado
no PVA da Receita antes da entrega.

Regras do Guia aplicadas à NFC-e (registro C100, exceções):
- só C100 + C190 (sem C170); C100 sem COD_PART, VL_BC_ICMS_ST, VL_ICMS_ST, VL_IPI, VL_PIS, VL_COFINS,
  VL_PIS_ST e VL_COFINS_ST;
- cancelada (COD_SIT 02): só REG, IND_OPER, IND_EMIT, COD_MOD, COD_SIT, SER, NUM_DOC e CHV_NFE, sem filhos;
- numeração inutilizada (COD_SIT 05): os mesmos campos sem a chave, um C100 por número — só para períodos
  até 12/2022; a partir de 01/2023 os códigos 04 (denegado) e 05 (inutilizado) saíram da tabela 4.1.2 e a
  inutilização não é escriturada (fica no pacote do contador);
- C190 agrupa por CST_ICMS × CFOP × ALIQ_ICMS; VL_OPR = mercadoria − desconto + frete + seguro + outras +
  ST + FCP-ST + IPI; a soma dos VL_OPR da nota = VL_DOC.
"""
import re
from datetime import date, timedelta

from .month_report import analytic_rows
from .validators import valid_cnpj, valid_cpf

LAYOUTS = {  # ano do período -> (COD_VER, confirmado)
    2020: ("014", True), 2021: ("015", True), 2022: ("016", True), 2023: ("017", True),
    2024: ("018", True), 2025: ("019", True), 2026: ("020", True),
}
GUIDE_VERSION = "3.2.3"
ENCODING = "latin-1"
NEWLINE = "\r\n"

FIELDS = {  # quantidade de campos (incluindo REG) por registro — usada na validação
    "0000": 15, "0001": 2, "0005": 10, "0100": 14, "0150": 13, "0190": 3, "0200": 13, "0990": 2,
    "B001": 2, "B990": 2, "C001": 2, "C100": 29, "C190": 12, "C990": 2, "D001": 2, "D990": 2,
    "E001": 2, "E100": 3, "E110": 15, "E116": 10, "E990": 2, "G001": 2, "G990": 2, "H001": 2, "H990": 2,
    "K001": 2, "K990": 2, "1001": 2, "1010": 14, "1990": 2, "9001": 2, "9900": 3, "9990": 2, "9999": 2,
}
BLOCKS = ("0", "B", "C", "D", "E", "G", "H", "K", "1", "9")
UNIT_NAMES = {"UN": "Unidade", "UND": "Unidade", "KG": "Quilograma", "G": "Grama", "L": "Litro", "LT": "Litro",
              "ML": "Mililitro", "M": "Metro", "M2": "Metro quadrado", "M3": "Metro cubico", "CX": "Caixa",
              "PC": "Peca", "PCT": "Pacote", "DZ": "Duzia", "FD": "Fardo", "PAR": "Par", "KIT": "Kit"}
VALUE_RE = re.compile(r"^-?\d+(,\d+)?$")
DATE_RE = re.compile(r"^(0[1-9]|[12]\d|3[01])(0[1-9]|1[0-2])\d{4}$")


class EFDError(ValueError):
    pass


def layout_version(year):
    if year in LAYOUTS:
        return LAYOUTS[year]
    if year > max(LAYOUTS):
        return LAYOUTS[max(LAYOUTS)][0], False
    raise EFDError("Período anterior a 2020 não é suportado")


def val(cents):
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}{cents // 100},{cents % 100:02d}"


def dt(value):
    return value.strftime("%d%m%Y")


def txt(value, limit=None):
    value = re.sub(r"\s+", " ", re.sub(r"[\r\n|]+", " ", str(value or ""))).strip()
    return value[:limit] if limit else value


def digits(value):
    return "".join(c for c in str(value or "") if c.isdigit())


def month_bounds(year, month):
    start = date(year, month, 1)
    end = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))
    return start, end


def key_check_digit(key43):
    total, weight = 0, 2
    for digit in reversed(key43):
        total += int(digit) * weight
        weight = 2 if weight == 9 else weight + 1
    rest = total % 11
    return "0" if rest < 2 else str(11 - rest)


def valid_key(key):
    return len(key) == 44 and key.isdigit() and key_check_digit(key[:43]) == key[43]


def _line(*fields):
    return "|" + "|".join("" if f is None else str(f) for f in fields) + "|"


def _payment_indicator(nfe):
    kinds = {p["type"] for p in nfe["payments"]}
    if kinds & {"05", "15"}:
        return "1"
    if kinds and kinds <= {"90"}:
        return "2"
    return "0"


def build(company, accountant, year, month, documents, inutilizations=(), profile="A", options=None):
    """Monta o TXT. Devolve {text, data (bytes latin-1), lines, counts, e110, warnings, problems, version}."""
    options = options or {}
    if profile not in ("A", "B", "C"):
        raise EFDError("Perfil de apresentação deve ser A, B ou C")
    start, end = month_bounds(year, month)
    version, confirmed = layout_version(year)
    warnings = []
    if not confirmed:
        warnings.append(f"Leiaute para {year} não confirmado: usamos o código {version}. Confira no Guia Prático vigente.")
    lines = []
    cnpj = digits(company.get("cnpj"))
    if not valid_cnpj(cnpj):
        warnings.append("CNPJ da empresa inválido ou ausente.")
    if not digits(company.get("ie")):
        warnings.append("Inscrição estadual da empresa ausente.")
    if not digits(company.get("city_ibge")):
        warnings.append("Código IBGE do município da empresa ausente.")
    if not digits(company.get("cep")) or not company.get("address"):
        warnings.append("Endereço/CEP da empresa incompletos (registro 0005).")

    # ------------------------------------------------------------------ bloco 0
    lines.append(_line("0000", version, options.get("purpose", "0"), dt(start), dt(end), txt(company.get("name"), 100),
                       cnpj, "", txt(company.get("uf")).upper(), digits(company.get("ie")),
                       digits(company.get("city_ibge")), txt(company.get("im")), txt(company.get("suframa")),
                       profile, "1"))
    lines.append(_line("0001", "0"))
    lines.append(_line("0005", txt(company.get("trade_name") or company.get("name"), 60), digits(company.get("cep")),
                       txt(company.get("address"), 60), txt(company.get("number"), 10),
                       txt(company.get("complement"), 60), txt(company.get("neighborhood"), 60),
                       digits(company.get("phone"))[-11:], "", txt(company.get("email"))))
    acc = accountant or {}
    missing = [label for key, label in (("name", "nome"), ("cpf", "CPF"), ("crc", "CRC"), ("email", "e-mail"),
                                        ("city_ibge", "município (IBGE)")) if not acc.get(key)]
    if missing:
        warnings.append("Dados do contador incompletos para o registro 0100: falta " + ", ".join(missing) + ".")
    if acc.get("cpf") and not valid_cpf(digits(acc["cpf"])):
        warnings.append("CPF do contador inválido.")
    if acc.get("cnpj") and not valid_cnpj(digits(acc["cnpj"])):
        warnings.append("CNPJ do escritório de contabilidade inválido.")
    lines.append(_line("0100", txt(acc.get("name"), 100), digits(acc.get("cpf")), txt(acc.get("crc"), 15),
                       digits(acc.get("cnpj")), digits(acc.get("cep")), txt(acc.get("address"), 60),
                       txt(acc.get("number"), 10), txt(acc.get("complement"), 60), txt(acc.get("neighborhood"), 60),
                       digits(acc.get("phone"))[-11:], digits(acc.get("fax"))[-11:], txt(acc.get("email")),
                       digits(acc.get("city_ibge"))))
    # NFC-e não informa participante (COD_PART vazio): 0150 só existiria para outros modelos.
    authorized = sorted((d for d in documents if d["status"] == "authorized" and d.get("nfe")),
                        key=lambda d: (d.get("series") or 0, d.get("number") or 0))
    cancelled = [d for d in documents if d["status"] == "cancelled"]
    items, units = {}, {}
    for doc in authorized:
        for item in doc["nfe"]["items"]:
            code = txt(item["code"], 60) or "SEM-CODIGO"
            unit = (txt(item["unit"], 6) or "UN").upper()
            units.setdefault(unit, UNIT_NAMES.get(unit, unit))
            items.setdefault(code, (item, unit))
    for unit in sorted(units):
        lines.append(_line("0190", unit, units[unit]))
    for code in sorted(items):
        item, unit = items[code]
        lines.append(_line("0200", code, txt(item["description"], 120) or code, digits(item["gtin"]), "", unit, "00",
                           digits(item["ncm"]), digits(item["ex_tipi"]), digits(item["ncm"])[:2], "", "",
                           digits(item["cest"])))
    lines.append(_line("0990", len(lines) + 1))

    # ------------------------------------------------------------------ bloco B (só DF tem movimento)
    lines += [_line("B001", "1"), _line("B990", "2")]

    # ------------------------------------------------------------------ bloco C
    entries = []  # ((série, número, ordem), [linhas]) — C100 em ordem de numeração
    debit_cents = 0
    for doc in authorized:
        nfe = doc["nfe"]
        tot = nfe["totals"]
        issued = doc["local_date"]
        if not start <= issued <= end:
            raise EFDError(f"Nota {doc.get('number')} fora do período")
        exit_date = nfe["exit_at"].date() if nfe.get("exit_at") else issued
        rows = analytic_rows(nfe)
        doc_cod_sit = "00"
        group = [_line(
            "C100", "1", "0", "", "65", doc_cod_sit, str(doc["series"]), doc["number"], doc["key"],
            dt(issued), dt(exit_date), val(tot["vNF"]), _payment_indicator(nfe), val(tot["vDesc"]),
            val(tot.get("vICMSDeson", 0)), val(tot["vProd"]), nfe.get("freight_mode") or "9", val(tot["vFrete"]),
            val(tot["vSeg"]), val(tot["vOutro"]), val(sum(r["bc_cents"] for r in rows)),
            val(sum(r["icms_cents"] for r in rows)), "", "", "", "", "", "", "")]
        for row in rows:
            group.append(_line("C190", row["cst"], row["cfop"], row["rate"], val(row["operation_cents"]),
                                 val(row["bc_cents"]), val(row["icms_cents"]), val(row["st_bc_cents"]),
                                 val(row["st_cents"]), val(row["reduction_cents"]), val(row["ipi_cents"]), ""))
            if row["cfop"][:1] in ("5", "6", "7"):
                debit_cents += row["icms_cents"]
        entries.append(((doc["series"], doc["number"], 0), group))
    for doc in cancelled:
        if not doc.get("key"):
            warnings.append(f"Cancelada nº {doc.get('number')} sem chave de acesso: não escriturada.")
            continue
        entries.append(((doc.get("series") or 0, doc.get("number") or 0, 1),
                        [_line("C100", "1", "0", "", "65", "02", str(doc["series"]), doc["number"], doc["key"],
                               *([""] * 20))]))
    if year < 2023:
        for inut in inutilizations:
            for number in range(inut["start"], inut["end"] + 1):
                entries.append(((inut["series"], number, 2),
                                [_line("C100", "1", "0", "", "65", "05", str(inut["series"]), number, "",
                                       *([""] * 20))]))
    c_lines = [line for _, group in sorted(entries, key=lambda e: e[0]) for line in group]
    lines.append(_line("C001", "0" if c_lines else "1"))
    lines += c_lines
    lines.append(_line("C990", len(c_lines) + 2))

    # ------------------------------------------------------------------ blocos D
    lines += [_line("D001", "1"), _line("D990", "2")]

    # ------------------------------------------------------------------ bloco E (apuração do ICMS)
    previous_credit = int(options.get("previous_credit_cents") or 0)
    credits = int(options.get("credits_cents") or 0)
    balance = debit_cents - credits - previous_credit
    to_pay = max(balance, 0)
    carry = max(-balance, 0)
    e110 = {"VL_TOT_DEBITOS": debit_cents, "VL_AJ_DEBITOS": 0, "VL_TOT_AJ_DEBITOS": 0, "VL_ESTORNOS_CRED": 0,
            "VL_TOT_CREDITOS": credits, "VL_AJ_CREDITOS": 0, "VL_TOT_AJ_CREDITOS": 0, "VL_ESTORNOS_DEB": 0,
            "VL_SLD_CREDOR_ANT": previous_credit, "VL_SLD_APURADO": to_pay, "VL_TOT_DED": 0,
            "VL_ICMS_RECOLHER": to_pay, "VL_SLD_CREDOR_TRANSPORTAR": carry, "DEB_ESP": 0}
    e_lines = [_line("E100", dt(start), dt(end)), _line("E110", *(val(v) for v in e110.values()))]
    if to_pay > 0:
        e116 = options.get("e116") or {}
        due = e116.get("due_date")
        if not due and e116.get("due_day"):
            nxt = end + timedelta(days=1)
            day = min(int(e116["due_day"]), month_bounds(nxt.year, nxt.month)[1].day)
            due = date(nxt.year, nxt.month, day)
        if not e116.get("cod_rec"):
            warnings.append("ICMS a recolher sem código de receita da UF (E116/COD_REC): configure antes de entregar.")
        if not due:
            warnings.append("ICMS a recolher sem data de vencimento (E116/DT_VCTO): configure o dia de vencimento.")
        e_lines.append(_line("E116", e116.get("cod_or") or "000", val(to_pay), dt(due) if due else "",
                             txt(e116.get("cod_rec"), 30), "", "", "", "", f"{month:02d}{year}"))
    lines.append(_line("E001", "0"))
    lines += e_lines
    lines.append(_line("E990", len(e_lines) + 2))

    # ------------------------------------------------------------------ G, H, K, 1
    lines += [_line("G001", "1"), _line("G990", "2"), _line("H001", "1"), _line("H990", "2"),
              _line("K001", "1"), _line("K990", "2")]
    if month == 2:
        warnings.append("Fevereiro: o inventário de 31/12 (bloco H, H005/H010) é obrigatório e não é gerado aqui.")
    lines += [_line("1001", "0"), _line("1010", *(["N"] * 13)), _line("1990", "3")]

    # ------------------------------------------------------------------ bloco 9
    counts = {}
    for line in lines:
        reg = line[1:5]
        counts[reg] = counts.get(reg, 0) + 1
    for reg in ("9001", "9990", "9999"):
        counts[reg] = 1
    counts["9900"] = len(counts) + 1
    block9 = [_line("9001", "0")]
    for reg in sorted(counts, key=lambda r: (BLOCKS.index(r[0]), r)):
        block9.append(_line("9900", reg, counts[reg]))
    block9.append(_line("9990", len(block9) + 2))
    total = len(lines) + len(block9) + 1
    block9.append(_line("9999", total))
    lines += block9
    text = NEWLINE.join(lines) + NEWLINE
    data = text.encode(ENCODING, "replace")
    result = {"text": text, "data": data, "lines": lines, "counts": counts, "e110": e110, "warnings": warnings,
              "version": version, "guide": GUIDE_VERSION, "period": (dt(start), dt(end)),
              "documents": {"authorized": len(authorized), "cancelled": len(cancelled),
                            "inutilized": sum(i["end"] - i["start"] + 1 for i in inutilizations)}}
    result["problems"] = validate(lines, cnpj=cnpj, start=start, end=end)
    return result


def split(line):
    if not (line.startswith("|") and line.endswith("|")):
        raise EFDError(f"Linha sem pipes nas pontas: {line[:40]}")
    return line[1:-1].split("|")


def validate(lines, cnpj="", start=None, end=None):
    """Regras do PVA reproduzidas (subconjunto). Lista vazia = sem problemas encontrados."""
    problems = []
    rows = []
    for number, line in enumerate(lines, 1):
        try:
            fields = split(line)
        except EFDError as exc:
            problems.append(f"Linha {number}: {exc}")
            continue
        reg = fields[0]
        if reg not in FIELDS:
            problems.append(f"Linha {number}: registro {reg} desconhecido")
        elif len(fields) != FIELDS[reg]:
            problems.append(f"Linha {number}: {reg} com {len(fields)} campos (esperado {FIELDS[reg]})")
        rows.append((number, reg, fields))
    regs = [r for _, r, _ in rows]
    if not regs or regs[0] != "0000" or regs[-1] != "9999":
        problems.append("Arquivo deve começar em 0000 e terminar em 9999")
    order = [BLOCKS.index(r[0]) for r in regs if r[0] in BLOCKS]
    if order != sorted(order):
        problems.append("Blocos fora da ordem 0, B, C, D, E, G, H, K, 1, 9")
    # Encerramentos de bloco: quantidade de linhas do bloco, incluindo abertura e encerramento.
    for block in BLOCKS:
        size = sum(1 for r in regs if r[0] == block)
        closing = [f for _, r, f in rows if r == (f"{block}990" if block != "9" else "9990")]
        if not closing:
            problems.append(f"Bloco {block} sem registro de encerramento")
        elif int(closing[0][1]) != size:
            problems.append(f"Encerramento do bloco {block}: informado {closing[0][1]}, contado {size}")
    counted = {}
    for reg in regs:
        counted[reg] = counted.get(reg, 0) + 1
    informed = {f[1]: int(f[2]) for _, r, f in rows if r == "9900"}
    if informed != counted:
        diff = sorted(set(informed.items()) ^ set(counted.items()))
        problems.append(f"9900 não confere com as linhas do arquivo: {diff[:6]}")
    last = rows[-1][2] if rows else None
    if last and last[0] == "9999" and int(last[1]) != len(lines):
        problems.append(f"9999 informa {last[1]} linhas, arquivo tem {len(lines)}")
    # Formatos de data e valor por registro.
    date_fields = {"0000": (3, 4), "C100": (9, 10), "E100": (1, 2), "E116": (3,)}
    value_fields = {"C100": range(11, 29), "C190": range(3, 11), "E110": range(1, 15), "E116": (2,)}
    for number, reg, fields in rows:
        for index in date_fields.get(reg, ()):
            if index < len(fields) and fields[index] and not DATE_RE.match(fields[index]):
                problems.append(f"Linha {number}: {reg} campo {index + 1} data inválida '{fields[index]}'")
            elif index < len(fields) and fields[index] and start and reg == "C100":
                day = date(int(fields[index][4:]), int(fields[index][2:4]), int(fields[index][:2]))
                if index == 9 and not start <= day <= end:
                    problems.append(f"Linha {number}: DT_DOC fora do período")
        for index in value_fields.get(reg, ()):
            if index < len(fields) and fields[index] and index != 12 and not VALUE_RE.match(fields[index]):
                problems.append(f"Linha {number}: {reg} campo {index + 1} valor inválido '{fields[index]}'")
    # C100 × C190 e chave de acesso.
    cents = lambda raw: int(raw.replace(",", "")) if raw else 0  # noqa: E731  (sempre 2 casas)
    current = None
    analytic = []

    def close(doc, children):
        if doc is None:
            return
        number, fields = doc
        if fields[5] != "00":
            if children:
                problems.append(f"Linha {number}: C100 COD_SIT {fields[5]} não pode ter C190")
            return
        if not children:
            problems.append(f"Linha {number}: C100 sem C190")
        for index, child_index, name in ((11, 4, "VL_DOC × soma VL_OPR"), (20, 5, "VL_BC_ICMS"), (21, 6, "VL_ICMS")):
            if cents(fields[index]) != sum(cents(c[child_index]) for c in children):
                problems.append(f"Linha {number}: C100 {name} não confere com os C190")
        if fields[3] or any(fields[i] for i in range(22, 29)):
            problems.append(f"Linha {number}: NFC-e não informa COD_PART nem ST/IPI/PIS/COFINS no C100")
        seen = set()
        for child in children:
            key = (child[1], child[2], child[3])
            if key in seen:
                problems.append(f"Linha {number}: C190 repetido para CST/CFOP/alíquota {key}")
            seen.add(key)

    debits = 0
    for number, reg, fields in rows:
        if reg == "C100":
            close(current, analytic)
            current, analytic = (number, fields), []
            key = fields[8]
            if fields[5] in ("00", "02") and not valid_key(key):
                problems.append(f"Linha {number}: chave de acesso inválida")
            elif key and ((cnpj and key[6:20] != cnpj) or key[20:22] != fields[4]
                          or int(key[22:25]) != int(fields[6] or 0) or int(key[25:34]) != int(fields[7] or 0)):
                problems.append(f"Linha {number}: chave não corresponde a CNPJ/modelo/série/número")
        elif reg == "C190":
            analytic.append(fields)
            if fields[2][:1] in ("5", "6", "7"):
                debits += cents(fields[6])
        elif reg == "C990" or reg[0] != "C":
            close(current, analytic)
            current, analytic = None, []
    close(current, analytic)
    e110 = next((f for _, r, f in rows if r == "E110"), None)
    if e110:
        v = [cents(x) for x in e110[1:]]
        if v[0] != debits:
            problems.append("E110 VL_TOT_DEBITOS diferente da soma do ICMS dos C190 de saída")
        balance = (v[0] + v[1] + v[2] + v[3]) - (v[4] + v[5] + v[6] + v[7] + v[8])
        if v[9] != max(balance, 0) or v[12] != max(-balance, 0) or v[11] != v[9] - v[10]:
            problems.append("E110 com saldo apurado/recolher/transportar inconsistente")
        e116_total = sum(cents(f[2]) for _, r, f in rows if r == "E116")
        if e116_total != v[11] + v[13]:
            problems.append("Soma dos E116 diferente de VL_ICMS_RECOLHER + DEB_ESP")
    return problems
