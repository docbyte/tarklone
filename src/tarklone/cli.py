from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .auth import AuthError, NotLoggedIn, OAuthClient, TokenStore
from .config import (
    config_path,
    ensure_dirs,
    load_settings,
    log_path,
    save_settings,
    state_path,
)
from .mcp import McpAuthError, McpClient, McpError
from .tarkle import probe_web_api


def _print_token_status(token) -> None:
    if token is None:
        print("Tarkle-Login:   nicht angemeldet")
        return
    email = token.email or "unbekanntes Konto"
    if token.expires_at:
        import datetime as _dt

        expires = _dt.datetime.fromtimestamp(token.expires_at).strftime("%d.%m.%Y %H:%M")
        print(f"Tarkle-Login:   {email} (Token gültig bis {expires})")
    else:
        print(f"Tarkle-Login:   {email}")


def cmd_login(args: argparse.Namespace) -> int:
    settings = load_settings()
    client = OAuthClient(settings)
    try:
        print("Öffne den Tarkle-Login im Browser …")
        token = client.login(
            timeout_seconds=args.timeout,
            on_url=lambda url: print(f"Falls der Browser nicht öffnet: {url}"),
        )
    finally:
        client.close()
    print(f"Angemeldet als {token.email or 'Tarkle-Konto'}.")
    return 0


def cmd_logout(_args: argparse.Namespace) -> int:
    settings = load_settings()
    OAuthClient(settings).logout()
    print("Tarkle-Login wurde entfernt.")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    settings = load_settings()
    client = OAuthClient(settings)
    try:
        token = client.ensure_valid()
        mcp = McpClient(settings.mcp_url, token.access_token)
        try:
            info = mcp.initialize()
            tools = mcp.list_tools()
        finally:
            mcp.close()
    except NotLoggedIn as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        print("Bitte zuerst `tarklone login` ausführen.", file=sys.stderr)
        return 2
    except McpAuthError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2
    except (AuthError, McpError) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()

    web = probe_web_api(settings, token)
    report: dict[str, Any] = {
        "server": info,
        "tool_count": len(tools),
        "tools": tools,
        "web": web,
    }
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"MCP-Server: {info.get('serverInfo', {}).get('name', 'Tarkle MCP')}")
        print(f"Protokoll:  {info.get('protocolVersion', 'unbekannt')}")
        print(f"Tools:      {len(tools)}")
        print()
        for tool in tools:
            name = tool.get("name", "?")
            description = (tool.get("description") or "").strip().replace("\n", " ")
            print(f"- {name}: {description}")
            schema = tool.get("inputSchema")
            if args.verbose and isinstance(schema, dict):
                print(json.dumps(schema, indent=2, ensure_ascii=False))
        print("\nWeb-API-Prüfung:")
        for name, value in web.items():
            print(f"- {name}: {value}")

    if args.save:
        target = Path(args.save)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        if not args.json:
            print(f"\nReport gespeichert: {target}")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    ensure_dirs()
    settings = load_settings()
    token = TokenStore().load()

    print(f"tarklone {__version__}")
    print(f"Konfiguration:  {settings.local_folder or '—'}")
    if settings.workspace_name or settings.workspace_id:
        print(f"Workspace:      {settings.workspace_name or settings.workspace_id}")
    if settings.remote_folder_name or settings.remote_folder_id:
        print(f"Remote-Ordner:  {settings.remote_folder_name or settings.remote_folder_id}")
    _print_token_status(token)
    print(f"Status:         {'bereit' if settings.is_configured and token else 'Einrichtung nötig'}")

    errors = settings.validation_errors()
    if errors:
        print("\nHinweise:")
        for error in errors:
            print(f"- {error}")
    print(f"\nKonfigdatei:    {config_path()}")
    print(f"Logdatei:       {log_path()}")
    print(f"Zustand:        {state_path()}")
    return 0


