from __future__ import annotations

import argparse
import getpass
import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import __version__
from .api import TarkleApi, TarkleApiError
from .auth import (
    ApiKeyMissing,
    ApiKeyStore,
    ApiKeyStoreError,
    clear_legacy_oauth_token,
    load_api_key,
    mask_api_key,
)
from .config import (
    SyncProfile,
    config_path,
    crypto_keyring_username,
    data_dir,
    ensure_dirs,
    is_api_key,
    is_workspace_key,
    load_settings,
    log_path,
    profile_state_path,
    save_settings,
    state_path,
)
from .tarkle import probe_api


def _print_key_status(api_key: str | None) -> None:
    if not api_key:
        print("Tarkle-API-Key: nicht gespeichert")
    else:
        print(f"Tarkle-API-Key: gespeichert ({mask_api_key(api_key)})")


def _fetch_workspaces(api_key: str) -> list[dict[str, Any]]:
    api = TarkleApi(api_key)
    try:
        return api.list_workspaces()
    finally:
        api.close()


def cmd_login(args: argparse.Namespace) -> int:
    key = (getattr(args, "key", "") or "").strip()
    if not key:
        key = getpass.getpass(
            "Tarkle-API-Key (Tarkle -> Einstellungen -> API): "
        ).strip()
    if not key:
        print("Fehler: Es wurde kein API-Key eingegeben.", file=sys.stderr)
        return 2
    if not is_api_key(key):
        print(
            "Fehler: Ungültiger API-Key. Der Key beginnt mit tk_ und wird in "
            "Tarkle unter Einstellungen -> API erstellt.",
            file=sys.stderr,
        )
        return 2
    try:
        workspaces = _fetch_workspaces(key)
    except TarkleApiError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2
    try:
        ApiKeyStore().save(key)
    except ApiKeyStoreError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2
    clear_legacy_oauth_token()
    print(f"API-Key gespeichert ({mask_api_key(key)}).")
    if workspaces:
        print("Verfügbare Workspaces:")
        for workspace in workspaces:
            print(
                f"- {workspace.get('name')} "
                f"({workspace.get('workspace_key')}, "
                f"{workspace.get('role') or 'unbekannt'})"
            )
    else:
        print("Der Key erreicht derzeit keinen Workspace.")
    return 0


def cmd_logout(_args: argparse.Namespace) -> int:
    deleted = ApiKeyStore().delete()
    clear_legacy_oauth_token()
    if not deleted:
        print(
            "Fehler: Der API-Key konnte nicht zuverlässig aus dem Keyring "
            "entfernt werden. Bitte Credential Manager/Keyring prüfen.",
            file=sys.stderr,
        )
        return 1
    print(
        "Tarkle-API-Key lokal entfernt. Der Key bleibt in Tarkle gültig; "
        "zum Widerrufen dort unter Einstellungen -> API löschen."
    )
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    settings = load_settings()
    try:
        key = load_api_key()
        report = probe_api(settings, key)
    except ApiKeyMissing as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        print("Bitte zuerst `tarklone login` ausführen.", file=sys.stderr)
        return 2
    except TarkleApiError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"API-Key:    {mask_api_key(key)}")
        print(f"Workspaces: {len(report.get('workspaces', []))}")
        for workspace in report.get("workspaces", []):
            print(
                f"- {workspace.get('name')} "
                f"({workspace.get('workspace_key')}, "
                f"{workspace.get('role') or 'unbekannt'})"
            )
        usage = report.get("usage") or {}
        if usage:
            print("\nSpeicherplatz:")
            for name, value in usage.items():
                if "error" in value:
                    print(f"- {name}: Fehler: {value['error']}")
                else:
                    print(
                        f"- {name}: {value.get('used', 0)} von "
                        f"{value.get('limit', 0)} Bytes "
                        f"({value.get('percent', 0)} %)"
                    )

    if args.save:
        target = Path(args.save)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        if not args.json:
            print(f"\nReport gespeichert: {target}")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    ensure_dirs()
    settings = load_settings()
    api_key = ApiKeyStore().load()

    print(f"tarklone {__version__}")
    _print_key_status(api_key)
    errors = settings.validation_errors()
    ready = settings.is_configured and api_key is not None and not errors
    print(f"Status:         {'bereit' if ready else 'Einrichtung nötig'}")
    if settings.profiles:
        print("\nWorkspace-Profile:")
        for profile in settings.profiles:
            state = "aktiv" if profile.enabled else "deaktiviert"
            used = "Ja" if profile.sync_deletions else "Nein"
            mode = "Multi-Ordner" if profile.sync_mode == "multi" else "Einzelordner"
            print(
                f"- {profile.display_name} ({profile.workspace_id}) "
                f"[{state}, {mode}]"
            )
            if profile.sync_mode == "multi":
                for folder in profile.active_folders():
                    print(f"  Ordner:      {folder.local_folder}")
                    print(
                        f"  Remote:      "
                        f"{folder.remote_folder_id or 'Workspace-Root'}"
                    )
            else:
                print(f"  Ordner:      {profile.local_folder or '—'}")
                print(
                    f"  Remote:      {profile.remote_folder_id or 'Workspace-Root'}"
                )
            print(
                f"  Intervall:   {profile.sync_interval_seconds}s, "
                f"Löschungen: {used}"
            )
    else:
        print("\nWorkspace-Profile: keine")

    if errors:
        print("\nHinweise:")
        for error in errors:
            print(f"- {error}")
    print(f"\nKonfigdatei:    {config_path()}")
    print(f"Logdatei:       {log_path()}")
    print(f"Zustand:        {data_dir() / 'profiles'}")
    return 0


