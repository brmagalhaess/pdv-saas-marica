"""Install and data locations of the terminal. Standard library only.

Installed layout (Windows):
    %ProgramFiles%\\PDV2\\current.txt        version in use, e.g. "2.0.1"
    %ProgramFiles%\\PDV2\\app-<version>\\     one folder per installed version
    %ProgramFiles%\\PDV2\\bin\\               stable service host and shortcut target
    %ProgramData%\\PDV2\\                     database, backups, logs and downloads
"""

import json
import ntpath
import os
import posixpath
import re
import sys
import time
from pathlib import Path

APP_NAME = "PDV2"
POINTER_NAME = "current.txt"
STABLE_DIR = "bin"
DEFAULT_PORT = 8010
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


def parse_version(text):
    """'2.10.3' -> (2, 10, 3). Raises ValueError for anything else."""
    text = str(text).strip()
    if not VERSION_RE.fullmatch(text):
        raise ValueError("Versão inválida: " + text[:40])
    return tuple(int(part) for part in text.split("."))


def is_newer(candidate, current):
    return parse_version(candidate) > parse_version(current)


def app_dir_name(version):
    parse_version(version)
    return "app-" + str(version).strip()


def is_windows(platform=None):
    return (platform or sys.platform).startswith("win")


def _flavor(platform):
    return ntpath if is_windows(platform) else posixpath


def installed_data_dir(env=None, platform=None):
    """Data folder of an installed terminal: shared by the service and every user."""
    env = os.environ if env is None else env
    if is_windows(platform):
        return ntpath.join(env.get("ProgramData") or env.get("PROGRAMDATA") or r"C:\ProgramData", APP_NAME)
    return "/var/lib/pdv2"


def user_data_dir(env=None, platform=None, home=None):
    """Per-user folder: development database, browser profile and launcher lock."""
    env = os.environ if env is None else env
    home = home or env.get("HOME") or str(Path.home())
    if is_windows(platform):
        return ntpath.join(env.get("LOCALAPPDATA") or home, APP_NAME)
    return posixpath.join(home, ".local", "share", "pdv2")


def data_dir(env=None, platform=None, installed=False, home=None):
    """PDV_TERMINAL_DATA always wins; an installed terminal uses the machine folder."""
    env = os.environ if env is None else env
    if env.get("PDV_TERMINAL_DATA"):
        return env["PDV_TERMINAL_DATA"]
    if installed:
        return installed_data_dir(env, platform)
    return user_data_dir(env, platform, home)


def default_cloud_url(env=None, frozen=None):
    """Server the cashier pairs with. PDV_CLOUD_URL wins; the installed program already
    knows the CrownTech server, so nobody types an address at the customer's shop."""
    env = os.environ if env is None else env
    if env.get("PDV_CLOUD_URL"):
        return env["PDV_CLOUD_URL"]
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    return PRODUCTION_CLOUD_URL if frozen else ""


def default_install_root(env=None, platform=None):
    env = os.environ if env is None else env
    if is_windows(platform):
        return ntpath.join(env.get("ProgramFiles") or env.get("PROGRAMFILES") or r"C:\Program Files", APP_NAME)
    return "/opt/pdv2"


def install_root(env=None, executable=None, frozen=None, platform=None):
    """Folder that holds current.txt, or None when running from source code."""
    env = os.environ if env is None else env
    if env.get("PDV_INSTALL_ROOT"):
        return env["PDV_INSTALL_ROOT"]
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if not frozen:
        return None
    flavor = _flavor(platform)
    folder = flavor.dirname(executable or sys.executable)
    name = flavor.basename(folder)
    if name == STABLE_DIR or name.startswith("app-"):
        return flavor.dirname(folder)
    return folder


def read_pointer(root):
    """Version named by current.txt, or None when missing or malformed."""
    if not root:
        return None
    try:
        text = (Path(root) / POINTER_NAME).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text if VERSION_RE.fullmatch(text) else None