def cmd_init(_args: argparse.Namespace) -> int:
    settings = load_settings()
    print("Einrichtung von tarklone")
    print("========================")
    local = input(f"Lokaler Ordner [{settings.local_folder or Path.cwd()}]: ").strip()
    if local:
        settings.local_folder = str(Path(local).expanduser())
    workspace = input(f"Tarkle-Workspace-ID [{settings.workspace_id}]: ").strip()
    if workspace:
        settings.workspace_id = workspace
    folder = input(f"Remote-Ordner-ID (leer = Workspace-Root) [{settings.remote_folder_id}]: ").strip()
    if folder:
        settings.remote_folder_id = folder
    save_settings(settings)
    print("Gespeichert. Anschließend `tarklone login` und `tarklone probe` ausführen.")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    from .state import SyncState
    from .sync import SyncEngine, SyncError
    from .tarkle import TarkleRemote
    from .webapi import WebApiError

    settings = load_settings()
    if args.local:
        settings.local_folder = str(Path(args.local).expanduser())
    if args.workspace:
        settings.workspace_id = args.workspace
    if args.folder is not None:
        settings.remote_folder_id = args.folder.strip().replace("\\", "/").strip("/")

    errors = settings.validation_errors()
    if errors:
        print("Einrichtung unvollständig:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        print("Bitte `tarklone init` oder die Tray-App verwenden.", file=sys.stderr)
        return 2

    oauth = OAuthClient(settings)
    remote = None
    state = None
    try:
        token = oauth.ensure_valid()
        remote = TarkleRemote(settings, token)
        state = SyncState(state_path())
        engine = SyncEngine(
            Path(settings.local_folder),
            remote,
            state,
            sync_deletions=settings.sync_deletions,
            conflict_policy=settings.conflict_policy,
            exclude=settings.exclude,
            progress=(print if args.verbose or args.dry_run else None),
        )
        result = engine.sync(dry_run=args.dry_run, full=args.full)
    except (AuthError, SyncError, WebApiError) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2
    finally:
        if remote is not None:
            remote.close()
        if state is not None:
            state.close()
        oauth.close()

    mode = " (dry-run)" if result.dry_run else ""
    print(f"Sync{mode} abgeschlossen in {result.duration:.1f}s")
    print(f"  hochgeladen:   {result.uploaded}")
    print(f"  heruntergeladen: {result.downloaded}")
    print(f"  lokal gelöscht: {result.deleted_local}")
    print(f"  remote gelöscht: {result.deleted_remote}")
    print(f"  Konflikte:     {result.conflicts}")
    print(f"  übersprungen:  {result.skipped}")
    if result.errors:
        print(f"  Fehler:        {len(result.errors)}", file=sys.stderr)
        for error in result.errors:
            print(f"    - {error}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tarklone",
        description="Bidirektionaler Ordner-Sync für Tarkle.",
    )
    parser.add_argument("--version", action="version", version=f"tarklone {__version__}")
    parser.add_argument("--debug", action="store_true", help="Ausführliche Fehlerausgaben")
    sub = parser.add_subparsers(dest="command")

    login = sub.add_parser("login", help="Im Browser bei Tarkle anmelden")
    login.add_argument("--timeout", type=int, default=300, help="Timeout in Sekunden")
    login.set_defaults(func=cmd_login)

    logout = sub.add_parser("logout", help="Tarkle-Anmeldung entfernen")
    logout.set_defaults(func=cmd_logout)

    probe = sub.add_parser("probe", help="MCP-Schnittstelle und verfügbare Tools anzeigen")
    probe.add_argument("--json", action="store_true", help="JSON ausgeben")
    probe.add_argument("--verbose", action="store_true", help="Tool-Schemas ausgeben")
    probe.add_argument("--save", metavar="DATEI", help="Report als JSON speichern")
    probe.set_defaults(func=cmd_probe)

    status = sub.add_parser("status", help="Konfiguration und Login-Status anzeigen")
    status.set_defaults(func=cmd_status)

    init = sub.add_parser("init", help="Lokalen Ordner und Workspace einrichten")
    init.set_defaults(func=cmd_init)

    sync = sub.add_parser("sync", help="Einen Sync-Lauf starten")
    sync.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts ändern")
    sync.add_argument("--full", action="store_true", help="Zustand ignorieren und vollständig vergleichen")
    sync.add_argument("--local", metavar="ORDNER", help="Lokalen Ordner überschreiben")
    sync.add_argument("--workspace", metavar="ID", help="Tarkle-Workspace überschreiben")
    sync.add_argument("--folder", metavar="PFAD", help="Remote-Ordnerpfad überschreiben")
    sync.add_argument("--verbose", action="store_true", help="Aktionen anzeigen")
    sync.set_defaults(func=cmd_sync)

    tray = sub.add_parser("tray", help="Tray-App starten")
    tray.set_defaults(func=lambda _args: _run_tray())

    return parser


def _run_tray() -> int:
    from .app import main as tray_main

    return tray_main()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        return _run_tray()
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nAbgebrochen.", file=sys.stderr)
        return 130
    except AuthError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - CLI-Schutznetz
        if getattr(args, "debug", False):
            raise
        print(f"Unerwarteter Fehler: {exc}", file=sys.stderr)
        return 1
