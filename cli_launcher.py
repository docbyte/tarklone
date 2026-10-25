"""PyInstaller entry point für die reine CLI-Variante."""

from tarklone.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
