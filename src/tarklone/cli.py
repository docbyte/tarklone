from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import __version__
from .auth import AuthError, NotLoggedIn, OAuthClient, TokenStore
from .config import (
    SyncProfile,
    config_path,
    crypto_keyring_username,
    data_dir,
    ensure_dirs,
    load_settings,
    log_path,
    profile_state_path,
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
    _print_token_status(token)
    print(f"Status:         {'bereit' if settings.is_configured and token else 'Einrichtung nötig'}")
    if settings.profiles:
        print("\nWorkspace-Profile:")
        for profile in settings.profiles:
            state = "aktiv" if profile.enabled else "deaktiviert"
            used = "Ja" if profile.sync_deletions else "Nein"
            print(f"- {profile.display_name} ({profile.workspace_id}) [{state}]")
            print(f"  Ordner:      {profile.local_folder or '—'}")
            print(
                f"  Remote:      {profile.remote_folder_id or 'Workspace-Root'}"
            )
            print(
                f"  Intervall:   {profile.sync_interval_seconds}s, Löschungen: {used}"
            )
    else:
        print("\nWorkspace-Profile: keine")

    errors = settings.validation_errors()
    if errors:
        print("\nHinweise:")
        for error in errors:
            print(f"- {error}")
    print(f"\nKonfigdatei:    {config_path()}")
    print(f"Logdatei:       {log_path()}")
    print(f"Zustand:        {data_dir() / 'profiles'}")
    return 0


def cmd_init(_args: argparse.Namespace) -> int:
    settings = load_settings()
    first = settings.profiles[0] if settings.profiles else None
    print("Einrichtung von tarklone")
    print("========================")
    default_local = first.local_folder if first else settings.local_folder
    local = input(f"Lokaler Ordner [{default_local or Path.cwd()}]: ").strip()
    if local:
        local_folder = str(Path(local).expanduser())
    else:
        local_folder = default_local or str(Path.cwd())
    workspace = input(
        f"Tarkle-Workspace-ID [{first.workspace_id if first else settings.workspace_id}]: "
    ).strip()
    if not workspace:
        workspace = first.workspace_id if first else settings.workspace_id
    folder = input(
        f"Remote-Ordner (Pfad, leer = Workspace-Root) "
        f"[{first.remote_folder_id if first else settings.remote_folder_id}]: "
    ).strip()
    if not folder:
        folder = first.remote_folder_id if first else settings.remote_folder_id
    interval_raw = input(
        f"Sync-Intervall in Sekunden [{first.sync_interval_seconds if first else 60}]: "
    ).strip()
    try:
        interval = max(15, int(interval_raw or (first.sync_interval_seconds if first else 60)))
    except ValueError:
        interval = 60
    if not workspace:
        print("Fehler: Eine Tarkle-Workspace-ID ist erforderlich.", file=sys.stderr)
        return 2
    profile = next(
        (item for item in settings.profiles if item.workspace_id == workspace),
        None,
    )
    if profile is None:
        profile = SyncProfile(id=workspace, workspace_id=workspace)
        settings.profiles.append(profile)
    profile.local_folder = local_folder
    profile.remote_folder_id = folder.strip().replace("\\", "/").strip("/")
    profile.sync_interval_seconds = interval
    settings.sync_legacy_fields()
    save_settings(settings)
    print("Gespeichert. Anschließend `tarklone login` und `tarklone sync` ausführen.")
    return 0


def _validate_profile(profile: SyncProfile) -> list[str]:
    errors: list[str] = []
    if not profile.local_folder:
        errors.append("Kein lokaler Ordner ausgewählt.")
    elif not Path(profile.local_folder).is_dir():
        errors.append(f"Lokaler Ordner existiert nicht: {profile.local_folder}")
    if not profile.workspace_id:
        errors.append("Keine Tarkle-Workspace-ID gesetzt.")
    if profile.sync_interval_seconds < 15:
        errors.append("Das Sync-Intervall muss mindestens 15 Sekunden betragen.")
    return errors


def cmd_sync(args: argparse.Namespace) -> int:
    import getpass

    from .crypto import CryptoError, EncryptionManager
    from .encrypted_remote import EncryptionAdapter
    from .state import SyncState
    from .sync import SyncEngine, SyncError
    from .tarkle import TarkleRemote
    from .webapi import WebApiError

    settings = load_settings()
    local_override = str(Path(args.local).expanduser()) if args.local else ""
    folder_override = (
        args.folder.strip().replace("\\", "/").strip("/")
        if args.folder is not None
        else None
    )

    profiles = list(settings.enabled_profiles)
    if args.workspace:
        selected = next(
            (
                profile
                for profile in settings.profiles
                if profile.workspace_id == args.workspace or profile.id == args.workspace
            ),
            None,
        )
        if selected is None:
            selected = SyncProfile(
                id=args.workspace,
                workspace_id=args.workspace,
                local_folder=local_override,
                sync_interval_seconds=settings.sync_interval_seconds,
                sync_deletions=settings.sync_deletions,
            )
        profiles = [selected]
    elif local_override and len(profiles) > 1:
        print(
            "Fehler: --local kann bei mehreren Profilen nur zusammen mit "
            "--workspace verwendet werden.",
            file=sys.stderr,
        )
        return 2
    if not profiles:
        print("Kein Workspace-Profil eingerichtet.", file=sys.stderr)
        print("Bitte `tarklone init` oder die Tray-App verwenden.", file=sys.stderr)
        return 2

    prepared = []
    for profile in profiles:
        current = replace(
            profile,
            local_folder=local_override or profile.local_folder,
            remote_folder_id=(
                folder_override
                if folder_override is not None
                else profile.remote_folder_id
            ),
        )
        errors = _validate_profile(current)
        if errors:
            print(
                f"Profil „{current.display_name}“ ist unvollständig:",
                file=sys.stderr,
            )
            for error in errors:
                print(f"- {error}", file=sys.stderr)
            return 2
        prepared.append(current)

    oauth = OAuthClient(settings)
    token = None
    overall_errors = 0
    try:
        token = oauth.ensure_valid()
        for profile in prepared:
            print(f"Sync „{profile.display_name}“ …")
            profile_settings = replace(
                settings,
                local_folder=profile.local_folder,
                workspace_id=profile.workspace_id,
                workspace_name=profile.workspace_name,
                remote_folder_id=profile.remote_folder_id,
                sync_deletions=profile.sync_deletions,
                sync_interval_seconds=profile.sync_interval_seconds,
            )
            remote = None
            state = None
            try:
                encryption = EncryptionManager(
                    Path(profile.local_folder),
                    keyring_username=crypto_keyring_username(profile.id, profile.local_folder),
                )
                encryption.load_cached_key()
                if encryption.enabled and not encryption.unlocked:
                    password = getpass.getpass(
                        f"Passwort für encrypted ({profile.display_name}): "
                    )
                    encryption.unlock(password)
                if not encryption.enabled:
                    encrypted_dir = Path(profile.local_folder) / "encrypted"
                    if encrypted_dir.is_dir() and any(
                        path.is_file() and path.name != ".tarklone-key.json"
                        for path in encrypted_dir.rglob("*")
                    ):
                        print(
                            "Fehler: Der Ordner encrypted enthält Dateien, aber es "
                            "ist kein Passwort gesetzt. Bitte zuerst in der Tray-App "
                            "ein Passwort festlegen.",
                            file=sys.stderr,
                        )
                        return 2
                remote = TarkleRemote(profile_settings, token)
                adapter = EncryptionAdapter(remote, encryption)
                state_file = profile_state_path(profile.id)
                existing_states = (
                    list(state_file.parent.glob("*.sqlite3"))
                    if state_file.parent.exists()
                    else []
                )
                if (
                    not state_file.exists()
                    and not existing_states
                    and settings.profiles
                    and profile.id == settings.profiles[0].id
                    and state_path().exists()
                ):
                    state_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(state_path(), state_file)
                state = SyncState(state_file)
                engine = SyncEngine(
                    Path(profile.local_folder),
                    adapter,
                    state,
                    sync_deletions=profile.sync_deletions,
                    conflict_policy=settings.conflict_policy,
                    exclude=settings.exclude,
                    progress=(print if args.verbose or args.dry_run else None),
                )
                result = engine.sync(dry_run=args.dry_run, full=args.full)
                mode = " (dry-run)" if result.dry_run else ""
                print(f"  Sync{mode} abgeschlossen in {result.duration:.1f}s")
                print(f"  hochgeladen:     {result.uploaded}")
                print(f"  heruntergeladen: {result.downloaded}")
                print(f"  lokal gelöscht:  {result.deleted_local}")
                print(f"  remote gelöscht: {result.deleted_remote}")
                print(f"  Konflikte:       {result.conflicts}")
                print(f"  übersprungen:    {result.skipped}")
                if result.errors:
                    overall_errors += len(result.errors)
                    print(
                        f"  Fehler:          {len(result.errors)}",
                        file=sys.stderr,
                    )
                    for error in result.errors:
                        print(f"    - {error}", file=sys.stderr)
            finally:
                if remote is not None:
                    remote.close()
                if state is not None:
                    state.close()
    except CryptoError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2
    except (AuthError, SyncError, WebApiError) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2
    finally:
        oauth.close()

    return 1 if overall_errors else 0


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
    try:
        from .app import main as tray_main
    except ImportError:
        print(
            "Der Tray-Modus ist in dieser reinen CLI-Version nicht enthalten. "
            "Bitte die GUI-Variante für deine Plattform verwenden.",
            file=sys.stderr,
        )
        return 2
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
