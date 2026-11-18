"""Etiquetas da balança (pesados) e de gôndola para etiquetadoras térmicas.

Puro: sem banco e sem rede. O código de barras de pesados segue o MESMO formato que
o caixa já lê (terminal-core.js scaleLabel/scaleLabelQuantity):

    2 + PLU (5 dígitos) + peso em gramas OU preço total em centavos (5 dígitos) + 0 + DV

(13 dígitos: a 12ª posição é um 0 de preenchimento, que o caixa ignora; ex.: 2543210125005.)

Linguagens: ZPL (Zebra e compatíveis), EPL (Zebra/Eltron antigas), PPLA (Argox, Elgin
L42/L42 Pro) e TSPL (TSC, Elgin L42 DT novas, genéricas chinesas). Todas a 203 dpi
(8 pontos por milímetro), que é a resolução das etiquetadoras de balcão comuns.
"""

import unicodedata
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

LABEL_LANGUAGES = ("zpl", "epl", "ppla", "tspl")
LANGUAGE_NAMES = {"zpl": "ZPL (Zebra e compatíveis)", "epl": "EPL (Zebra/Eltron)",
                  "ppla": "PPLA (Argox, Elgin)", "tspl": "TSPL (TSC, Elgin, genéricas)"}
LABEL_SIZES = {"40x30": (40, 30), "50x30": (50, 30), "60x40": (60, 40)}
DEFAULT_SIZE = "50x30"
BARCODE_MODES = ("weight", "price")
DOTS_PER_MM = 8
MAX_PAYLOAD = 99999
MAX_COPIES = 50


# Tabelas locais da balança (criadas pelo Store): conferência das etiquetas e validade por produto.
SCALE_SCHEMA = """
CREATE TABLE IF NOT EXISTS scale_labels(
    id TEXT PRIMARY KEY, product_id TEXT NOT NULL, product_name TEXT NOT NULL, scale_code TEXT NOT NULL,
    weight_milli INTEGER NOT NULL, price_cents INTEGER NOT NULL, total_cents INTEGER NOT NULL,
    barcode TEXT NOT NULL, barcode_mode TEXT NOT NULL, copies INTEGER NOT NULL, manual INTEGER NOT NULL,
    authorizer_id TEXT, operator_id TEXT NOT NULL, station_id TEXT NOT NULL, output TEXT NOT NULL,
    job_id TEXT, packed_at INTEGER NOT NULL, valid_until TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS scale_labels_created ON scale_labels(created_at);
CREATE INDEX IF NOT EXISTS scale_labels_product ON scale_labels(product_id, created_at);
CREATE TABLE IF NOT EXISTS scale_product_settings(
    product_id TEXT PRIMARY KEY, validity_days INTEGER NOT NULL, updated_at INTEGER NOT NULL);
"""


class LabelError(ValueError):
    """Etiqueta impossível com os dados informados (mensagem para o lojista)."""


# ---------- EAN-13 ----------

def ean13_check_digit(digits12):
    digits12 = str(digits12)
    if len(digits12) != 12 or not digits12.isdigit():
        raise LabelError("O código precisa de 12 dígitos para calcular o dígito verificador")
    total = sum(int(digit) * (3 if index % 2 else 1) for index, digit in enumerate(digits12))
    return str((10 - total % 10) % 10)


def ean13_valid(code):
    code = str(code or "")
    return len(code) == 13 and code.isdigit() and ean13_check_digit(code[:12]) == code[12]


def plu5(scale_code):
    """PLU de 5 dígitos do produto, ou erro claro quando o cadastro não cabe na etiqueta."""
    text = str(scale_code or "").strip()
    if not text:
        raise LabelError("Este produto não tem código de balança (PLU). Cadastre no painel, em Produtos.")
    if not text.isdigit() or int(text) <= 0 or int(text) > MAX_PAYLOAD:
        raise LabelError("O código de balança deste produto precisa ser um número de 1 a 99999. "
                         "Corrija no painel, em Produtos.")
    return f"{int(text):05d}"


def line_total(price_cents, weight_milli):
    """Mesmo arredondamento da venda (store._line_gross): meio centavo para cima."""
    return int((Decimal(price_cents) * Decimal(weight_milli) / Decimal(1000)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP))


