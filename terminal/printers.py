"""Non-fiscal thermal receipt and label rendering plus local spool transports."""

import base64
import json
import os
import socket
import shutil
import subprocess
import time
import textwrap
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from pathlib import Path


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


def _money(cents):
    cents = int(cents)
    whole, fraction = divmod(abs(cents), 100)
    amount = f"{whole:,}".replace(",", ".")
    return f"R$ {'-' if cents < 0 else ''}{amount},{fraction:02d}"


def _clean(value, length=80):
    return "".join(ch for ch in str(value) if ch.isprintable() and ch not in "^~\r\n").strip()[:length]


def _document(value):
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) == 14:
        return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"
    if len(digits) == 11:
        return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"
    return _clean(value or "", 30)


def _quantity(milli):
    whole, fraction = divmod(milli, 1000)
    return f"{whole},{str(fraction).zfill(3).rstrip('0')}" if fraction else str(whole)


def _receipt_text(value):
    # Keep receipt text to single-byte printable glyphs, so column counts match
    # Epson font A. Never allow data to become ESC/POS commands.
    return " ".join(str(value or "").split()).encode("cp850", "replace").decode("cp850").translate(
        {code: None for code in range(32)})


def _receipt_date(value):
    try:
        date = (datetime.fromtimestamp(value / 1000, ZoneInfo("America/Sao_Paulo"))
                if isinstance(value, (int, float)) else datetime.fromisoformat(str(value).replace("Z", "+00:00")))
        if date.tzinfo:
            date = date.astimezone(ZoneInfo("America/Sao_Paulo"))
        return date.strftime("%d/%m/%Y %H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return _receipt_text(value)


def receipt_layout(sale, width=80):
    """One layout shared by the browser and the raw ESC/POS spool.

    Width is paper width; font A uses 12 dots per character at 203 dpi.
    The 80 mm TM-T20 has a 576-dot (72 mm) printable area / 48 columns.
    58 mm uses a conservative 384-dot area / 32 columns across printers.
    """
    width = 58 if width == 58 else 80
    columns = 32 if width == 58 else 48
    blocks = []

    def text(value, align="left", bold=False, tall=False, wide=False):
        lines = textwrap.wrap(_receipt_text(value), columns // 2 if wide else columns, break_long_words=True,
                              break_on_hyphens=False) or [""]
        blocks.append({"kind": "text", "align": align, "bold": bold, "tall": tall, "wide": wide, "lines": lines})

    def pair(label, value, bold=False, tall=False):
        label, value = _receipt_text(label), _receipt_text(value)
        # Long labels/large totals wrap rather than cutting a value or column.
        if len(label) + len(value) + 1 > columns:
            text(label, bold=bold)
            text(value, align="right", bold=bold, tall=tall)
        else:
            blocks.append({"kind": "text", "align": "left", "bold": bold, "tall": tall,
                           "lines": [label.ljust(columns - len(value)) + value]})

    def rule():
        blocks.append({"kind": "rule"})

    issuer = sale.get("issuer") or {}
    fiscal = sale.get("fiscal") or {}
    authorized = bool(fiscal.get("authorized") and fiscal.get("access_key") and fiscal.get("protocol"))
    trade_name, legal_name = issuer.get("trade_name"), issuer.get("legal_name")
    text(trade_name or legal_name or "COMPROVANTE DE VENDA", "center", bold=True, tall=True, wide=True)
    if trade_name and legal_name and trade_name != legal_name:
        text(legal_name, "center")
    document = _document(issuer.get("document"))
    if document:
        text(f"CNPJ/CPF: {document}", "center")
    if issuer.get("state_registration"):
        text(f"IE: {issuer['state_registration']}", "center")
    address = issuer.get("address") or {}
    street = ", ".join(str(address[key]) for key in ("line", "number") if address.get(key))
    if address.get("complement"):
        street += f" - {address['complement']}"
    if street:
        text(street, "center")
    place = " - ".join(str(address[key]) for key in ("neighborhood", "city", "state") if address.get(key))
    if place:
        text(place, "center")
    if address.get("zip"):
        zipcode = "".join(ch for ch in str(address['zip']) if ch.isdigit())
        text("CEP: " + (zipcode[:5] + "-" + zipcode[5:] if len(zipcode) == 8 else str(address['zip'])), "center")
    rule()
    text("DANFE NFC-e" if authorized else "COMPROVANTE NAO FISCAL", "center", bold=True)
    if sale.get("status") == "voided":
        text("*** VENDA CANCELADA ***", "center", bold=True, tall=True)
    if sale.get("created_at"):
        text(_receipt_date(sale['created_at']), "center")
    text(f"Venda: {str(sale.get('id', ''))[:16]}")
    if sale.get("terminal_name"):
        text(f"Caixa: {sale['terminal_name']}")
    if sale.get("operator_name"):
        text(f"Operador: {sale['operator_name']}")
    rule()
    text("ITENS DA COMPRA", bold=True)
    for index, item in enumerate(sale.get("items", []), 1):
        code = f"{item['sku']} - " if item.get("sku") else ""
        description = f"{index:02d} {code}{item['name']}"
        if item.get("ncm"):
            description += f" | NCM: {item['ncm']}"
        text(description, bold=True)
        qty = (_quantity(item['quantity_milli']) if item['unit'] == "UN"
               else f"{item['quantity_milli'] // 1000},{item['quantity_milli'] % 1000:03d}")
        unit_label = f"{qty} {item['unit']} | Unit:"
        unit_price = _money(item['unit_price_cents'])
        item_total = f"Total item: {_money(item['line_total_cents'])}"
        if len(unit_label) + len(unit_price) + len(item_total) + 2 <= columns:
            pair(f"{unit_label} {unit_price}", item_total)
        else:
            # Narrow paper and large amounts keep each price with its label.
            pair(unit_label, unit_price)
            pair("Total item:", _money(item['line_total_cents']))
        if item.get("discount_cents"):
            pair("Desconto no item", _money(item['discount_cents']))
    rule()
    pair("Quantidade de itens", str(len(sale.get("items", []))))
    pair("Subtotal", _money(sale['gross_cents']))
    pair("Descontos", _money(sale['discount_cents']))
    text("")
    pair("TOTAL A PAGAR", _money(sale['total_cents']), bold=True, tall=True)
    rule()
    pair("FORMA DE PAGAMENTO", "VALOR PAGO", bold=True)
    methods = {"cash": "Dinheiro", "pix": "Pix", "card": "Cartao", "card_credit": "Cartao de credito",
               "card_debit": "Cartao de debito"}
    for payment in sale.get("payments", []):
        pair(methods.get(payment['method'], payment['method']), _money(payment['amount_cents']))
    if len(sale.get("payments", [])) > 1:
        pair("Total recebido", _money(sum(p['amount_cents'] for p in sale['payments'])))
    if any(p['method'] == "cash" for p in sale.get("payments", [])):
        pair("Troco", _money(sale.get('change_cents', 0)), bold=True)
    rule()
    if sale.get("customer_name") or sale.get("customer_document"):
        text("CONSUMIDOR", "center", bold=True)
        if sale.get("customer_name"):
            text(sale['customer_name'], "center")
        if sale.get("customer_document"):
            text(f"CPF/CNPJ: {_document(sale['customer_document'])}", "center")
    else:
        text("CONSUMIDOR NAO IDENTIFICADO", "center")
    if authorized:
        rule()
        text(f"NFC-e: {fiscal.get('number', '')}  Serie: {fiscal.get('series', '')}", "center", bold=True)
        text("CHAVE DE ACESSO", "center")
        key = str(fiscal['access_key']).replace(" ", "")
        text(" ".join(key[i:i + 4] for i in range(0, len(key), 4)), "center")
        text(f"Protocolo: {fiscal['protocol']}", "center")
        if fiscal.get("authorized_at"):
            text(_receipt_date(fiscal['authorized_at']), "center")
        qr_url = fiscal.get("qr_url")
        if qr_url and len(str(qr_url).encode('utf-8')) <= 700:
            # Include a four-module quiet zone when sizing the symbol. Epson
            # silently skips QR codes whose modules exceed its print area.
            try:
                import qrcode
                qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4)
                qr.add_data(str(qr_url), optimize=0); qr.make(fit=True)
                modules = len(qr.get_matrix())
            except ImportError:
                modules = 117  # conservative upper bound for the accepted payload
            module_size = min(5 if width == 58 else 6, (columns * 12) // modules)
            blocks.append({"kind": "qr", "value": qr_url, "data_uri": qr_svg_data_uri(qr_url),
                           "module_size": module_size, "width_mm": round(modules * module_size * 25.4 / 203, 2),
                           "label": "Consulte pelo QR Code"})
        if fiscal.get("taxes_cents") is not None:
            pair("Tributos informados", _money(fiscal['taxes_cents']))
    else:
        rule()
        text("NFC-e nao emitida", "center")
        text("Documento sem valor fiscal", "center")
    text("")
    text("OBRIGADO PELA PREFERENCIA!", "center", bold=True)
    text("Volte sempre", "center")
    return {"paper_width_mm": width, "printable_width_mm": 48 if width == 58 else 72,
            "columns": columns, "bottom_feed_mm": 20, "blocks": blocks}


def receipt_bytes(sale, width=80):
    """Render the shared receipt with explicit Epson width, font and alignment."""
    layout = receipt_layout(sale, width)
    dots = layout['columns'] * 12
    output = bytearray(b"\x1b@")  # initialize before selecting the area/font
    output.extend(b"\x1dP\xcb\xcb")  # 203 dpi motion units
    output.extend(b"\x1dL\x00\x00\x1dW" + dots.to_bytes(2, 'little'))
    output.extend(b"\x1bM\x00\x1b \x00\x1bt\x02\x1b3\x20")  # font A, no spacing, CP850, 32-dot lines
    for block in layout['blocks']:
        kind = block['kind']
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
    # Function B advances to the cutter plus 20 mm, compensating the
    # TM-T20 printhead-to-cutter gap before the partial cut.
    output.extend(b"\x1d!\x00\x1bE\x00\x1ba\x00\x1dVB\xa0")
    return bytes(output)


def label_bytes(product, fmt="zpl"):
    """Basic ZPL/EPL label. Verify dimensions against the physical printer."""
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
