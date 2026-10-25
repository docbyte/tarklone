#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-python3}"
OUT="$ROOT/builds/ubuntu/dist"

if ! "$PYTHON" -c "import tkinter" >/dev/null 2>&1; then
    echo "Fehler: tkinter fehlt. Unter Ubuntu: sudo apt install python3-tk python3-venv" >&2
    exit 1
fi

"$PYTHON" -m venv "$ROOT/.venv-ubuntu"
# shellcheck disable=SC1091
. "$ROOT/.venv-ubuntu/bin/activate"
python -m pip install --disable-pip-version-check --upgrade pip
python -m pip install \
    --disable-pip-version-check \
    -e "$ROOT" \
    pyinstaller \
    python-xlib \
    secretstorage

python -m PyInstaller \
    --noconfirm \
    --clean \
    --distpath "$OUT" \
    "$ROOT/builds/ubuntu/tarklone-ubuntu.spec"

chmod +x "$OUT/tarklone-ubuntu"
echo
echo "Fertig: $OUT/tarklone-ubuntu"
echo "Hinweis: Für das Tray-Icon wird eine X11/XWayland-Session benötigt."