def weighed_barcode(scale_code, price_cents, weight_milli, mode="weight"):
    """EAN-13 de pesado e o valor da etiqueta. mode: 'weight' (gramas) ou 'price' (centavos)."""
    if mode not in BARCODE_MODES:
        raise LabelError("Modo da etiqueta inválido")
    if type(weight_milli) is not int or weight_milli <= 0:
        raise LabelError("Peso inválido: coloque o produto na balança")
    if weight_milli > MAX_PAYLOAD:
        raise LabelError("Peso acima de 99,999 kg não cabe no código da etiqueta")
    if type(price_cents) is not int or price_cents <= 0:
        raise LabelError("Produto sem preço por quilo. Corrija no painel antes de etiquetar.")
    total = line_total(price_cents, weight_milli)
    if mode == "price":
        if total <= 0:
            raise LabelError("Valor zero: o peso é pequeno demais para este preço")
        if total > MAX_PAYLOAD:
            raise LabelError("Valor acima de R$ 999,99 não cabe na etiqueta no modo preço. "
                             "Divida em duas embalagens ou use o modo peso.")
        payload = total
    else:
        payload = weight_milli
    body = "2" + plu5(scale_code) + f"{payload:05d}" + "0"
    return {"barcode": body + ean13_check_digit(body), "payload": payload, "total_cents": total}


def decode_weighed(code):
    """Leitura de volta, igual a terminal-core.js scaleLabel (usada nos testes de ida e volta)."""
    code = str(code or "").strip()
    if len(code) != 13 or not code.startswith("2") or not ean13_valid(code):
        return None
    return {"scale_code": code[1:6], "payload": int(code[6:11])}


# Padrões do EAN-13 (A = ímpar, B = par, C = lado direito) para a pré-visualização em SVG.
_EAN_A = ("0001101", "0011001", "0010011", "0111101", "0100011", "0110001", "0101111", "0111011", "0110111", "0001011")
_EAN_B = tuple("".join("1" if bit == "0" else "0" for bit in pattern)[::-1] for pattern in _EAN_A)
_EAN_C = tuple("".join("1" if bit == "0" else "0" for bit in pattern) for pattern in _EAN_A)
_EAN_PARITY = ("AAAAAA", "AABABB", "AABBAB", "AABBBA", "ABAABB", "ABBAAB", "ABBBAA", "ABABAB", "ABABBA", "ABBABA")


def ean13_modules(code):
    """95 módulos (1 = barra) do EAN-13; mesma saída que scale-core.js ean13Modules."""
    if not ean13_valid(code):
        raise LabelError("EAN-13 inválido")
    parity = _EAN_PARITY[int(code[0])]
    left = "".join((_EAN_A if parity[index] == "A" else _EAN_B)[int(digit)] for index, digit in enumerate(code[1:7]))
    right = "".join(_EAN_C[int(digit)] for digit in code[7:13])
    return "101" + left + "01010" + right + "101"


# ---------- textos da etiqueta ----------

def _amount(cents):
    value = f"{abs(int(cents)) / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("-" if int(cents) < 0 else "") + value


def money_text(cents):
    return "R$ " + _amount(cents)


def weight_text(milli):
    return f"{int(milli) // 1000},{int(milli) % 1000:03d} kg"


