# Build-Varianten

Dieses Verzeichnis enthält die plattformspezifischen Builds. Alle Varianten
enthalten denselben Funktionsumfang; die CLI-Variante lässt Tray und
Einstellungsfenster bewusst weg.

| Variante | Bauen auf | Skript | Ergebnis |
|---|---|---|---|
| Windows GUI | Windows | `builds/windows/build.ps1` | `dist/tarklone-single.exe` |
| Ubuntu GUI | Ubuntu/Linux | `builds/ubuntu/build.sh` | `builds/ubuntu/dist/tarklone-ubuntu` |
| macOS GUI | macOS | `builds/macos/build.sh` | `builds/macos/dist/tarklone.app` + `.zip` |
| CLI Linux/macOS | Linux/macOS | `builds/cli/build.sh` | `builds/cli/dist/<os>/tarklone-cli` |
| CLI Windows | Windows | `builds/cli/build.ps1` | `builds/cli/dist/windows/tarklone-cli.exe` |

## Ubuntu GUI

Systempakete:

```bash
sudo apt update
sudo apt install -y python3-venv python3-tk \
  libx11-6 libxext6 libxrender1 libxtst6 libxi6 libxfixes3 \
  libxdamage1 libxcomposite1 libxrandr2 libxcursor1 libxinerama1
```

Bauen:

```bash
bash builds/ubuntu/build.sh
./builds/ubuntu/dist/tarklone-ubuntu
```

Das Tray-Icon nutzt den X11-Backend von `pystray`. Unter Wayland wird
XWayland benötigt; in GNOME kann zusätzlich eine AppIndicator-Erweiterung
nötig sein. Die GUI ist unter Linux derzeit als **experimentell** zu betrachten.

## macOS GUI

Muss auf macOS gebaut werden (PyInstaller kann nicht cross-kompilieren).

```bash
bash builds/macos/build.sh
open builds/macos/dist/tarklone.app
```

Wenn `tkinter` fehlt:

```bash
brew install python-tk@3.12
```

Das Ergebnis ist ein `.app`-Bundle mit `LSUIElement` (kein Dock-Symbol) und
einem ZIP-Archiv. Die macOS-Tray-Integration ist derzeit **experimentell**.

## Reine CLI

Die CLI enthält Login, Sync, Status, Init und Probe; **kein** Tray, kein
Tkinter und keine GUI-Abhängigkeiten.

```bash
# Linux/macOS
bash builds/cli/build.sh
./builds/cli/dist/<os>/tarklone-cli --help

# Windows
.\builds\cli\build.ps1
.\builds\cli\dist\windows\tarklone-cli.exe --help
```

Beispiele:

```bash
tarklone-cli login
tarklone-cli status
tarklone-cli sync --dry-run --verbose
tarklone-cli sync
```

Auf headless Linux-Systemen wird der OAuth-Token ohne SecretService als
Fallback in `token.json` (Dateimodus 0600) gespeichert. Ein Keyring
(z. B. GNOME Keyring) ist sicherer und wird bevorzugt, ist aber für die CLI
nicht zwingend erforderlich.

## GitHub Actions

`.github/workflows/build-platforms.yml` baut alle Varianten auf
`ubuntu-latest`, `macos-latest` und `windows-latest` und lädt sie als
Artifacts hoch. Bei einem Tag `v*` werden die Artefakte zusätzlich an das
GitHub-Release angehängt.

## Version

Alle Builds bleiben bei **1.1.1**. Die Version wird zentral aus
`pyproject.toml`, `src/tarklone/__init__.py` und
`packaging/version_info.txt` übernommen.
