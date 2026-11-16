"""PDV2Server.exe: the local terminal server, started by the service host."""
import os
import sys
import traceback


def crash_log():
    # A windowed program must never die with a traceback dialog nobody can see.
    base = os.environ.get("PDV_TERMINAL_DATA") or os.path.join(
        os.environ.get("ProgramData") or os.path.expanduser("~"), "PDV2")
    folder = os.path.join(base, "logs")
    try:
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "server-crash.log"), "a", encoding="utf-8") as stream:
            stream.write(traceback.format_exc() + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    try:
        from terminal.service_win import run_server
        code = run_server()
    except Exception:
        crash_log()
        code = 1
    sys.exit(code)
