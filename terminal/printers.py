"""Thermal receipt (DANFE NFC-e or non-fiscal) and label rendering plus local spool transports."""

import base64
import json
import os
import re
import socket
import shutil
import subprocess
import time
import textwrap
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from pathlib import Path

BRAND_LINE = "PDV2 - crowntech.com.br"
FISCAL_STATES = ("none", "authorized", "contingency", "homologation")
HOMOLOGATION_ENVIRONMENTS = ("homologation", "homologacao", "homologação", "sandbox", "2")
PAYMENT_LABELS = {"cash": "Dinheiro", "pix": "Pix", "card": "Cartão", "card_credit": "Cartão de crédito",
                  "card_debit": "Cartão de débito", "credit_account": "A prazo", "store_credit": "Crédito na loja",
                  "other": "Outros"}


def qr_svg_data_uri(value, scale=4):
    """Create a self-contained QR image for the on-screen receipt."""
    if not value:
        return ""
    try:
        import qrcode
    except ImportError:
        return ""
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=scale, border=4)
    qr.add_data(str(value), optimize=0); qr.make(fit=True)
    matrix = qr.get_matrix()
    size = len(matrix); modules = []
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                modules.append(f"<rect x='{x}' y='{y}' width='1' height='1'/>")
    svg = (f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {size} {size}' "
           f"shape-rendering='crispEdges'><rect width='100%' height='100%' fill='white'/><g fill='black'>"
           + "".join(modules) + "</g></svg>")
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode("ascii")


def _qr_escpos(value, size=5):
    """Epson ESC/POS QR command (model 2, error correction M)."""
    if not value:
        return b""
    data = str(value).encode("utf-8")
    if len(data) > 700:
        return b""
    size = max(1, min(int(size), 16))
    store_len = len(data) + 3
    p_l, p_h = store_len & 0xFF, (store_len >> 8) & 0xFF
    return (b"\x1d\x28\x6b\x04\x00\x31\x41\x32\x00"  # model 2
            + b"\x1d\x28\x6b\x03\x00\x31\x43" + bytes([size])
            + b"\x1d\x28\x6b\x03\x00\x31\x45\x31"
            + b"\x1d\x28\x6b" + bytes([p_l, p_h]) + b"\x31\x50\x30" + data
            + b"\x1d\x28\x6b\x03\x00\x31\x51\x30")


def _amount(cents):
    cents = int(cents)
    whole, fraction = divmod(abs(cents), 100)
    amount = f"{whole:,}".replace(",", ".")
    return f"{'-' if cents < 0 else ''}{amount},{fraction:02d}"


def _money(cents):
    return f"R$ {_amount(cents)}"


def _clean(value, length=80):
    return "".join(ch for ch in str(value) if ch.isprintable() and ch not in "^~\r\n").strip()[:length]


