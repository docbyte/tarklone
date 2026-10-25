# -*- mode: python ; coding: utf-8 -*-
"""Tray-GUI-Variante für macOS als .app-Bundle."""

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
        "keyring.backends.macOS",
        "pystray._darwin",
        "AppKit",
        "Foundation",
        "objc",
        "PyObjCTools.MachSignals",
        "watchdog.observers.kqueue",
        "watchdog.observers.fsevents",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pystray._win32",
        "pystray._xorg",
        "pystray._gtk",
        "pystray._appindicator",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="tarklone",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(ROOT / "assets" / "tarklone.icns")],
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="tarklone",
)

app = BUNDLE(
    coll,
    name="tarklone.app",
    icon=str(ROOT / "assets" / "tarklone.icns"),
    bundle_identifier="com.docbyte.tarklone",
    info_plist={
        "CFBundleShortVersionString": "1.1",
        "CFBundleVersion": "1.1.0",
        "NSHighResolutionCapable": True,
        "LSUIElement": True,
    },
)
