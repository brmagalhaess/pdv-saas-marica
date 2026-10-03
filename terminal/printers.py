"""Non-fiscal thermal receipt and label rendering plus local spool transports."""

import base64
import json
import os
import socket
import shutil
import subprocess
import time
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
                       box_size=scale, border=2)
    qr.add_data(str(value)); qr.make(fit=True)
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
    return f"R$ {cents // 100},{cents % 100:02d}"


def _clean(value, length=80):
    return "".join(ch for ch in str(value) if ch.isprintable() and ch not in "^~\r\n").strip()[:length]


def _document(value):
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) == 14:
        return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"
    if len(digits) == 11:
        return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"
    return _clean(value, 30)


def _quantity(milli):
    whole, fraction = divmod(milli, 1000)
    return f"{whole},{str(fraction).zfill(3).rstrip('0')}" if fraction else str(whole)


def receipt_bytes(sale, width=58):
    """ESC/POS receipt with company identity and optional authorized NFC-e data.

    The fiscal block is printed only when a provider supplies an authorization,
    access key and protocol. Offline/local sales remain explicitly non-fiscal.
    """
    columns = 32 if width == 58 else 48
    divider = "-" * columns
    issuer = sale.get("issuer") or {}
    fiscal = sale.get("fiscal") or {}
    authorized = bool(fiscal.get("authorized") and fiscal.get("access_key"))
    title = "DANFE NFC-e" if authorized else "COMPROVANTE NAO FISCAL"
    lines = [title]
    trade_name = _clean(issuer.get("trade_name") or "", columns)
    legal_name = _clean(issuer.get("legal_name") or "", columns)
    if trade_name and trade_name != legal_name:
        lines.append(trade_name)
    if legal_name:
        lines.append(legal_name)
    document = _document(issuer.get("document") or "")
    if document:
        lines.append(f"CNPJ/CPF: {document}")
    if issuer.get("state_registration"):
        lines.append(f"IE: {_clean(issuer['state_registration'], columns - 4)}")
    address = issuer.get("address") or {}
    address_line = " ".join(str(address.get(key) or "").strip() for key in
                             ("line", "number", "complement", "neighborhood", "city", "state") if address.get(key))
    if address_line:
        lines.append(_clean(address_line, columns))
    if address.get("zip"):
        lines.append(f"CEP: {_clean(address['zip'], columns - 5)}")
    lines.extend([divider, f"Venda {str(sale['id'])[:16]}"])
    if sale.get("status") == "voided":
        lines.append("VENDA CANCELADA")
    lines.append(divider)
    for item in sale["items"]:
        qty = f"{_quantity(item['quantity_milli'])} {item['unit']}"
        lines.append(_clean(item["name"], columns))
        if item.get("sku"):
            lines.append(f"Codigo: {_clean(item['sku'], columns - 8)}")
        if item.get("ncm"):
            lines.append(f"NCM: {_clean(item['ncm'], columns - 5)}")
        label = f"{qty} x {_money(item['unit_price_cents'])}"
        total = _money(item["line_total_cents"])
        lines.append((label[:columns - len(total) - 1]).ljust(columns - len(total)) + total)
    lines.extend([divider, f"Bruto: {_money(sale['gross_cents'])}",
                  f"Desconto: {_money(sale['discount_cents'])}",
                  f"TOTAL: {_money(sale['total_cents'])}"])
    for payment in sale["payments"]:
        lines.append(f"{payment['method'].upper()}: {_money(payment['amount_cents'])}")
    if sale["change_cents"]:
        lines.append(f"Troco: {_money(sale['change_cents'])}")
    if sale.get("customer_name") or sale.get("customer_document"):
        lines.append(divider)
        if sale.get("customer_name"):
            lines.append(f"Cliente: {_clean(sale['customer_name'], columns - 9)}")
        if sale.get("customer_document"):
            lines.append(f"CPF/CNPJ: {_document(sale['customer_document'])}")
    qr_url = fiscal.get("qr_url") if authorized else ""
    if authorized:
        lines.extend([divider, f"NFC-e: {fiscal.get('number', '—')}  Serie: {fiscal.get('series', '—')}",
                      f"Chave: {_clean(fiscal.get('access_key'), columns)}"])
        if fiscal.get("protocol"):
            lines.append(f"Protocolo: {_clean(fiscal['protocol'], columns - 10)}")
        if fiscal.get("authorized_at"):
            lines.append(f"Autorizada em: {_clean(fiscal['authorized_at'], columns - 14)}")
        if qr_url:
            lines.append("Leia o QR Code para consultar")
        if fiscal.get("taxes_cents") is not None:
            lines.append(f"Tributos: {_money(int(fiscal['taxes_cents']))}")
    else:
        lines.extend([divider, "NFC-e nao emitida", "Documento sem valor fiscal"])
    lines.extend(["Obrigado pela preferencia", "", "", "", "", "", "", ""])
    text = "\n".join(lines).encode("cp850", errors="replace")
    qr = _qr_escpos(qr_url)
    if qr:
        marker = "Leia o QR Code para consultar".encode("cp850")
        before, after = text.split(marker, 1)
        return b"\x1b@\x1ba\x00" + before + marker + b"\n\x1ba\x01" + qr + b"\n\x1ba\x00" + after + b"\n\x1dV\x00"
    return b"\x1b@\x1ba\x00" + text + b"\n\x1dV\x00"


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
