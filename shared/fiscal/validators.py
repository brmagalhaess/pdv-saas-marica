"""Brazilian registration numbers and fiscal codes, in pure Python.

Every function takes the value as typed (punctuation allowed) and answers
True/False; `digits()` / `cnpj_value()` give the normalized form to store.

Sources of the check-digit rules:
- CNPJ/CPF: Receita Federal (módulo 11). Alphanumeric CNPJ (IN RFB 2.229/2024,
  from July 2026): the 12 base characters are worth ord(char) - 48.
- Inscrição Estadual: the "Conferência de Inscrições Estaduais" tables published
  by SINTEGRA for every UF (all 27 implemented; see IE_RULES below).
- Código IBGE de município: 7 digits, the first two are the UF code and the last
  one is a módulo 10 check digit (weights 1-2); nine municipalities created
  before the rule keep a non-conforming digit and are accepted by name.
"""
import re

from .data import UF_CODES

_DIGITS = re.compile(r"\D")


def digits(value):
    return _DIGITS.sub("", str(value or ""))


# --------------------------------------------------------------------- CNPJ/CPF

def cnpj_value(value):
    """Uppercase, without punctuation; letters are kept (alphanumeric CNPJ)."""
    return re.sub(r"[^0-9A-Za-z]", "", str(value or "")).upper()


def _cnpj_dv(base):
    weights = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2][-len(base):]
    total = sum((ord(char) - 48) * weight for char, weight in zip(base, weights))
    rest = total % 11
    return "0" if rest < 2 else str(11 - rest)


def valid_cnpj(value):
    code = cnpj_value(value)
    if not re.fullmatch(r"[0-9A-Z]{12}[0-9]{2}", code) or len(set(code)) == 1:
        return False
    first = _cnpj_dv(code[:12])
    return code[12:] == first + _cnpj_dv(code[:12] + first)


def valid_cpf(value):
    code = digits(value)
    if len(code) != 11 or len(set(code)) == 1:
        return False
    for size in (9, 10):
        total = sum(int(code[i]) * (size + 1 - i) for i in range(size))
        dv = (total * 10) % 11 % 10
        if dv != int(code[size]):
            return False
    return True


# ------------------------------------------------------------------ CEP / IBGE

def valid_cep(value):
    code = digits(value)
    return len(code) == 8 and code != "00000000"


# Municipalities whose official code does not follow the módulo 10 rule.
IBGE_DV_EXCEPTIONS = {"2201919", "2201988", "2202251", "2611533", "3117836", "3152131",
                      "4305871", "5203939", "5203962"}


def valid_ibge_city(value, uf=None):
    code = digits(value)
    if len(code) != 7 or code[:2] not in UF_CODES.values():
        return False
    if uf and UF_CODES.get(str(uf).upper()) != code[:2]:
        return False
    if code in IBGE_DV_EXCEPTIONS:
        return True
    total = 0
    for index, char in enumerate(code[:6]):
        product = int(char) * (1 if index % 2 == 0 else 2)
        total += product // 10 + product % 10
    return (10 - total % 10) % 10 == int(code[6])


# ----------------------------------------------------------------- fiscal codes

def valid_ncm(value):
    code = digits(value)
    return len(code) == 8 and code == str(value).replace(".", "").strip() and code[:2] != "00"


def valid_cest(value):
    code = digits(value)
    return len(code) == 7 and 1 <= int(code[:2]) <= 28


def valid_cfop(value):
    code = digits(value)
    return len(code) == 4 and code[0] in "123567" and code[1:] != "000"


def valid_cbenef(value):
    """cBenef: UF letters plus 6 characters (8) or plus 8 characters (10)."""
    return bool(re.fullmatch(r"[A-Z]{2}[0-9A-Z]{6}([0-9A-Z]{2})?", str(value or "").strip().upper()))


def valid_cnae(value):
    return len(digits(value)) == 7


def valid_gtin(code):
    code = str(code or "")
    if not code.isdigit() or len(code) not in (8, 12, 13, 14):
        return False
    numbers = [int(c) for c in code]
    check = numbers.pop()
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(numbers)))
    return (10 - total % 10) % 10 == check


# --------------------------------------------------------- Inscrição Estadual

def _weighted(numbers, weights):
    return sum(int(n) * w for n, w in zip(numbers, weights))


def _mod11(numbers, weights, low_zero=True):
    """11 - (sum % 11); 10 and 11 become 0 (the most common SINTEGRA variant)."""
    dv = 11 - _weighted(numbers, weights) % 11
    return 0 if dv >= 10 else dv


