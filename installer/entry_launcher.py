"""PDV2.exe: desktop shortcut target (no console)."""
import os
import sys
import traceback


def crash_log():
    # A windowed program must never die with a traceback dialog nobody can see.
    base = os.environ.get("PDV_TERMINAL_DATA") or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "PDV2")
    folder = os.path.join(base, "logs")
    try:
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "launcher-crash.log"), "a", encoding="utf-8") as stream:
            stream.write(traceback.format_exc() + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    try:
        from terminal.launcher import main
        code = main()
    except Exception:
        crash_log()
        try:
            from terminal.launcher import show_message
            show_message("O PDV2 encontrou um erro ao abrir. Reinicie o computador; se continuar, chame o suporte.")
        except Exception:
            pass
        code = 1
    sys.exit(code)