def _digits(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _document(value):
    digits = _digits(value)
    if len(digits) == 14:
        return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"
    if len(digits) == 11:
        return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"
    return _clean(value or "", 30)


def _document_label(value, unknown="CNPJ/CPF"):
    return {14: "CNPJ", 11: "CPF"}.get(len(_digits(value)), unknown)


def _quantity(milli):
    whole, fraction = divmod(milli, 1000)
    return f"{whole},{str(fraction).zfill(3).rstrip('0')}" if fraction else str(whole)


def _receipt_text(value):
    # Keep receipt text to single-byte printable glyphs, so column counts match
    # Epson font A. Never allow data to become ESC/POS commands.
    return " ".join(str(value or "").split()).encode("cp850", "replace").decode("cp850").translate(
        {code: None for code in range(32)})


def _receipt_zone():
    try:
        return ZoneInfo("America/Sao_Paulo")
    except Exception:
        # Windows has no IANA database unless tzdata is installed. Brazil has
        # had no daylight saving time since 2019, so a fixed offset is right.
        return timezone(timedelta(hours=-3))


def _receipt_date(value):
    # Runs inside the sale transaction: a date must never abort a sale.
    try:
        zone = _receipt_zone()
        date = (datetime.fromtimestamp(value / 1000, zone)
                if isinstance(value, (int, float)) else datetime.fromisoformat(str(value).replace("Z", "+00:00")))
        if date.tzinfo:
            date = date.astimezone(zone)
        return date.strftime("%d/%m/%Y %H:%M:%S")
    except Exception:
        try:
            return _receipt_text(value)
        except Exception:
            return ""


def _padded(value, length):
    value = str(value if value is not None else "").strip()
    return value.zfill(length) if value.isdigit() else value


def _homologation(sale):
    fiscal = sale.get("fiscal") or {}
    environment = fiscal.get("environment") or (sale.get("issuer") or {}).get("fiscal_environment")
    return str(environment or "").strip().lower() in HOMOLOGATION_ENVIRONMENTS


def fiscal_state(sale):
    """Return none | authorized | contingency | homologation for a sale.

    Reads the explicit fiscal["state"] or the older authorized/access_key/
    protocol shape. Incomplete data is never promoted: a fiscal document needs
    the access key, and an authorized one also needs the protocol.
    """
    fiscal = sale.get("fiscal") or {}
    if not fiscal.get("access_key"):
        return "none"
    state = fiscal.get("state")
    if state not in FISCAL_STATES:
        state = ("authorized" if fiscal.get("authorized") and fiscal.get("protocol")
                 else "contingency" if fiscal.get("contingency") else "none")
    if state in ("authorized", "homologation") and not fiscal.get("protocol"):
        return "none"
    if state == "authorized" and _homologation(sale):
        return "homologation"
    return state


def fiscal_message(state, homologation=False):
    """Lines of the DANFE fiscal message area as (text, bold) pairs."""
    lines = []
    if state == "contingency":
        lines += [("EMITIDA EM CONTINGÊNCIA", True), ("Pendente de autorização", False)]
    if state == "homologation" or (homologation and state == "contingency"):
        lines.append(("EMITIDA EM AMBIENTE DE HOMOLOGAÇÃO - SEM VALOR FISCAL", True))
    return lines


class _Layout:
    """Block builder shared by the receipt and the vouchers.

    Width is paper width; font A uses 12 dots per character at 203 dpi.
    The 80 mm TM-T20 has a 576-dot (72 mm) printable area / 48 columns.
    58 mm uses a conservative 384-dot area / 32 columns across printers.
    """

    def __init__(self, width):
        self.width = 58 if width == 58 else 80
        self.columns = 32 if self.width == 58 else 48
        self.blocks = []

    def _wrap(self, value, limit, balance=False):
        value = _receipt_text(value)
        lines = textwrap.wrap(value, limit, break_long_words=True, break_on_hyphens=False) or [""]
        if balance and len(lines) > 1:
            # Even out centered lines without adding a line or splitting a word.
            start = max(max(len(word) for word in value.split()), -(-len(value) // len(lines)))
            for narrower in range(start, limit):
                even = textwrap.wrap(value, narrower, break_long_words=True, break_on_hyphens=False)
                if len(even) == len(lines):
                    return even
        return lines

    def _append(self, lines, align="left", bold=False, tall=False, wide=False, small=False):
        block = {"kind": "text", "align": align, "bold": bold, "tall": tall, "wide": wide, "lines": lines}
        if small:
            block["small"] = True  # fine print: font B on paper, smaller type on screen
        self.blocks.append(block)

    def text(self, value, align="left", bold=False, tall=False, wide=False, small=False):
        self._append(self._wrap(value, self.columns // 2 if wide else self.columns, align == "center"),
                     align, bold, tall, wide, small)

    def pair(self, label, value, bold=False, tall=False, indent=0):
        label, value = _receipt_text(label), _receipt_text(value)
        if indent + len(label) + len(value) + 1 > self.columns:
            indent = 0  # narrow paper gives up the indent before using a second line
        # Long labels/large totals wrap rather than cutting a value or column.
        if len(label) + len(value) + 1 > self.columns:
            self.text(label, bold=bold)
            self.text(value, align="right", bold=bold, tall=tall)
        else:
            self._append([(" " * indent + label).ljust(self.columns - len(value)) + value], bold=bold, tall=tall)

    def flow(self, parts, sep=" ", align="center", bold=False):
        """Pack short parts into lines; a part that fits a line is never split."""
        lines = []
        for part in (_receipt_text(part) for part in parts):
            if not part:
                continue
            if lines and len(lines[-1]) + len(sep) + len(part) <= self.columns:
                lines[-1] += sep + part
            else:
                lines.extend(self._wrap(part, self.columns))
        if lines:
            self._append(lines, align, bold)

    def url(self, value):
        """Centered address broken after a separator instead of mid-word."""
        value, lines = _receipt_text(value), []
        while len(value) > self.columns:
            cut = max(value.rfind(mark, 1, self.columns) for mark in "/?&=.") + 1 or self.columns
            lines.append(value[:cut])
            value = value[cut:]
        self._append(lines + [value], "center")

    def rule(self):
        self.blocks.append({"kind": "rule"})

    def header(self, issuer, address=True):
        trade_name, legal_name = issuer.get("trade_name"), issuer.get("legal_name")
        if trade_name or legal_name:
            # Double width would leave only 16 columns on 58 mm paper.
            self.text(trade_name or legal_name, "center", bold=True, tall=True, wide=self.width == 80)
        if trade_name and legal_name and trade_name != legal_name:
            self.text(legal_name, "center")
        document = _document(issuer.get("document"))
        self.flow([f"{_document_label(issuer.get('document'))} {document}" if document else "",
                   f"IE {issuer['state_registration']}" if issuer.get("state_registration") else ""], sep="  ")
        address = (issuer.get("address") or {}) if address else {}
        street = ", ".join(str(address[key]) for key in ("line", "number") if address.get(key))
        if address.get("complement"):
            street += f" - {address['complement']}"
        if street:
            self.text(street, "center")
        city = "/".join(str(address[key]) for key in ("city", "state") if address.get(key))
        place = " - ".join(part for part in (str(address.get("neighborhood") or ""), city) if part)
        if place:
            self.text(place, "center")
        if address.get("zip"):
            zipcode = _digits(address["zip"])
            self.text("CEP " + (zipcode[:5] + "-" + zipcode[5:] if len(zipcode) == 8 else str(address["zip"])),
                      "center")
        if self.blocks:
            self.rule()

    def access_key(self, value):
        key = "".join(str(value).split())
        groups = [key[i:i + 4] for i in range(0, len(key), 4)]
        per_line = 6 if self.columns == 48 else 4
        self._append([line for i in range(0, len(groups), per_line)
                      for line in self._wrap(" ".join(groups[i:i + per_line]), self.columns)], "center")

    def qr(self, value):
        value = str(value or "")
        if not value or len(value.encode("utf-8")) > 700:
            # The key printed above still allows the lookup; never fail a sale here.
            self.text("QR Code indisponível. Consulte pela chave de acesso.", "center")
            return
        # Include a four-module quiet zone when sizing the symbol. Epson
        # silently skips QR codes whose modules exceed its print area.
        try:
            import qrcode
            qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4)
            qr.add_data(value, optimize=0); qr.make(fit=True)
            modules = len(qr.get_matrix())
        except ImportError:
            modules = 117  # conservative upper bound for the accepted payload
        # At least 25 mm (200 dots) of symbol whenever the paper allows it.
        wanted = max(3 if self.width == 58 else 4, -(-200 // (modules - 8)))
        module_size = max(1, min(16, wanted, (self.columns * 12) // modules))
        self.blocks.append({"kind": "qr", "value": value, "data_uri": qr_svg_data_uri(value),
                            "module_size": module_size, "width_mm": round(modules * module_size * 25.4 / 203, 2),
                            "label": "Consulta via leitor de QR Code"})

    def brand(self):
        self.text(BRAND_LINE, "center", small=True)

    def result(self, **extra):
        return {"paper_width_mm": self.width, "printable_width_mm": 48 if self.width == 58 else 72,
                "columns": self.columns, "bottom_feed_mm": 20, "blocks": self.blocks, **extra}


def receipt_layout(sale, width=80, copy="Via consumidor"):
    """One layout shared by the browser and the raw ESC/POS spool.

    Follows the DANFE NFC-e order for 80 mm (48 columns) and 58 mm (32 columns).
    sale["fiscal"] drives the document: state (none | authorized | contingency |
    homologation, see fiscal_state), access_key, protocol, number, series,
    issued_at, authorized_at, url_consulta, qr_url, infcpl, taxes_cents,
    taxes_federal_cents / taxes_state_cents / taxes_municipal_cents, taxes_source.
    """
    out = _Layout(width)
    fiscal = sale.get("fiscal") or {}
    items = sale.get("items", [])
    payments = sale.get("payments", [])
    voided = sale.get("status") == "voided"
    # A cancelled sale is never reprinted as a valid DANFE.
    state = "none" if voided else fiscal_state(sale)
    number, series = _padded(fiscal.get("number"), 9), _padded(fiscal.get("series"), 3)

    out.header(sale.get("issuer") or {})
    if state == "none":
        out.text("COMPROVANTE DE VENDA", "center", bold=True)
        out.text("NÃO É DOCUMENTO FISCAL", "center")
    else:
        out.text("DOCUMENTO AUXILIAR DA NOTA FISCAL DE CONSUMIDOR ELETRÔNICA", "center", bold=True)
    if voided:
        out.text("*** VENDA CANCELADA ***", "center", bold=True, tall=True)
    if state == "none" and sale.get("created_at"):
        out.text(_receipt_date(sale["created_at"]), "center")
    out.rule()

    out.text("# CÓDIGO DESCRIÇÃO")
    out.pair("QTDE UN x VL UNIT", "VL TOTAL", indent=4)
    for index, item in enumerate(items, 1):
        out.text(" ".join(part for part in (f"{index:03d}", str(item.get("sku") or ""), str(item["name"])) if part))
        qty = (_quantity(item["quantity_milli"]) if item["unit"] == "UN"
               else f"{item['quantity_milli'] // 1000},{item['quantity_milli'] % 1000:03d}")
        promo, discount = item.get("promo_discount_cents") or 0, item.get("discount_cents") or 0
        gross = item.get("gross_cents")
        if gross is None:  # sales stored before the line kept its own gross value
            gross = item["line_total_cents"] + promo + discount
        out.pair(f"{qty} {item['unit']} x {_amount(item['unit_price_cents'])}", _amount(gross), indent=4)
        if promo:
            out.pair(f"Promoção: {item.get('promotion_name') or ''}", f"-{_amount(promo)}", indent=4)
        if discount:
            out.pair("Desconto no item", f"-{_amount(discount)}", indent=4)
    out.rule()

    out.pair("Qtde. total de itens", str(len(items)))
    out.pair("Valor total R$", _amount(sale["gross_cents"]))
    if sale.get("promo_discount_cents"):
        out.pair("Promoções", f"-{_amount(sale['promo_discount_cents'])}")
    if sale.get("discount_cents"):
        out.pair("Desconto R$", f"-{_amount(sale['discount_cents'])}")
    out.pair("VALOR A PAGAR R$", _amount(sale["total_cents"]), bold=True, tall=True)
    out.pair("FORMA DE PAGAMENTO", "VALOR PAGO", bold=True)
    names = sale.get("payment_names") or {}
    for payment in payments:
        label = names.get(payment.get("payment_method_id")) or PAYMENT_LABELS.get(payment["method"], payment["method"])
        out.pair(label, _amount(payment["amount_cents"]))
    if sale.get("change_cents") or any(payment["method"] == "cash" for payment in payments):
        out.pair("Troco R$", _amount(sale.get("change_cents") or 0))
    out.rule()

    messages = fiscal_message(state, _homologation(sale))
    if messages:
        for message, bold in messages:
            out.text(message, "center", bold=bold)
        out.rule()
    if state != "none":
        url = fiscal.get("url_consulta") or fiscal.get("consult_url")
        if url:
            out.text("Consulte pela Chave de Acesso em", "center")
            out.url(url)
        else:
            out.text("Chave de Acesso", "center")
        out.access_key(fiscal["access_key"])
        out.rule()

    customer_document = _document(sale.get("customer_document"))
    if customer_document:
        customer = f"CONSUMIDOR {_document_label(sale['customer_document'], 'CPF/CNPJ')} {customer_document}"
        out.text(" - ".join(str(part) for part in (customer, sale.get("customer_name")) if part), "center")
    elif sale.get("customer_name"):
        out.text(f"CONSUMIDOR: {sale['customer_name']}", "center")
    else:
        out.text("CONSUMIDOR NÃO IDENTIFICADO", "center")
    out.rule()

    if state != "none":
        issued_at = fiscal.get("issued_at") or sale.get("created_at")
        out.flow([f"NFC-e nº {number}" if number else "NFC-e", f"Série {series}" if series else "",
                  _receipt_date(issued_at) if issued_at else ""], bold=True)
        if state != "contingency":
            out.flow(["Protocolo de autorização:", fiscal["protocol"]])
            if fiscal.get("authorized_at"):
                out.flow(["Data de autorização:", _receipt_date(fiscal["authorized_at"])])
        elif copy:
            out.text(copy, "center")
        out.qr(fiscal.get("qr_url"))
        out.rule()
    elif voided and fiscal.get("access_key"):
        out.flow(["NFC-e CANCELADA", f"nº {number}" if number else "", f"Série {series}" if series else ""],
                 bold=True)
        out.access_key(fiscal["access_key"])
        if fiscal.get("cancel_protocol"):
            out.flow(["Protocolo de cancelamento:", fiscal["cancel_protocol"]])
        out.rule()

    taxes = fiscal.get("taxes_cents")
    if taxes is None:
        taxes = sale.get("taxes_cents")
    if taxes is not None:
        label = ("Tributos Totais Incidentes (Lei Federal 12.741/2012):" if state != "none"
                 else "Tributos aprox. (Lei 12.741/2012):")
        out.text(f"{label} {_money(taxes)}")
        spheres = (("Federal", "taxes_federal_cents"), ("Estadual", "taxes_state_cents"),
                   ("Municipal", "taxes_municipal_cents"))
        out.flow([f"{name} {_money(fiscal[key])}" for name, key in spheres if fiscal.get(key) is not None]
                 + [f"Fonte: {fiscal['taxes_source']}" if fiscal.get("taxes_source") else ""],
                 sep=" · ", align="left")
        out.rule()
    if state != "none" and fiscal.get("infcpl"):
        out.text(fiscal["infcpl"])
    terminal = _receipt_text(sale.get("terminal_name"))
    out.flow([f"Venda: {str(sale['id'])[:8]}" if sale.get("id") else "",
              terminal if not terminal or terminal.lower().startswith("caixa") else f"Caixa: {terminal}",
              f"Operador: {sale['operator_name']}" if sale.get("operator_name") else "",
              f"Vendedor: {sale['seller_name']}" if sale.get("seller_name") else ""], sep=" · ", align="left")
    out.text("Obrigado pela preferência!", "center", bold=True)
    out.brand()
    return out.result(fiscal_state=state)


def receipt_bytes(sale, width=80):
    """Render the shared receipt with explicit Epson width, font and alignment."""
    layout = receipt_layout(sale, width)
    payload = layout_bytes(layout)
    if layout["fiscal_state"] == "contingency":
        # Contingency prints two copies; each one ends with its own partial cut.
        payload += layout_bytes(receipt_layout(sale, width, copy="Via estabelecimento"))
    return payload


# Fixed voucher texts that callers may still send without accents.
_VOUCHER_WORDS = {"DEVOLUCAO": "DEVOLUÇÃO", "Devolucao": "Devolução", "Credito": "Crédito",
                  "disponivel": "disponível"}
_VOUCHER_WORD = re.compile(r"\b(" + "|".join(_VOUCHER_WORDS) + r")\b")


def _voucher_text(value):
    return _VOUCHER_WORD.sub(lambda match: _VOUCHER_WORDS[match.group(1)], str(value or ""))


def voucher_layout(doc, width=80):
    """Non-fiscal voucher (return, fiado payment) sharing the receipt skeleton.

    doc: {"title", "issuer", "created_at", "reference", "rows": [["pair", label, value] |
    ["text", value] | ["rule"]], "customer_name", "signature": bool}
    """
    out = _Layout(width)
    out.header(doc.get("issuer") or {}, address=False)
    out.text(_voucher_text(doc["title"]), "center", bold=True, tall=True)
    out.text("NÃO É DOCUMENTO FISCAL", "center")
    if doc.get("created_at"):
        out.text(_receipt_date(doc["created_at"]), "center")
    if doc.get("reference"):
        out.text(_voucher_text(doc["reference"]))
    out.rule()
    for row in doc.get("rows", []):
        if row[0] == "pair":
            out.pair(_voucher_text(row[1]), _voucher_text(row[2]), bold=len(row) > 3 and row[3])
        elif row[0] == "rule":
            out.rule()
        else:
            out.text(row[1])
    if doc.get("customer_name"):
        out.rule()
        out.text(f"Cliente: {doc['customer_name']}", "center")
    if doc.get("signature"):
        out.text("")
        out.text("")
        out.text("_" * min(out.columns, 36), "center")
        out.text("Assinatura do cliente", "center")
    out.text("")
    out.brand()
    return out.result()


def layout_bytes(layout):
    dots = layout['columns'] * 12
    output = bytearray(b"\x1b@")  # initialize before selecting the area/font
    output.extend(b"\x1dP\xcb\xcb")  # 203 dpi motion units
    output.extend(b"\x1dL\x00\x00\x1dW" + dots.to_bytes(2, 'little'))
    output.extend(b"\x1bM\x00\x1b \x00\x1bt\x02\x1b3\x20")  # font A, no spacing, CP850, 32-dot lines
    for block in layout['blocks']:
        kind = block['kind']
        small = bool(block.get('small'))
        if small:
            output.extend(b"\x1bM\x01")  # font B for the fine print
        alignment = {'left': 0, 'center': 1, 'right': 2}.get(block.get('align'), 0)
        output.extend(b"\x1ba" + bytes([alignment]))
        output.extend(b"\x1bE" + bytes([int(block.get('bold', False))]))
        output.extend(b"\x1d!" + bytes([(1 if block.get('tall') else 0) | (16 if block.get('wide') else 0)]))
        if kind == 'qr':
            output.extend(b"\x1ba\x01" + _qr_escpos(block['value'], size=block['module_size']) + b"\n")
            output.extend(_receipt_text(block['label']).encode('cp850') + b"\n")
        else:
            lines = ['-' * layout['columns']] if kind == 'rule' else block['lines']
            output.extend(('\n'.join(lines) + '\n').encode('cp850', 'replace'))
        if small:
            output.extend(b"\x1bM\x00")
    # Function B advances to the cutter plus 20 mm, compensating the
    # TM-T20 printhead-to-cutter gap before the partial cut.
    output.extend(b"\x1d!\x00\x1bE\x00\x1ba\x00\x1dVB\xa0")
    return bytes(output)


LABEL_PRINTER_REQUIRED = (
    "Etiquetas com intervalo exigem impressora de etiquetas compatível. "
    "A TM-T20 utiliza bobinas de recibo de 58 ou 80 mm. "
    "Prepare as etiquetas 50×30 mm no painel da empresa, em Produtos > Imprimir etiquetas."
)


def label_bytes(product, fmt="zpl"):
    """Render labels for a separately configured ZPL/EPL printer."""
    if fmt == "escpos50x30":
        raise ValueError(LABEL_PRINTER_REQUIRED)
    if fmt in ("ppla", "tspl"):
        from .labels import shelf_label_bytes  # Argox/Elgin (PPLA) e TSC (TSPL)
        return shelf_label_bytes(product, fmt)
    name = _clean(product["name"], 38)
    barcode = "".join(ch for ch in str(product.get("barcode") or product["id"])
                      if ch.isascii() and ch.isalnum())[:32]
    price = _money(product["price_cents"])
    if fmt == "zpl":
        return (f"^XA^PW600^LL280^FO20,20^A0N,34,34^FD{name}^FS"
                f"^FO20,80^A0N,48,48^FD{price}^FS"
                f"^FO20,145^BY2^BCN,85,Y,N,N^FD{barcode}^FS^XZ").encode("ascii", "replace")
    if fmt == "epl":
        # EPL encloses fields in quotes and treats backslashes specially.
        name = name.replace('"', "").replace("\\", "")
        return (f"N\nA20,20,0,3,1,1,N,\"{name}\"\n"
                f"A20,70,0,4,1,1,N,\"{price}\"\n"
                f"B20,130,0,1,2,4,70,B,\"{barcode}\"\nP1\n").encode("ascii", "replace")
    raise ValueError("Formato de etiqueta inválido")


def send_bytes(driver, target, payload, job_id):
    """Send once; caller marks uncertain after any error once a send begins."""
    if driver == "tcp":
        host, port_text = target.rsplit(":", 1)
        port = int(port_text)
        if not host or not 1 <= port <= 65535:
            raise ValueError("Destino TCP inválido")
        with socket.create_connection((host, port), timeout=5) as connection:
            connection.settimeout(5)
            connection.sendall(payload)
        return target
    if driver == "windows_raw":
        try:
            import win32print
        except ImportError as exc:
            raise RuntimeError("Instale pywin32 no Windows para usar RAW spool") from exc
        handle = win32print.OpenPrinter(target)
        try:
            win32print.StartDocPrinter(handle, 1, (f"PDV {job_id}", None, "RAW"))
            try:
                win32print.StartPagePrinter(handle)
                win32print.WritePrinter(handle, payload)
                win32print.EndPagePrinter(handle)
            finally:
                win32print.EndDocPrinter(handle)
        finally:
            win32print.ClosePrinter(handle)
        return target
    if driver == "cups":
        # CUPS is the native print queue on macOS and Linux.  The local
        # terminal must run on the host so it can see a USB printer; a Docker
        # container cannot see the host USB device automatically.
        queue = str(target).strip()
        if not queue or any(ch in queue for ch in "\r\n"):
            raise ValueError("Fila CUPS inválida")
        bridge_url = os.environ.get("PDV_PRINT_BRIDGE_URL", "").rstrip("/")
        if bridge_url:
            token = os.environ.get("PDV_PRINT_BRIDGE_TOKEN", "")
            if not token:
                raise RuntimeError("Ponte local de impressão sem token configurado")
            try:
                result = _bridge_request("POST", "/print", {
                    "queue": queue,
                    "job_id": str(job_id),
                    "payload": base64.b64encode(payload).decode("ascii"),
                })
            except (OSError, HTTPError, URLError, ValueError) as exc:
                raise RuntimeError("Ponte local de impressão indisponível") from exc
            if not isinstance(result, dict) or not result.get("ok"):
                raise RuntimeError(str(result.get("error") if isinstance(result, dict) else "Fila CUPS recusou o trabalho"))
            return str(result.get("destination") or queue)
        if not shutil.which("lp"):
            raise RuntimeError("CUPS não está disponível; execute o terminal nativo neste computador")
        result = subprocess.run(
            ["lp", "-d", queue, "-o", "raw"],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        if result.returncode:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(message or "A fila CUPS recusou o trabalho")
        return queue
    if driver == "file":
        directory = Path(target).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"pdv-{job_id}-{time.time_ns()}.bin"
        with destination.open("xb") as output:
            output.write(payload)
        return str(destination)
    raise ValueError("Driver de impressora inválido")


def cups_queues():
    """Return local CUPS queue names for the native terminal setup screen."""
    bridge_url = os.environ.get("PDV_PRINT_BRIDGE_URL", "").rstrip("/")
    bridge_token = os.environ.get("PDV_PRINT_BRIDGE_TOKEN", "")
    if bridge_url and bridge_token:
        try:
            result = _bridge_request("GET", "/queues")
            queues = result.get("queues") if isinstance(result, dict) else None
            if isinstance(queues, list):
                return [str(queue) for queue in queues if queue]
        except (OSError, HTTPError, URLError, ValueError):
            pass
    if not shutil.which("lpstat"):
        return []
    try:
        result = subprocess.run(["lpstat", "-p"], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=4, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    queues = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0].lower() in ("printer", "impressora"):
            queue = fields[1].strip()
            if queue and queue not in queues:
                queues.append(queue)
    return queues


def _bridge_request(method, path, body=None):
    """Call the authenticated host bridge over Unix socket or HTTP."""
    token = os.environ.get("PDV_PRINT_BRIDGE_TOKEN", "")
    encoded = json.dumps(body).encode("utf-8") if body is not None else b""
    headers = {"X-PDV-Bridge-Token": token}
    if encoded:
        headers["Content-Type"] = "application/json"
    request = Request(
        os.environ.get("PDV_PRINT_BRIDGE_URL", "").rstrip("/") + path,
        data=encoded or None,
        headers={**headers, "Content-Type": "application/json"} if encoded else headers,
        method=method,
    )
    with urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8"))
    return result
