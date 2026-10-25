#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-python3}"
OUT="$ROOT/builds/macos/dist"

if ! "$PYTHON" -c "import tkinter" >/dev/null 2>&1; then
    echo "Fehler: tkinter fehlt. Bitte eine Python-Installation mit Tk verwenden" >&2
    echo "(z. B. python.org-Installer oder 'brew install python-tk@3.12')." >&2
    exit 1
fi

"$PYTHON" -m venv "$ROOT/.venv-macos"
# shellcheck disable=SC1091
. "$ROOT/.venv-macos/bin/activate"
python -m pip install --disable-pip-version-check --upgrade pip
python -m pip install \
    --disable-pip-version-check \
    -e "$ROOT" \
    pyinstaller \
    pyobjc-core \
    pyobjc-framework-Cocoa

python -m PyInstaller \
    --noconfirm \
    --clean \
    --distpath "$OUT" \
    "$ROOT/builds/macos/tarklone-macos.spec"

if [ -d "$OUT/tarklone.app" ]; then
    rm -f "$OUT/tarklone-macos.zip"
    (
        cd "$OUT"
        ditto -c -k --sequesterRsrc --keepParent tarklone.app tarklone-macos.zip
    )
fi

echo
echo "Fertig: $OUT/tarklone.app"
echo "Archiv:  $OUT/tarklone-macos.zip"