def _resolve_workspace(
    value: str,
    workspaces: list[dict[str, Any]],
) -> str:
    text = value.strip()
    if is_workspace_key(text):
        return text
    for workspace in workspaces:
        name = str(workspace.get("name") or "")
        key = str(workspace.get("workspace_key") or "")
        if text and (name.casefold() == text.casefold() or key == text):
            return key
    return text


def cmd_init(_args: argparse.Namespace) -> int:
    settings = load_settings()
    key = ApiKeyStore().load()
    workspaces: list[dict[str, Any]] = []
    if key:
        try:
            workspaces = _fetch_workspaces(key)
        except TarkleApiError as exc:
            print(f"Warnung: Workspaces konnten nicht geladen werden: {exc}", file=sys.stderr)
    if workspaces:
        print("Verfügbare Tarkle-Workspaces:")
        for workspace in workspaces:
            print(
                f"- {workspace.get('name')} "
                f"({workspace.get('workspace_key')})"
            )
        print()

    first = next(
        (profile for profile in settings.profiles if profile.enabled),
        settings.profiles[0] if settings.profiles else None,
    )
    print("Einrichtung von tarklone")
    print("========================")
    default_local = first.local_folder if first else settings.local_folder
    local = input(f"Lokaler Ordner [{default_local or Path.cwd()}]: ").strip()
    if local:
        local_folder = str(Path(local).expanduser())
    else:
        local_folder = default_local or str(Path.cwd())

    default_workspace = first.workspace_id if first else settings.workspace_id
    workspace_input = input(
        f"Tarkle-Workspace (Key oder Name) [{default_workspace}]: "
    ).strip()
    if not workspace_input:
        workspace_input = default_workspace
    workspace = _resolve_workspace(workspace_input, workspaces)
    if not is_workspace_key(workspace):
        print(
            "Fehler: Ein gültiger Workspace-Key hat 8 Zeichen (a–z/0–9). "
            "Den Key zeigt `tarklone login` bzw. Tarkle unter Einstellungen -> API.",
            file=sys.stderr,
        )
        return 2

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
        interval = max(
            15,
            int(interval_raw or (first.sync_interval_seconds if first else 60)),
        )
    except ValueError:
        interval = 60

    profile = next(
        (item for item in settings.profiles if item.workspace_id == workspace),
        None,
    )
    if profile is None:
        profile = SyncProfile(id=workspace, workspace_id=workspace)
        settings.profiles.append(profile)
    profile.local_folder = local_folder
    profile.remote_folder_id = folder.replace("\\", "/").strip("/")
    profile.sync_mode = "single"
    profile.folders = []
    profile.sync_interval_seconds = interval
    profile.enabled = True
    settings.sync_legacy_fields()
    save_settings(settings)
    print("Gespeichert. Anschließend `tarklone sync` ausführen.")
    return 0


