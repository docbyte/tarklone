$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}

.\.venv\Scripts\python.exe -m pip install --disable-pip-version-check --upgrade pip
.\.venv\Scripts\python.exe -m pip install --disable-pip-version-check -e . pyinstaller

.\.venv\Scripts\python.exe -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --onefile `
    --name tarklone `
    --icon assets\tarklone.ico `
    --add-data "assets;assets" `
    --hidden-import keyring.backends.Windows `
    --hidden-import pystray._win32 `
    --hidden-import watchdog.observers.winapi `
    --hidden-import tkinter `
    --hidden-import _tkinter `
    launcher.py

Write-Host ""
Write-Host "Fertig: $PSScriptRoot\dist\tarklone.exe"
