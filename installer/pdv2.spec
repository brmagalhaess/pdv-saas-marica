# -*- mode: python ; coding: utf-8 -*-
# PyInstaller, folder mode. One folder with three programs sharing the runtime:
#   PDV2.exe         desktop shortcut (launcher, no console)
#   PDV2Server.exe   local terminal server (Flask + Waitress, no console)
#   PDV2Service.exe  Windows service host (pywin32)
# Build with installer/build.ps1; the folder becomes app-<version>/ and bin/.
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))
VERSION = open(os.path.join(ROOT, "VERSION"), encoding="utf-8").read().strip()
ICON = os.path.join(SPECPATH, "pdv2.ico")
WINDOWS = sys.platform.startswith("win")


def no_tests(name):
    return ".tests" not in name


datas = [
    (os.path.join(ROOT, "terminal", "templates"), "terminal/templates"),
    (os.path.join(ROOT, "terminal", "static"), "terminal/static"),
    (os.path.join(ROOT, "VERSION"), "."),
]
# Windows has no IANA time zone database; receipts need America/Sao_Paulo.
datas += collect_data_files("tzdata")

hiddenimports = ["tzdata", "qrcode", "qrcode.constants", "qrcode.main"]
hiddenimports += collect_submodules("terminal", filter=no_tests)
hiddenimports += collect_submodules("shared", filter=no_tests)
hiddenimports += collect_submodules("waitress")
if WINDOWS:
    hiddenimports += ["win32timezone", "win32print", "win32api", "win32con", "win32job", "win32event",
                      "win32service", "win32serviceutil", "servicemanager", "pywintypes"]

version_file = None
if WINDOWS:
    numbers = ", ".join(VERSION.split(".") + ["0"])
    # PyInstaller exposes the --workpath folder to the spec as "workpath" (lowercase).
    work = globals().get("workpath") or os.path.join(ROOT, "build", "pyinstaller")
    version_file = os.path.join(work, "version-info.txt")
    os.makedirs(work, exist_ok=True)
    with open(version_file, "w", encoding="utf-8") as stream:
        stream.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({numbers}), prodvers=({numbers}), mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041604B0', [
      StringStruct('CompanyName', 'CrownTech'),
      StringStruct('FileDescription', 'PDV2 - Caixa local'),
      StringStruct('FileVersion', '{VERSION}'),
      StringStruct('ProductName', 'PDV2'),
      StringStruct('ProductVersion', '{VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [1046, 1200])])
  ]
)
""")


def program(script, name):
    analysis = Analysis([os.path.join(SPECPATH, script)], pathex=[ROOT], datas=datas,
                        hiddenimports=hiddenimports, excludes=["tkinter", "pytest"], noarchive=False)
    executable = EXE(PYZ(analysis.pure), analysis.scripts, [], exclude_binaries=True, name=name,
                     console=False, icon=ICON if WINDOWS else None, version=version_file, upx=False)
    return analysis, executable


parts = [program("entry_launcher.py", "PDV2"),
         program("entry_server.py", "PDV2Server"),
         program("entry_service.py", "PDV2Service")]

collected = []
for analysis, executable in parts:
    collected += [executable, analysis.binaries, analysis.datas]
COLLECT(*collected, name="PDV2", upx=False)
