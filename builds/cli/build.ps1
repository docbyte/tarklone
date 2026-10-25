$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root

if (-not (Test-Path ".venv-cli")) {
    python -m venv .venv-cli
}

.\.venv-cli\Scripts\python.exe -m pip install --disable-pip-version-check --upgrade pip
.\.venv-cli\Scripts\python.exe -m pip install --disable-pip-version-check -e . pyinstaller

.\.venv-cli\Scripts\python.exe -m PyInstaller `
    --noconfirm `
    --clean `
    --distpath "builds\cli\dist\windows" `
    "builds\cli\tarklone-cli.spec"

Write-Host ""
Write-Host "Fertig: $Root\builds\cli\dist\windows\tarklone-cli.exe"
