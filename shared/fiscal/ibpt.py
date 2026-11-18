"""IBPT table ("De Olho no Imposto", Lei 12.741/2012) reader.

The IBPT publishes one CSV per UF (TabelaIBPTax<UF><versão>.csv), separated by
";" with the header:
codigo;ex;tipo;descricao;nacionalfederal;importadosfederal;estadual;municipal;
vigenciainicio;vigenciafim;chave;versao;fonte
`tipo` 0 = NCM (mercadorias), 1 = NBS and 2 = LC 116 (serviços). Only tipo 0
rows are used by the PDV. Rates are percentages with "." decimals.
"""
import csv
import io
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

REQUIRED = ("codigo", "ex", "tipo", "nacionalfederal", "importadosfederal", "estadual", "municipal")


def _date(value):
    value = (value or "").strip()
    for layout in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, layout).date()
        except ValueError:
            continue
    return None


def _rate(value):
    number = Decimal(str(value).strip().replace(",", ".") or "0")
    if not Decimal(0) <= number <= Decimal(100):
        raise InvalidOperation
    return number


def parse(text):
    """Rows of NCM goods: dicts with ncm, ex, federal_national, federal_imported, state,
    municipal (Decimal), valid_from, valid_until (date|None), version, source, description."""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = text.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    header = [name.strip().lower() for name in (reader.fieldnames or [])]
    reader.fieldnames = header
    missing = [name for name in REQUIRED if name not in header]
    if missing:
        raise ValueError("Arquivo IBPT sem as colunas: " + ", ".join(missing))
    rows, errors = [], 0
    for raw in reader:
        if (raw.get("tipo") or "").strip() != "0":
            continue
        code = "".join(c for c in (raw.get("codigo") or "") if c.isdigit())
        if len(code) != 8:
            errors += 1
            continue
        try:
            rows.append({"ncm": code, "ex": (raw.get("ex") or "").strip()[:3],
                         "federal_national": _rate(raw["nacionalfederal"]),
                         "federal_imported": _rate(raw["importadosfederal"]),
                         "state": _rate(raw["estadual"]), "municipal": _rate(raw["municipal"]),
                         "valid_from": _date(raw.get("vigenciainicio")), "valid_until": _date(raw.get("vigenciafim")),
                         "version": (raw.get("versao") or "").strip()[:20], "source": (raw.get("fonte") or "").strip()[:60],
                         "description": (raw.get("descricao") or "").strip()[:200]})
        except (InvalidOperation, KeyError):
            errors += 1
    return rows, errors


def lookup_from(rows, today=None):
    """Callable(ncm, ex) for the engine over in-memory rows (exact EX first, then EX blank)."""
    today = today or date.today()
    table = {}
    for row in rows:
        if row.get("valid_until") and row["valid_until"] < today:
            continue
        table[(row["ncm"], row.get("ex") or "")] = row

    def lookup(ncm, ex=""):
        return table.get((ncm, ex or "")) or table.get((ncm, ""))
    return lookup
