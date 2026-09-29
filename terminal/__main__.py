"""Start the local terminal with Waitress."""

import os

from waitress import serve

from .app import create_app


def main():
    host = os.environ.get("PDV_TERMINAL_HOST", "127.0.0.1")
    port = int(os.environ.get("PDV_TERMINAL_PORT", "8010"))
    app = create_app({"START_WORKER": True})
    print(f"Terminal PDV local: http://{host}:{port}", flush=True)
    serve(app, host=host, port=port, threads=6)


if __name__ == "__main__":
    main()
