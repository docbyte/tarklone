# -*- mode: python ; coding: utf-8 -*-
"""Tray-GUI-Variante für Ubuntu/Linux (X11/XWayland)."""

from pathlib import Path

ROOT = Path(SPECPATH).parent.parent

a = Analysis(
    [str(ROOT / "launcher.py")],
    pathex=[],
    binaries=[],
    datas=[(str(ROOT / "assets"), "assets")],
    hiddenimports=[
        "tkinter",
        "_tkinter",
        "PIL._tkinter_finder",
        "keyring.backends.SecretService",
        "pystray._xorg",
        "Xlib.display",
        "Xlib.threaded",
        "Xlib.XK",
        "watchdog.observers.inotify",
        "watchdog.observers.polling",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pystray._win32",
        "pystray._darwin",
        "AppKit",
        "Foundation",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="tarklone-ubuntu",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(ROOT / "assets" / "tarklone.png")],
)
