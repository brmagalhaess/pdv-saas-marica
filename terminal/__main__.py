"""Start the local terminal with Waitress (shop server or station, see runtime.py)."""

import os

from waitress import serve

from . import runtime


def main():
    host = os.environ.get("PDV_TERMINAL_HOST", "127.0.0.1")
    port = int(os.environ.get("PDV_TERMINAL_PORT", "8010"))
    current = runtime.create()
    role = "caixa adicional" if current.mode == "station" else "caixa principal"
    print(f"Terminal PDV local ({role}): http://{host}:{port}", flush=True)
    serve(current.app, host=host, port=port, threads=6)


if __name__ == "__main__":
    main()
