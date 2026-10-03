#!/usr/bin/env python3
"""Small host-side bridge from the Docker terminal to native CUPS.

Docker Desktop does not expose macOS USB devices or the local CUPS socket to a
container. The bridge keeps that host-only access in one process and accepts
only authenticated, raw ESC/POS jobs from the local terminal container.
"""

import base64
import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


TOKEN = os.environ.get("PDV_PRINT_BRIDGE_TOKEN", "")
HOST = os.environ.get("PDV_PRINT_BRIDGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("PDV_PRINT_BRIDGE_PORT", "8020"))


def queues():
    result = subprocess.run(
        ["/usr/bin/lpstat", "-p"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=4,
        check=False,
    )
    found = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0].lower() in ("printer", "impressora") and fields[1] not in found:
            found.append(fields[1])
    return found


class Handler(BaseHTTPRequestHandler):
    server_version = "PDVPrintBridge/1.0"

    def log_message(self, *_args):
        return

    def send_json(self, status, body):
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def authorized(self):
        return bool(TOKEN) and self.headers.get("X-PDV-Bridge-Token", "") == TOKEN

    def do_GET(self):
        if self.path == "/health":
            self.send_json(200, {"ok": True})
            return
        if self.path == "/queues" and self.authorized():
            try:
                self.send_json(200, {"ok": True, "queues": queues()})
            except (OSError, subprocess.SubprocessError) as exc:
                self.send_json(502, {"ok": False, "error": str(exc)})
            return
        self.send_json(404 if self.path != "/queues" else 401, {"ok": False, "error": "Não autorizado"})

    def do_POST(self):
        if self.path != "/print" or not self.authorized():
            self.send_json(401, {"ok": False, "error": "Não autorizado"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 2_000_000:
                raise ValueError("Carga de impressão inválida")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            queue = str(body.get("queue", "")).strip()
            if not queue or any(char in queue for char in "\r\n"):
                raise ValueError("Fila CUPS inválida")
            payload = base64.b64decode(str(body.get("payload", "")), validate=True)
            if not payload or len(payload) > 1_500_000:
                raise ValueError("Conteúdo de impressão inválido")
            if queue not in queues():
                raise ValueError("Fila CUPS não encontrada")
            result = subprocess.run(
                ["/usr/bin/lp", "-d", queue, "-o", "raw"],
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                check=False,
            )
            if result.returncode:
                message = result.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(message or "A fila CUPS recusou o trabalho")
            self.send_json(200, {"ok": True, "destination": queue})
        except (ValueError, OSError, subprocess.SubprocessError, RuntimeError, json.JSONDecodeError) as exc:
            self.send_json(400, {"ok": False, "error": str(exc)})


def main():
    if not TOKEN:
        raise SystemExit("PDV_PRINT_BRIDGE_TOKEN não configurado")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Ponte de impressão PDV2 em http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
