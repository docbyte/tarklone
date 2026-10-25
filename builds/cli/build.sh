#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-python3}"
PLATFORM="$(uname -s | tr '[:upper:]' '[:lower:]')"
OUT="$ROOT/builds/cli/dist/$PLATFORM"

"$PYTHON" -m venv "$ROOT/.venv-cli"
# shellcheck disable=SC1091
. "$ROOT/.venv-cli/bin/activate"
python -m pip install --disable-pip-version-check --upgrade pip
python -m pip install --disable-pip-version-check -e "$ROOT" pyinstaller
python -m PyInstaller \
    --noconfirm \
    --clean \
    --distpath "$OUT" \
    "$ROOT/builds/cli/tarklone-cli.spec"

echo
echo "Fertig: $OUT/tarklone-cli"