def write_pointer(root, version):
    """Atomic replacement: a crash leaves either the old or the new version."""
    parse_version(version)
    root = Path(root)
    temporary = root / (POINTER_NAME + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(str(version).strip() + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, root / POINTER_NAME)


def app_dir(root, version):
    return Path(root) / app_dir_name(version)


def source_version(env=None):
    """Version of the code being executed (set by the service for each launch)."""
    env = os.environ if env is None else env
    value = str(env.get("PDV_VERSION") or "").strip()
    if VERSION_RE.fullmatch(value):
        return value
    base = getattr(sys, "_MEIPASS", None)
    candidates = [Path(base) / "VERSION"] if base else []
    candidates.append(Path(__file__).resolve().parents[1] / "VERSION")
    for candidate in candidates:
        try:
            value = candidate.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if VERSION_RE.fullmatch(value):
            return value
    return "2.1.0"


# Friendly address of the cashier screen. Edge and Chrome resolve any *.localhost name to
# this computer and treat it as a secure origin (Web Serial for the scale keeps working),
# so no hosts-file entry is needed.
FRIENDLY_HOST = "pdv2.localhost"
PRODUCTION_CLOUD_URL = "https://pdv2.crowntech.com.br"
PREFERRED_PORT = 80
PORT_FILE = "porta.txt"


def _valid_port(value):
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return port if 0 < port < 65536 else None


def port_file(data):
    return os.path.join(str(data), PORT_FILE)


def terminal_port(env=None):
    """PDV_TERMINAL_PORT wins; an installed terminal uses the port its service chose (porta.txt)."""
    env = os.environ if env is None else env
    if env.get("PDV_TERMINAL_PORT"):
        return _valid_port(env.get("PDV_TERMINAL_PORT")) or DEFAULT_PORT
    try:
        if read_pointer(install_root(env)) is not None:
            with open(port_file(data_dir(env, installed=True)), encoding="utf-8") as stream:
                return _valid_port(stream.read()) or DEFAULT_PORT
    except OSError:
        pass
    return DEFAULT_PORT


def choose_service_port(env=None, data=None, try_bind=None):
    """Port for the installed service: 80 when free (address without port), else 8010.

    The choice is written to porta.txt so the launcher opens the same address.
    """
    env = os.environ if env is None else env
    if env.get("PDV_TERMINAL_PORT"):
        return _valid_port(env.get("PDV_TERMINAL_PORT")) or DEFAULT_PORT

    def can_bind(port):
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    port = PREFERRED_PORT if (try_bind or can_bind)(PREFERRED_PORT) else DEFAULT_PORT
    if data:
        try:
            os.makedirs(data, exist_ok=True)
            with open(port_file(data), "w", encoding="utf-8") as stream:
                stream.write(str(port))
        except OSError:
            pass
    return port


def cashier_address(port, host=FRIENDLY_HOST):
    return f"http://{host}/" if port == 80 else f"http://{host}:{port}/"


def health_url(env=None):
    return f"http://127.0.0.1:{terminal_port(env)}/health"


# ------------------------------------------------------------------- mode
# One executable, two roles: "server" (caixa principal, owns the database) and
# "station" (caixa adicional, a bridge to the server). The choice is a local file.

MODE_FILE = "mode.json"
MODES = ("server", "station")


def _default_data_dir(env):
    return data_dir(env, installed=read_pointer(install_root(env)) is not None)


def mode_path(data_dir):
    return Path(data_dir) / MODE_FILE


def read_mode(data_dir=None, env=None):
    """PDV_MODE wins; a missing or unreadable file means "server"."""
    env = os.environ if env is None else env
    forced = str(env.get("PDV_MODE") or "").strip().lower()
    if forced in MODES:
        return forced
    folder = _default_data_dir(env) if data_dir is None else data_dir
    try:
        value = json.loads(mode_path(folder).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return "server"
    mode = value.get("mode") if isinstance(value, dict) else None
    return mode if mode in MODES else "server"


def write_mode(data_dir, mode):
    """Atomic replacement, like write_pointer: a crash leaves the old or the new mode."""
    if mode not in MODES:
        raise ValueError("Modo inválido: " + str(mode)[:40])
    target = mode_path(data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(MODE_FILE + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps({"v": 1, "mode": mode, "changed_at": int(time.time())}) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
