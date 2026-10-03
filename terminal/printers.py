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


def _money(cents):
    return f"R$ {cents // 100},{cents % 100:02d}"


def _clean(value, length=80):
    return "".join(ch for ch in str(value) if ch.isprintable() and ch not in "^~\r\n").strip()[:length]


def _quantity(milli):
    whole, fraction = divmod(milli, 1000)
    return f"{whole},{str(fraction).zfill(3).rstrip('0')}" if fraction else str(whole)


def receipt_bytes(sale, width=58):
    """ESC/POS receipt; fiscal authorization is deliberately absent."""
    columns = 32 if width == 58 else 48
    divider = "-" * columns
    lines = ["COMPROVANTE NAO FISCAL", divider, f"Venda {str(sale['id'])[:16]}"]
    if sale.get("status") == "voided":
        lines.append("VENDA CANCELADA")
    if sale.get("customer_name"):
        lines.append(f"Cliente: {_clean(sale['customer_name'], columns - 9)}")
    lines.append(divider)
    for item in sale["items"]:
        qty = f"{_quantity(item['quantity_milli'])} {item['unit']}"
        lines.append(_clean(item["name"], columns))
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
    lines.extend([divider, "Documento sem valor fiscal", "Obrigado pela preferencia", "", "", ""])
    text = "\n".join(lines).encode("cp850", errors="replace")
    return b"\x1b@" + text + b"\n\x1dV\x00"


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