def ascii_text(value, length=60):
    """Sem acento e sem caracteres de controle: as etiquetadoras usam páginas de código diferentes."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in text)
    return " ".join(text.split())[:length]


def label_fields(product, weight_milli, barcode_mode="weight", validity_days=None, store_name="", now=None):
    """Tudo o que vai impresso numa etiqueta de pesado (textos já formatados)."""
    now = now or datetime.now()
    code = weighed_barcode(product.get("scale_code"), product.get("price_cents"), weight_milli, barcode_mode)
    valid_until = None
    if validity_days:
        valid_until = (now + timedelta(days=int(validity_days))).date()
    return {
        "store_name": str(store_name or "").strip()[:60],
        "product_name": str(product.get("name") or "").strip()[:80],
        "plu": plu5(product.get("scale_code")),
        "weight_milli": weight_milli,
        "price_cents": int(product["price_cents"]),
        "total_cents": code["total_cents"],
        "barcode": code["barcode"],
        "barcode_mode": barcode_mode,
        "weight_text": weight_text(weight_milli),
        "unit_price_text": money_text(product["price_cents"]) + "/kg",
        "total_text": money_text(code["total_cents"]),
        "packed_at": now.strftime("%d/%m/%Y %H:%M"),
        "valid_until": valid_until.strftime("%d/%m/%Y") if valid_until else "",
        "validity_days": int(validity_days or 0),
    }


# ---------- layout comum (pontos a 203 dpi) ----------

def _size(size):
    if size not in LABEL_SIZES:
        raise LabelError("Tamanho de etiqueta inválido")
    width_mm, height_mm = LABEL_SIZES[size]
    return width_mm * DOTS_PER_MM, height_mm * DOTS_PER_MM


def _fit(text, width_dots, char_dots):
    return text[:max(4, int(width_dots // char_dots))]


def _short_date(text):
    """'18/11/2026 14:03' -> '18/11/26 14:03' (cabe em etiquetas de 40 mm)."""
    return text[:6] + text[8:] if len(text) >= 10 and text[2] == "/" and text[5] == "/" else text


def weighed_layout(fields, size):
    """Elementos posicionados: ('text', x, y, classe, texto) e ('ean13', x, y, altura, código).

    Classes de letra: tiny, small, normal, large (cada linguagem escolhe a fonte equivalente).
    """
    width, height = _size(size)
    tall = height >= 300  # 60x40: letras maiores
    margin = 12
    usable = width - 2 * margin
    heights = {"tiny": 16, "small": 22 if tall else 18, "normal": 30 if tall else 24, "large": 44 if tall else 36}
    store = ascii_text(fields.get("store_name"), 40)
    name = ascii_text(fields.get("product_name"), 80)
    items = []
    y = 8
    if store:
        items.append(("text", margin, y, "small", _fit(store, usable, heights["small"] * 0.55)))
        y += heights["small"] + 4
    items.append(("text", margin, y, "normal", _fit(name, usable, heights["normal"] * 0.6)))
    y += heights["normal"] + 6
    items.append(("text", margin, y, "small",
                  _fit(f"{fields['weight_text']} x {ascii_text(fields['unit_price_text'])}", usable,
                       heights["small"] * 0.55)))
    y += heights["small"] + 6
    total = "Total " + ascii_text(fields["total_text"])
    if len(total) * heights["large"] * 0.55 > usable:
        total = ascii_text(fields["total_text"])
    items.append(("text", margin, y, "large", total))
    y += heights["large"] + 6
    dates = "Emb " + _short_date(fields["packed_at"])
    if fields.get("valid_until"):
        dates += " Val " + _short_date(fields["valid_until"] + " ").strip()
    dates_class = "tiny" if len(dates) * heights["small"] * 0.55 > usable else "small"
    items.append(("text", margin, y, dates_class, _fit(dates, usable, heights[dates_class] * 0.5)))
    y += heights[dates_class] + 6
    # EAN-13 com módulo de 2 pontos: 95 módulos = 190 pontos, centralizado; números embaixo.
    bar_height = max(36, height - y - 30)
    items.append(("ean13", max(margin, (width - 190) // 2), y, bar_height, fields["barcode"]))
    return width, height, items


# ---------- linguagens ----------

def _zpl(width, height, items, copies, tall):
    sizes = {"tiny": 16, "small": 22 if tall else 18, "normal": 30 if tall else 24, "large": 44 if tall else 36}
    parts = [f"^XA^CI0^PW{width}^LL{height}^LH0,0"]
    for kind, x, y, extra, value in items:
        if kind == "text":
            text = value.replace("^", " ").replace("~", " ")
            h = sizes[extra]
            parts.append(f"^FO{x},{y}^A0N,{h},{h}^FD{text}^FS")
        else:
            # ^BE recebe 12 dígitos e calcula o verificador; ^BY2 = módulo de 2 pontos.
            parts.append(f"^FO{x},{y}^BY2^BEN,{extra},Y,N^FD{value[:12]}^FS")
    parts.append(f"^PQ{copies},0,1,Y^XZ")
    return "".join(parts).encode("ascii", "replace")


def _epl(width, height, items, copies, tall):
    # Fontes residentes: 1 = 8x12, 2 = 10x16, 3 = 12x20, 4 = 14x24 pontos.
    fonts = {"tiny": ("1", 1, 1), "small": ("2", 1, 1), "normal": ("3" if not tall else "4", 1, 1), "large": ("4", 1, 2 if tall else 1)}
    lines = ["", "N", f"q{width}", f"Q{height},24"]
    for kind, x, y, extra, value in items:
        if kind == "text":
            text = value.replace('"', "'").replace("\\", "/")
            font, hmul, vmul = fonts[extra]
            lines.append(f'A{x},{y},0,{font},{hmul},{vmul},N,"{text}"')
        else:
            # E30 = EAN-13 (12 dígitos, verificador calculado pela impressora); B = com números.
            lines.append(f'B{x},{y},0,E30,2,2,{extra},B,"{value[:12]}"')
    lines.append(f"P{copies}")
    return ("\n".join(lines) + "\n").encode("ascii", "replace")


def _ppla(width, height, items, copies, tall):
    """PPLA (Argox/Elgin, família Datamax DPL): modo métrico, coordenadas em 0,1 mm.

    Registro: rotação(1) fonte/código(1) mult. largura(1) mult. altura(1) tamanho(3)
    linha(4) coluna(4) dados. A linha é medida a partir da BASE da etiqueta.
    'F' = EAN-13 com os números legíveis.
    """
    fonts = {"tiny": ("1", 1, 1, 12), "small": ("2", 1, 1, 16), "normal": ("3" if not tall else "4", 1, 1, 24 if not tall else 30),
             "large": ("4", 1, 2 if tall else 1, 44 if tall else 30)}

    def tenth_mm(dots):
        return int(round(dots * 10 / DOTS_PER_MM))

    lines = ["\x02L", "m", "D11", "H14", "PC"]
    for kind, x, y, extra, value in items:
        if kind == "text":
            font, hmul, vmul, glyph = fonts[extra]
            text = value.replace("\x02", " ")
            row = tenth_mm(max(0, height - y - glyph * vmul))
            lines.append(f"1{font}{hmul}{vmul}000{row:04d}{tenth_mm(x):04d}{text}")
        else:
            row = tenth_mm(max(0, height - y - extra - 24))
            lines.append(f"1F22{tenth_mm(extra):03d}{row:04d}{tenth_mm(x):04d}{value[:12]}")
    lines.append(f"Q{copies:04d}")
    lines.append("E")
    return ("\r".join(lines) + "\r").encode("ascii", "replace")


def _tspl(width, height, items, copies, tall):
    fonts = {"tiny": ("1", 1, 1), "small": ("2", 1, 1), "normal": ("3", 1, 1), "large": ("4", 1, 1 if not tall else 2)}
    lines = [f"SIZE {width // DOTS_PER_MM} mm,{height // DOTS_PER_MM} mm", "GAP 2 mm,0 mm",
             "DIRECTION 1", "REFERENCE 0,0", "CLS"]
    for kind, x, y, extra, value in items:
        if kind == "text":
            text = value.replace('"', "'")
            font, xmul, ymul = fonts[extra]
            lines.append(f'TEXT {x},{y},"{font}",0,{xmul},{ymul},"{text}"')
        else:
            lines.append(f'BARCODE {x},{y},"EAN13",{extra},1,0,2,2,"{value[:12]}"')
    lines.append(f"PRINT 1,{copies}")
    return ("\r\n".join(lines) + "\r\n").encode("ascii", "replace")


_RENDERERS = {"zpl": _zpl, "epl": _epl, "ppla": _ppla, "tspl": _tspl}


def weighed_label_bytes(fields, language, size=DEFAULT_SIZE, copies=1):
    """Bytes prontos para a etiquetadora (uma tarefa com N cópias)."""
    if language not in _RENDERERS:
        raise LabelError("Linguagem de etiqueta inválida")
    if type(copies) is not int or not 1 <= copies <= MAX_COPIES:
        raise LabelError(f"Cópias devem ficar entre 1 e {MAX_COPIES}")
    width, height, items = weighed_layout(fields, size)
    return _RENDERERS[language](width, height, items, copies, height >= 300)


def shelf_label_bytes(product, language):
    """Etiqueta de gôndola (nome, preço e código) em PPLA ou TSPL; ZPL/EPL ficam em printers.label_bytes."""
    name = ascii_text(product.get("name"), 38)
    unit = " /kg" if product.get("unit") == "KG" else ""
    price = ascii_text(money_text(product.get("price_cents") or 0)) + unit
    barcode = "".join(ch for ch in str(product.get("barcode") or product.get("id") or "")
                      if ch.isascii() and ch.isalnum())[:32]
    if language == "ppla":
        lines = ["\x02L", "m", "D11", "H14", "PC",
                 f"1311000{240:04d}{20:04d}{name.replace(chr(2), ' ')}",
                 f"1411000{165:04d}{20:04d}{price}"]
        if ean13_valid(barcode):
            lines.append(f"1F22080{25:04d}{20:04d}{barcode[:12]}")
        elif barcode:
            lines.append(f"1E22080{25:04d}{20:04d}{barcode}")  # E = Code 128
        lines += ["Q0001", "E"]
        return ("\r".join(lines) + "\r").encode("ascii", "replace")
    if language == "tspl":
        lines = ["SIZE 50 mm,30 mm", "GAP 2 mm,0 mm", "DIRECTION 1", "REFERENCE 0,0", "CLS",
                 f'TEXT 16,12,"3",0,1,1,"{name.replace(chr(34), chr(39))}"',
                 f'TEXT 16,48,"4",0,1,1,"{price}"']
        if ean13_valid(barcode):
            lines.append(f'BARCODE 16,100,"EAN13",80,1,0,2,2,"{barcode[:12]}"')
        elif barcode:
            lines.append(f'BARCODE 16,100,"128",80,1,0,2,2,"{barcode}"')
        lines.append("PRINT 1,1")
        return ("\r\n".join(lines) + "\r\n").encode("ascii", "replace")
    raise LabelError("Linguagem de etiqueta inválida")
