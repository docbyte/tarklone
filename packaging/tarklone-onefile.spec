# -*- mode: python ; coding: utf-8 -*-
"""Reproduzierbare Single-EXE-Variante für tarklone.

Bauen mit:
    .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean packaging\tarklone-onefile.spec

Die Spec erzeugt genau eine Datei: dist\tarklone-single.exe.
"""

from pathlib import Path

ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(ROOT / "launcher.py")],
    pathex=[],
    binaries=[],
    datas=[(str(ROOT / "assets"), "assets")],
    hiddenimports=[
        "keyring.backends.Windows",
        "pystray._win32",
        "watchdog.observers.winapi",
        "tkinter",
        "_tkinter",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name="tarklone-single",
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
    icon=[str(ROOT / "assets" / "tarklone.ico")],
    version=str(ROOT / "packaging" / "version_info.txt"),
)
