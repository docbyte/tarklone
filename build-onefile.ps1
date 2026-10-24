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
    packaging\tarklone-onefile.spec

$target = Join-Path $PSScriptRoot "dist\tarklone-single.exe"
if (-not (Test-Path $target)) {
    throw "Build fehlgeschlagen: $target fehlt."
}

$size = [math]::Round((Get-Item $target).Length / 1MB, 1)
Write-Host ""
Write-Host "Fertig: $target ($size MB, eine einzelne Datei)"