def _validate_profile(profile: SyncProfile) -> list[str]:
    errors: list[str] = []
    folders = profile.active_folders()
    if not folders:
        errors.append("Kein lokaler Ordner ausgewählt.")
    for folder in folders:
        if not Path(folder.local_folder).is_dir():
            errors.append(f"Lokaler Ordner existiert nicht: {folder.local_folder}")
    if not profile.workspace_id:
        errors.append("Keine Tarkle-Workspace gesetzt.")
    elif not is_workspace_key(profile.workspace_id):
        errors.append(
            "Kein gültiger Tarkle-Workspace-Key (8 Zeichen, a–z/0–9)."
        )
    if profile.sync_interval_seconds < 15:
        errors.append("Das Sync-Intervall muss mindestens 15 Sekunden betragen.")
    return errors


def _migrate_workspace_keys(settings, api_key: str) -> None:
    """Ersetzt alte Workspace-IDs anhand des Namens durch API-Workspace-Keys."""
    if not any(
        profile.workspace_name and not is_workspace_key(profile.workspace_id)
        for profile in settings.profiles
    ):
        return
    try:
        workspaces = _fetch_workspaces(api_key)
    except TarkleApiError:
        return
    by_name = {
        str(item.get("name") or "").strip().casefold(): str(
            item.get("workspace_key") or ""
        )
        for item in workspaces
        if item.get("name") and item.get("workspace_key")
    }
    changed = False
    for profile in settings.profiles:
        if is_workspace_key(profile.workspace_id):
            continue
        match = by_name.get(profile.workspace_name.strip().casefold())
        if match:
            profile.workspace_id = match
            changed = True
    if changed:
        save_settings(settings)


