"""PyInstaller entry point (absolute import, kein relativer Paketimport)."""

from tarklone.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