def _mod11_rest(numbers, weights):
    """Rest 0 or 1 gives 0, otherwise 11 - rest."""
    rest = _weighted(numbers, weights) % 11
    return 0 if rest < 2 else 11 - rest


def _desc(start, count):
    return list(range(start, start - count, -1))


def _ie_ac(c):
    if len(c) != 13 or not c.startswith("01"):
        return False
    first = _mod11(c[:11], [4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    second = _mod11(c[:11] + str(first), [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    return c[11:] == f"{first}{second}"


def _ie_al(c):
    if len(c) != 9 or not c.startswith("24"):
        return False
    dv = _weighted(c[:8], _desc(9, 8)) * 10 % 11
    return int(c[8]) == (0 if dv == 10 else dv)


def _ie_ap(c):
    if len(c) != 9 or not c.startswith("03"):
        return False
    number = int(c[:8])
    if 3000001 <= number <= 3017000:
        p, d = 5, 0
    elif 3017001 <= number <= 3019022:
        p, d = 9, 1
    else:
        p, d = 0, 0
    dv = 11 - (p + _weighted(c[:8], _desc(9, 8))) % 11
    dv = 0 if dv == 10 else d if dv == 11 else dv
    return int(c[8]) == dv


def _ie_am(c):
    if len(c) != 9:
        return False
    total = _weighted(c[:8], _desc(9, 8))
    if total < 11:
        dv = 11 - total
    else:
        rest = total % 11
        dv = 0 if rest <= 1 else 11 - rest
    return int(c[8]) == dv


def _ie_ba(c):
    if len(c) not in (8, 9):
        return False
    modulo = 10 if c[len(c) - 8] in "0123458" else 11
    size = len(c) - 2

    def calc(numbers, weights):
        total = _weighted(numbers, weights)
        if modulo == 10:
            return (10 - total % 10) % 10
        rest = total % 11
        return 0 if rest < 2 else 11 - rest

    second = calc(c[:size], _desc(size + 1, size))
    first = calc(c[:size] + str(second), _desc(size + 2, size + 1))
    return c[size:] == f"{first}{second}"


def _ie_ce(c):
    return len(c) == 9 and int(c[8]) == _mod11(c[:8], _desc(9, 8))


def _ie_df(c):
    if len(c) != 13 or not c.startswith("07"):
        return False
    first = _mod11(c[:11], [4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    second = _mod11(c[:11] + str(first), [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    return c[11:] == f"{first}{second}"


def _ie_es(c):
    return len(c) == 9 and int(c[8]) == _mod11_rest(c[:8], _desc(9, 8))


def _ie_go(c):
    if len(c) != 9 or c[:2] not in ("10", "11", "15", "20", "29"):
        return False
    rest = _weighted(c[:8], _desc(9, 8)) % 11
    if rest == 0:
        dv = 0
    elif rest == 1:
        dv = 1 if 10103105 <= int(c[:8]) <= 10119997 else 0
    else:
        dv = 11 - rest
    return int(c[8]) == dv


def _ie_ma(c):
    return len(c) == 9 and c.startswith("12") and int(c[8]) == _mod11_rest(c[:8], _desc(9, 8))


def _ie_mt(c):
    c = c.zfill(11)
    return len(c) == 11 and int(c[10]) == _mod11_rest(c[:10], [3, 2, 9, 8, 7, 6, 5, 4, 3, 2])


def _ie_ms(c):
    if len(c) != 9 or c[:2] not in ("28", "50"):
        return False
    rest = _weighted(c[:8], _desc(9, 8)) % 11
    dv = 0 if rest == 0 else 11 - rest
    return int(c[8]) == (0 if dv > 9 else dv)


def _ie_mg(c):
    if len(c) != 13:
        return False
    expanded = c[:3] + "0" + c[3:11]
    total = 0
    for index, char in enumerate(expanded):
        product = int(char) * (1 if index % 2 == 0 else 2)
        total += product // 10 + product % 10
    first = (10 - total % 10) % 10
    second = _mod11_rest(c[:11] + str(first), [3, 2, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2])
    return c[11:] == f"{first}{second}"


def _ie_pa(c):
    return len(c) == 9 and c.startswith("15") and int(c[8]) == _mod11_rest(c[:8], _desc(9, 8))


def _ie_pb(c):
    return len(c) == 9 and int(c[8]) == _mod11(c[:8], _desc(9, 8))


def _ie_pr(c):
    if len(c) != 10:
        return False
    first = _mod11_rest(c[:8], [3, 2, 7, 6, 5, 4, 3, 2])
    second = _mod11_rest(c[:8] + str(first), [4, 3, 2, 7, 6, 5, 4, 3, 2])
    return c[8:] == f"{first}{second}"


def _ie_pe(c):
    if len(c) == 14:  # former CACEPE layout: format only
        return True
    if len(c) != 9:
        return False
    first = _mod11_rest(c[:7], _desc(8, 7))
    second = _mod11_rest(c[:7] + str(first), _desc(9, 8))
    return c[7:] == f"{first}{second}"


def _ie_pi(c):
    return len(c) == 9 and int(c[8]) == _mod11(c[:8], _desc(9, 8))


def _ie_rj(c):
    return len(c) == 8 and int(c[7]) == _mod11_rest(c[:7], [2, 7, 6, 5, 4, 3, 2])


def _ie_rn(c):
    if len(c) not in (9, 10) or not c.startswith("20"):
        return False
    size = len(c) - 1
    dv = _weighted(c[:size], _desc(size + 1, size)) * 10 % 11
    return int(c[size]) == (0 if dv == 10 else dv)


def _ie_rs(c):
    return len(c) == 10 and int(c[9]) == _mod11(c[:9], [2, 9, 8, 7, 6, 5, 4, 3, 2])


def _ie_ro(c):
    if len(c) != 14:
        return False
    dv = 11 - _weighted(c[:13], [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]) % 11
    return int(c[13]) == (dv - 10 if dv >= 10 else dv)


def _ie_rr(c):
    if len(c) != 9 or not c.startswith("24"):
        return False
    return int(c[8]) == _weighted(c[:8], range(1, 9)) % 9


def _ie_sc(c):
    return len(c) == 9 and int(c[8]) == _mod11_rest(c[:8], _desc(9, 8))


def _ie_sp(c):
    if c.startswith("P"):  # produtor rural: P + 12 digits, one check digit (9th)
        body = c[1:]
        if len(body) != 12 or not body.isdigit():
            return False
        return int(body[8]) == _weighted(body[:8], [1, 3, 4, 5, 6, 7, 8, 10]) % 11 % 10
    if len(c) != 12:
        return False
    first = _weighted(c[:8], [1, 3, 4, 5, 6, 7, 8, 10]) % 11 % 10
    second = _weighted(c[:11], [3, 2, 10, 9, 8, 7, 6, 5, 4, 3, 2]) % 11 % 10
    return int(c[8]) == first and int(c[11]) == second


def _ie_se(c):
    return len(c) == 9 and int(c[8]) == _mod11(c[:8], _desc(9, 8))


def _ie_to(c):
    if len(c) == 11:  # former layout: digits 3-4 are the company type, outside the check
        if c[2:4] not in ("01", "02", "03", "99"):
            return False
        c = c[:2] + c[4:]
    return len(c) == 9 and int(c[8]) == _mod11_rest(c[:8], _desc(9, 8))


IE_RULES = {
    "AC": _ie_ac, "AL": _ie_al, "AP": _ie_ap, "AM": _ie_am, "BA": _ie_ba, "CE": _ie_ce, "DF": _ie_df,
    "ES": _ie_es, "GO": _ie_go, "MA": _ie_ma, "MT": _ie_mt, "MS": _ie_ms, "MG": _ie_mg, "PA": _ie_pa,
    "PB": _ie_pb, "PR": _ie_pr, "PE": _ie_pe, "PI": _ie_pi, "RJ": _ie_rj, "RN": _ie_rn, "RS": _ie_rs,
    "RO": _ie_ro, "RR": _ie_rr, "SC": _ie_sc, "SP": _ie_sp, "SE": _ie_se, "TO": _ie_to,
}


def ie_value(value):
    """Digits only; São Paulo rural producers keep the leading P; ISENTO stays ISENTO."""
    raw = str(value or "").strip().upper()
    if raw == "ISENTO":
        return raw
    return ("P" if raw.startswith("P") else "") + digits(raw)


def valid_ie(value, uf):
    uf = str(uf or "").upper()
    code = ie_value(value)
    rule = IE_RULES.get(uf)
    if not rule or not code or code == "ISENTO":
        return False
    if code.startswith("P") and uf != "SP":
        return False
    if not code.startswith("P") and (len(set(code)) == 1):
        return False
    try:
        return bool(rule(code))
    except (ValueError, IndexError):
        return False
