"""Chave de acesso (44 posições) da NF-e/NFC-e, dígito verificador e cNF.

Layout (MOC 7.0, item 5.4): cUF(2) AAMM(4) CNPJ(14) mod(2) serie(3) nNF(9) tpEmis(1) cNF(8) cDV(1).
cDV: módulo 11 com pesos 2..9 da direita para a esquerda; resto 0 ou 1 -> DV 0.
CNPJ alfanumérico (IN RFB 2.229/2024, NT 2025.xxx): cada caractere vale ``ord(c) - 48``
no cálculo do DV, o que mantém o resultado idêntico para chaves só com dígitos.
"""
import re
import secrets

from shared.fiscal.data import UF_CODES

KEY_SHAPE = re.compile(r"[0-9]{6}[0-9A-Z]{12}[0-9]{26}")


def mod11(value):
    total, weight = 0, 2
    for char in reversed(str(value)):
        total += (ord(char) - 48) * weight
        weight = 2 if weight == 9 else weight + 1
    rest = total % 11
    return 0 if rest in (0, 1) else 11 - rest


def random_cnf(number, rng=None):
    """Código numérico (8 dígitos) sem padrões que a SEFAZ recusa: igual ao nNF,
    todos os dígitos iguais ou sequências 12345678/87654321."""
    rng = rng or secrets.SystemRandom()
    forbidden = {str(number % 10**8).zfill(8), "12345678", "23456789", "87654321", "98765432", "01234567"}
    while True:
        code = str(rng.randrange(0, 10**8)).zfill(8)
        if code not in forbidden and len(set(code)) > 1:
            return code


def access_key(uf, year_month, cnpj, model, series, number, emission_type, cnf):
    """year_month: "AAMM". Returns the 44-character key (with DV)."""
    cuf = UF_CODES.get(uf, uf if str(uf).isdigit() else "")
    if len(str(cuf)) != 2:
        raise ValueError("UF inválida para a chave de acesso")
    document = str(cnpj).upper().rjust(14, "0")
    base = (str(cuf) + str(year_month).zfill(4) + document + str(model).zfill(2) + str(series).zfill(3)
            + str(number).zfill(9) + str(emission_type) + str(cnf).zfill(8))
    if len(base) != 43:
        raise ValueError("Dados da chave de acesso com tamanho inválido")
    return base + str(mod11(base))


def valid_key(key):
    key = str(key or "")
    return bool(KEY_SHAPE.fullmatch(key)) and mod11(key[:43]) == int(key[43])


def split_key(key):
    return {"cuf": key[0:2], "year_month": key[2:6], "cnpj": key[6:20], "model": key[20:22],
            "series": int(key[22:25]), "number": int(key[25:34]), "emission_type": key[34],
            "cnf": key[35:43], "dv": key[43]}


def formatted(key):
    """Groups of 4 for the DANFE."""
    return " ".join(key[index:index + 4] for index in range(0, len(key), 4))
