"""What the PDV2 desktop shortcut runs: make sure the cashier is up and open it.

Single instance, no console. Checks ``/health``, starts the service when it is
not answering and opens the cashier in an application window (Edge, then Chrome,
then the default browser). Standard library only; runs on macOS and Linux too.
"""

import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from . import paths, updater

SERVICE_NAME = "PDV2Service"
LAUNCHER_EXE = "PDV2.exe"
START_TIMEOUT = 40
TITLE = "PDV2"


# ------------------------------------------------------------------ browser

def browser_candidates(platform=None, env=None):
    """Ordered (kind, path-or-command) pairs; Edge first, Chrome as the alternative."""
    env = os.environ if env is None else env
    platform = platform or sys.platform
    if paths.is_windows(platform):
        roots = [env.get(name) for name in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        roots = [root for root in roots if root]
        edge = [("edge", root + r"\Microsoft\Edge\Application\msedge.exe") for root in roots]
        chrome = [("chrome", root + r"\Google\Chrome\Application\chrome.exe") for root in roots]
        return edge + chrome
    if platform == "darwin":
        return [("edge", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                ("chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                ("chrome", "/Applications/Chromium.app/Contents/MacOS/Chromium")]
    return [("edge", "microsoft-edge"), ("edge", "microsoft-edge-stable"), ("chrome", "google-chrome"),
            ("chrome", "google-chrome-stable"), ("chrome", "chromium"), ("chrome", "chromium-browser")]


def find_browser(platform=None, env=None, exists=os.path.exists, which=shutil.which):
    """First installed browser that supports an application window, or None."""
    for kind, candidate in browser_candidates(platform, env):
        if "/" in candidate or "\\" in candidate:
            if exists(candidate):
                return kind, candidate
        else:
            found = which(candidate)
            if found:
                return kind, found
    return None


def browser_command(path, url, profile_dir):
    """Application window without address bar, on a profile used only by the PDV2."""
    return [str(path), f"--app={url}", f"--user-data-dir={profile_dir}", "--no-first-run",
            "--no-default-browser-check", "--disable-background-mode", "--start-maximized"]


def cashier_url(env=None):
    return f"http://127.0.0.1:{paths.terminal_port(env)}/"


def profile_dir(env=None, platform=None):
    return paths._flavor(platform).join(paths.user_data_dir(env, platform), "browser-profile")


# ---------------------------------------------------------- single instance

def acquire_single_instance(lock_path):
    """Open handle while this is the only launcher, or None when another one runs."""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def focus_existing(pid_file):
    """Windows only: bring the already open cashier window to the front."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        pid = int(Path(pid_file).read_text().strip())
        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd, _):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
            return True

        user32.EnumWindows(visit, 0)
        for hwnd in found[:1]:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
        return bool(found)
    except Exception:
        return False


# ------------------------------------------------------------------ service

def start_service(platform=None, run=subprocess.run, popen=subprocess.Popen):
    """Ask for the local server: the Windows service, or a process elsewhere."""
    if paths.is_windows(platform):
        result = run(["sc", "start", SERVICE_NAME], capture_output=True,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        # 1056: the service is already running (still starting up).
        return result.returncode in (0, 1056)
    popen([sys.executable, "-m", "terminal"], cwd=str(Path(__file__).resolve().parents[1]),
          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
          start_new_session=True)
    return True


def ensure_running(probe, start, timeout=START_TIMEOUT, sleep=time.sleep, clock=time.monotonic):
    """True when /health answers, starting the server once if it does not."""
    if probe():
        return True
    try:
        start()
    except Exception:
        pass
    return updater.await_health(probe, timeout, sleep=sleep, clock=clock)


# ----------------------------------------------------------------- messages

def not_running_message(env=None, platform=None):
    logs = paths._flavor(platform).join(paths.data_dir(env, platform, installed=True), "logs")
    return ("O PDV2 não conseguiu iniciar neste computador.\n\n"
            "Reinicie o computador e abra o PDV2 de novo. Se continuar, chame o suporte e informe "
            f"que o serviço PDV2 não respondeu em 127.0.0.1:{paths.terminal_port(env)}.\n\n"
            f"Registros para o suporte: {logs}")


def no_browser_message(url):
    return ("O PDV2 está funcionando, mas não foi possível abrir o navegador.\n\n"
            f"Abra o Microsoft Edge ou o Google Chrome e digite o endereço:\n{url}")


def show_message(text, platform=None):
    """Dialog in the user's session; falls back to stderr when there is none."""
    platform = platform or sys.platform
    try:
        if paths.is_windows(platform):
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, TITLE, 0x30)  # MB_ICONWARNING
            return
        if platform == "darwin":
            script = 'display dialog (item 1 of argv) with title "PDV2" buttons {"OK"} default button 1'
            subprocess.run(["osascript", "-e", "on run argv", "-e", script, "-e", "end run", text],
                           capture_output=True, timeout=120)
            return
    except Exception:
        pass
    if sys.stderr is not None:
        print(text, file=sys.stderr)


# --------------------------------------------------------------- delegation

def delegate_target(env=None, executable=None, frozen=None, platform=None, exists=os.path.exists):
    """The stable launcher in bin/ hands over to the launcher of the version in use."""
    env = os.environ if env is None else env
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if not frozen or env.get("PDV_LAUNCHER_DELEGATED") == "1":
        return None
    flavor = paths._flavor(platform)
    executable = executable or sys.executable
    if flavor.basename(flavor.dirname(executable)) != paths.STABLE_DIR:
        return None
    root = paths.install_root(env, executable, frozen=True, platform=platform)
    version = paths.read_pointer(root)
    if not version:
        return None
    target = flavor.join(root, paths.app_dir_name(version), LAUNCHER_EXE)
    return target if exists(target) else None


# --------------------------------------------------------------------- main

def open_cashier(url, env=None, platform=None, popen=subprocess.Popen, find=find_browser,
                 fallback=webbrowser.open):
    """Returns the browser process (application window), True (default browser) or None."""
    found = find(platform, env)
    if found:
        folder = profile_dir(env, platform)
        try:
            os.makedirs(folder, exist_ok=True)
            return popen(browser_command(found[1], url, folder), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
    try:
        return True if fallback(url) else None
    except Exception:
        return None


def main(argv=None, env=None):
    env = os.environ if env is None else env
    target = delegate_target(env)
    if target:
        try:
            subprocess.Popen([target], env={**env, "PDV_LAUNCHER_DELEGATED": "1"})
            return 0
        except OSError:
            pass  # fall through and open the cashier from here
    user_dir = Path(paths.user_data_dir(env))
    pid_file = user_dir / "launcher.pid"
    lock = acquire_single_instance(user_dir / "launcher.lock")
    if lock is None:
        focus_existing(pid_file)
        return 0
    try:
        url = cashier_url(env)
        if not ensure_running(lambda: updater.probe_health(paths.health_url(env)), start_service):
            show_message(not_running_message(env))
            return 1
        opened = open_cashier(url, env)
        if opened is None:
            show_message(no_browser_message(url))
            return 1
        if opened is not True:
            try:
                pid_file.write_text(str(opened.pid))
            except OSError:
                pass
            # Keep the lock while the window is open so a second click does not open another one.
            opened.wait()
            pid_file.unlink(missing_ok=True)
        return 0
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