def cmd_sync(args: argparse.Namespace) -> int:
    from .crypto import CryptoError, EncryptionManager
    from .encrypted_remote import EncryptedStateAdapter, EncryptionAdapter
    from .state import SyncState
    from .sync import SyncEngine, SyncError
    from .tarkle import TarkleRemote

    settings = load_settings()
    try:
        api_key = load_api_key()
    except ApiKeyMissing as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        print("Bitte zuerst `tarklone login` ausführen.", file=sys.stderr)
        return 2

    _migrate_workspace_keys(settings, api_key)

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
        folders = profile.active_folders()
        primary = folders[0] if folders else None
        if local_override or folder_override is not None:
            current = replace(
                profile,
                sync_mode="single",
                local_folder=(
                    local_override
                    or (primary.local_folder if primary else profile.local_folder)
                ),
                remote_folder_id=(
                    folder_override
                    if folder_override is not None
                    else (
                        primary.remote_folder_id
                        if primary
                        else profile.remote_folder_id
                    )
                ),
                folders=[],
            )
        else:
            current = profile
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

    overall_errors = 0
    try:
        for profile in prepared:
            folders = profile.active_folders()
            print(f"Sync „{profile.display_name}“ …")
            for index, folder in enumerate(folders, start=1):
                folder_prefix = (
                    f" ({index}/{len(folders)})" if len(folders) > 1 else ""
                )
                print(
                    f"  Ordner „{folder.display_name}“{folder_prefix}: "
                    f"{folder.local_folder} -> "
                    f"{folder.remote_folder_id or 'Workspace-Root'}"
                )
                profile_settings = replace(
                    settings,
                    local_folder=folder.local_folder,
                    workspace_id=profile.workspace_id,
                    workspace_name=profile.workspace_name,
                    remote_folder_id=folder.remote_folder_id,
                    sync_deletions=profile.sync_deletions,
                    sync_interval_seconds=profile.sync_interval_seconds,
                )
                remote = None
                state = None
                try:
                    encryption = EncryptionManager(
                        Path(folder.local_folder),
                        keyring_username=crypto_keyring_username(
                            profile.id, folder.local_folder
                        ),
                    )
                    encryption.load_cached_key()
                    if encryption.enabled and not encryption.unlocked:
                        password = getpass.getpass(
                            "Passwort für encrypted "
                            f"({profile.display_name} / {folder.display_name}): "
                        )
                        encryption.unlock(password)
                    if not encryption.enabled:
                        encrypted_dir = Path(folder.local_folder) / "encrypted"
                        if encrypted_dir.is_dir() and any(
                            path.is_file() and path.name != ".tarklone-key.json"
                            for path in encrypted_dir.rglob("*")
                        ):
                            print(
                                "Fehler: Der Ordner encrypted enthält Dateien, aber "
                                "es ist kein Passwort gesetzt. Bitte zuerst in der "
                                "Tray-App ein Passwort festlegen.",
                                file=sys.stderr,
                            )
                            return 2
                    remote = TarkleRemote(profile_settings, api_key)
                    adapter = EncryptionAdapter(remote, encryption)
                    state_id = (
                        profile.id
                        if profile.sync_mode != "multi"
                        or folder.id == profile.id
                        else f"{profile.id}-{folder.id}"
                    )
                    state_file = profile_state_path(state_id)
                    existing_states = (
                        list(state_file.parent.glob("*.sqlite3"))
                        if state_file.parent.exists()
                        else []
                    )
                    if (
                        not state_file.exists()
                        and not existing_states
                        and state_id == profile.id
                        and settings.profiles
                        and profile.id == settings.profiles[0].id
                        and state_path().exists()
                    ):
                        state_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(state_path(), state_file)
                    state = SyncState(state_file)
                    sync_state = (
                        EncryptedStateAdapter(state, encryption)
                        if encryption.enabled
                        else state
                    )
                    engine = SyncEngine(
                        Path(folder.local_folder),
                        adapter,
                        sync_state,
                        sync_deletions=profile.sync_deletions,
                        conflict_policy=settings.conflict_policy,
                        exclude=settings.exclude,
                        progress=(
                            print if args.verbose or args.dry_run else None
                        ),
                    )
                    result = engine.sync(
                        dry_run=args.dry_run, full=args.full
                    )
                    mode = " (dry-run)" if result.dry_run else ""
                    print(f"    Sync{mode} abgeschlossen in {result.duration:.1f}s")
                    print(f"    hochgeladen:     {result.uploaded}")
                    print(f"    heruntergeladen: {result.downloaded}")
                    print(f"    lokal gelöscht:  {result.deleted_local}")
                    print(f"    remote gelöscht: {result.deleted_remote}")
                    print(f"    Konflikte:       {result.conflicts}")
                    print(f"    übersprungen:    {result.skipped}")
                    if result.errors:
                        overall_errors += len(result.errors)
                        print(
                            f"    Fehler:          {len(result.errors)}",
                            file=sys.stderr,
                        )
                        for error in result.errors:
                            print(f"      - {error}", file=sys.stderr)
                finally:
                    if remote is not None:
                        remote.close()
                    if state is not None:
                        state.close()
    except (ApiKeyStoreError, TarkleApiError, CryptoError, SyncError) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2

    return 1 if overall_errors else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tarklone",
        description="Bidirektionaler Ordner-Sync für Tarkle.",
    )
    parser.add_argument("--version", action="version", version=f"tarklone {__version__}")
    parser.add_argument("--debug", action="store_true", help="Ausführliche Fehlerausgaben")
    sub = parser.add_subparsers(dest="command")

    login = sub.add_parser("login", help="Tarkle-API-Key speichern")
    login.add_argument(
        "--key",
        metavar="TK_…",
        default="",
        help="API-Key direkt übergeben (sonst wird er verdeckt abgefragt)",
    )
    login.set_defaults(func=cmd_login)

    logout = sub.add_parser("logout", help="Tarkle-API-Key entfernen")
    logout.set_defaults(func=cmd_logout)

    probe = sub.add_parser("probe", help="API-Key, Workspaces und Speicher prüfen")
    probe.add_argument("--json", action="store_true", help="JSON ausgeben")
    probe.add_argument("--save", metavar="DATEI", help="Report als JSON speichern")
    probe.set_defaults(func=cmd_probe)

    status = sub.add_parser("status", help="Konfiguration und API-Key-Status anzeigen")
    status.set_defaults(func=cmd_status)

    init = sub.add_parser("init", help="Lokalen Ordner und Workspace einrichten")
    init.set_defaults(func=cmd_init)

    sync = sub.add_parser("sync", help="Einen Sync-Lauf starten")
    sync.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts ändern")
    sync.add_argument("--full", action="store_true", help="Zustand ignorieren und vollständig vergleichen")
    sync.add_argument("--local", metavar="ORDNER", help="Lokalen Ordner überschreiben")
    sync.add_argument("--workspace", metavar="KEY", help="Tarkle-Workspace-Key überschreiben")
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
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        return _run_tray()
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nAbgebrochen.", file=sys.stderr)
        return 130
    except (ApiKeyMissing, ApiKeyStoreError) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2
    except TarkleApiError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - CLI-Schutznetz
        if getattr(args, "debug", False):
            raise
        print(f"Unerwarteter Fehler: {exc}", file=sys.stderr)
        return 1
