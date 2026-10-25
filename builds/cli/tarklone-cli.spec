# -*- mode: python ; coding: utf-8 -*-
"""Einzelne CLI-EXE/-Binary ohne Tray/GUI-Abhängigkeiten."""

import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent.parent

hidden = []
if sys.platform == "win32":
    hidden.append("keyring.backends.Windows")
elif sys.platform == "darwin":
    hidden.append("keyring.backends.macOS")
else:
    hidden.extend(
        [
            "keyring.backends.SecretService",
            "keyring.backends.libsecret",
        ]
    )

a = Analysis(
    [str(ROOT / "cli_launcher.py")],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "_tkinter",
        "pystray",
        "PIL",
        "watchdog",
        "tarklone.app",
        "tarklone.ui",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

kwargs = {}
if sys.platform == "win32":
    kwargs["icon"] = str(ROOT / "assets" / "tarklone.ico")

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="tarklone-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    **kwargs,
)
