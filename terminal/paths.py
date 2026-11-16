"""Install and data locations of the terminal. Standard library only.

Installed layout (Windows):
    %ProgramFiles%\\PDV2\\current.txt        version in use, e.g. "2.0.1"
    %ProgramFiles%\\PDV2\\app-<version>\\     one folder per installed version
    %ProgramFiles%\\PDV2\\bin\\               stable service host and shortcut target
    %ProgramData%\\PDV2\\                     database, backups, logs and downloads
"""

import ntpath
import os
import posixpath
import re
import sys
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
    return "2.0.0"


def terminal_port(env=None):
    env = os.environ if env is None else env
    try:
        port = int(env.get("PDV_TERMINAL_PORT") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT
    return port if 0 < port < 65536 else DEFAULT_PORT


def health_url(env=None):
    return f"http://127.0.0.1:{terminal_port(env)}/health"
