from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from platformdirs import user_config_dir, user_data_dir, user_log_dir

APP_NAME = "tarklone"
APP_AUTHOR = "docbyte"

DEFAULT_EXCLUDES = [
    ".tarklone/**",
    "~$*",
    "*.tmp",
    "*.part",
    "*.tarklone-part",
    "*.tarklone-cipher",
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
]

DEFAULT_API_BASE = "https://tarkle.tech/api/v1"
API_KEY_PREFIX = "tk_"
_WORKSPACE_KEY_RE = re.compile(r"^[a-z0-9]{8}$")


def config_dir() -> Path:
    return Path(user_config_dir(APP_NAME, APP_AUTHOR))


def data_dir() -> Path:
    return Path(user_data_dir(APP_NAME, APP_AUTHOR))


def log_dir() -> Path:
    return Path(user_log_dir(APP_NAME, APP_AUTHOR))


def config_path() -> Path:
    override = os.environ.get("TARKLONE_CONFIG")
    return Path(override) if override else config_dir() / "config.json"


def state_path() -> Path:
    override = os.environ.get("TARKLONE_STATE")
    return Path(override) if override else data_dir() / "state.sqlite3"


def profile_state_path(profile_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", profile_id)[:80] or "default"
    return data_dir() / "profiles" / f"{safe}.sqlite3"


def crypto_keyring_username(profile_id: str, local_folder: str = "") -> str:
    base = f"crypto-master-{profile_id}"
    if not local_folder:
        return base
    normalized = str(Path(local_folder).resolve())
    if os.name == "nt":
        normalized = normalized.casefold()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"{base}-{digest}"


def log_path() -> Path:
    return log_dir() / "tarklone.log"


def ensure_dirs() -> None:
    for directory in (config_dir(), data_dir(), log_dir()):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            try:
                directory.chmod(0o700)
            except OSError:
                pass


def _validate_https_endpoint(
    value: str,
    *,
    expected_host: str,
    expected_path: str,
) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("Ungültiger tarklone-Endpunkt.") from exc
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != expected_host
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != expected_path.rstrip("/")
    ):
        raise ValueError(
            f"Nicht erlaubter Endpunkt. Erwartet wird https://{expected_host}"
            f"{expected_path}."
        )
    return f"https://{expected_host}{expected_path}"


def validate_api_base(value: str) -> str:
    """Validiert die offizielle Tarkle-API-Basis."""
    return _validate_https_endpoint(
        value,
        expected_host="tarkle.tech",
        expected_path="/api/v1",
    )


def normalize_api_key(value: str) -> str:
    key = (value or "").strip().strip('"').strip("'")
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    return key


def is_api_key(value: str) -> bool:
    key = normalize_api_key(value)
    return key.startswith(API_KEY_PREFIX) and len(key) >= len(API_KEY_PREFIX) + 8


def is_workspace_key(value: str) -> bool:
    return bool(_WORKSPACE_KEY_RE.fullmatch((value or "").strip()))


@dataclass
class SyncFolder:
    id: str
    local_folder: str
    remote_folder_id: str = ""

    @property
    def display_name(self) -> str:
        return self.remote_folder_id or Path(self.local_folder).name or self.local_folder


@dataclass
class SyncProfile:
    id: str
    # 8-stelliger Workspace-Key der offiziellen Tarkle-API (X-Workspace-Key).
    workspace_id: str
    workspace_name: str = ""
    local_folder: str = ""
    remote_folder_id: str = ""
    sync_interval_seconds: int = 60
    sync_deletions: bool = False
    enabled: bool = True
    sync_mode: str = "single"
    folders: list[SyncFolder] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        return self.workspace_name or self.workspace_id or self.id

    def active_folders(self) -> list[SyncFolder]:
        if self.sync_mode == "multi":
            return [
                folder
                for folder in self.folders
                if folder.local_folder.strip()
            ]
        if self.local_folder.strip():
            return [
                SyncFolder(
                    id=self.id,
                    local_folder=self.local_folder,
                    remote_folder_id=self.remote_folder_id,
                )
            ]
        return []

    def normalized_folders(self) -> list[SyncFolder]:
        """Returns all mappings that should be persisted for this profile."""
        if self.sync_mode == "multi":
            return [folder for folder in self.folders if folder.local_folder.strip()]
        return self.active_folders()


@dataclass
class Settings:
    # Legacy-Einzelprofil; wird beim Laden in profiles migriert.
    local_folder: str = ""
    workspace_id: str = ""
    workspace_name: str = ""
    remote_folder_id: str = ""
    remote_folder_name: str = ""

    profiles: list[SyncProfile] = field(default_factory=list)

    sync_interval_seconds: int = 60
    sync_deletions: bool = False
    conflict_policy: str = "copy"
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    start_minimized: bool = True
    launch_at_login: bool = False
    log_level: str = "INFO"

    update_check_interval_days: int = 7
    last_update_check: str = ""

    def ensure_profiles(self) -> None:
        if self.profiles:
            return
        if not (self.workspace_id or self.local_folder):
            return
        workspace_id = self.workspace_id
        self.profiles = [
            SyncProfile(
                id=workspace_id or "default",
                workspace_id=workspace_id,
                workspace_name=self.workspace_name,
                local_folder=self.local_folder,
                remote_folder_id=self.remote_folder_id,
                sync_interval_seconds=self.sync_interval_seconds,
                sync_deletions=self.sync_deletions,
            )
        ]

    def sync_legacy_fields(self) -> None:
        if not self.profiles:
            return
        first = next(
            (profile for profile in self.profiles if profile.enabled),
            self.profiles[0],
        )
        folders = first.active_folders()
        primary = folders[0] if folders else None
        self.local_folder = primary.local_folder if primary else first.local_folder
        self.workspace_id = first.workspace_id
        self.workspace_name = first.workspace_name
        self.remote_folder_id = (
            primary.remote_folder_id if primary else first.remote_folder_id
        )
        self.sync_interval_seconds = first.sync_interval_seconds
        self.sync_deletions = first.sync_deletions

    def profile(self, profile_id: str) -> SyncProfile | None:
        for profile in self.profiles:
            if profile.id == profile_id:
                return profile
        return None

    @property
    def is_configured(self) -> bool:
        return any(
            profile.enabled
            and profile.workspace_id
            and profile.active_folders()
            for profile in self.profiles
        )

    @property
    def enabled_profiles(self) -> list[SyncProfile]:
        return [
            profile
            for profile in self.profiles
            if profile.enabled and profile.workspace_id and profile.active_folders()
        ]

    def validation_errors(self) -> list[str]:
        errors: list[str] = []
        self.ensure_profiles()
        active = [profile for profile in self.profiles if profile.enabled]
        if not active:
            errors.append("Kein Workspace-Profil eingerichtet.")
        for profile in active:
            label = profile.display_name
            folders = profile.active_folders()
            if not folders:
                errors.append(f"[{label}] Kein lokaler Ordner ausgewählt.")
            if not profile.workspace_id:
                errors.append(f"[{label}] Kein Tarkle-Workspace ausgewählt.")
            elif not is_workspace_key(profile.workspace_id):
                errors.append(
                    f"[{label}] Kein gültiger Tarkle-Workspace-Key "
                    "(8 Zeichen, a–z/0–9)."
                )
            if profile.sync_interval_seconds < 15:
                errors.append(
                    f"[{label}] Das Sync-Intervall muss mindestens 15 Sekunden betragen."
                )
            seen_local: list[Path] = []
            seen_remote: list[str] = []
            for folder in folders:
                if not Path(folder.local_folder).is_dir():
                    errors.append(
                        f"[{label}] Lokaler Ordner existiert nicht: {folder.local_folder}"
                    )
                    continue
                resolved_local = Path(folder.local_folder).resolve()
                if any(
                    resolved_local == existing
                    or resolved_local.is_relative_to(existing)
                    or existing.is_relative_to(resolved_local)
                    for existing in seen_local
                ):
                    errors.append(
                        f"[{label}] Lokale Ordner dürfen sich nicht überschneiden: "
                        f"{folder.local_folder}"
                    )
                seen_local.append(resolved_local)
                remote = folder.remote_folder_id.strip("/")
                if any(
                    not remote
                    or not existing
                    or remote == existing
                    or remote.startswith(existing + "/")
                    or existing.startswith(remote + "/")
                    for existing in seen_remote
                ):
                    errors.append(
                        f"[{label}] Remote-Ordner dürfen sich nicht überschneiden: "
                        f"{remote or 'Workspace-Root'}"
                    )
                seen_remote.append(remote)
            if profile.sync_mode not in {"single", "multi"}:
                errors.append(f"[{label}] Unbekannter Sync-Modus.")
        if self.conflict_policy not in {"copy", "newest"}:
            errors.append("Unbekannte Konfliktstrategie.")
        if self.update_check_interval_days < 0:
            errors.append("Das Update-Intervall darf nicht negativ sein.")
        return errors


def load_settings(path: Path | None = None) -> Settings:
    target = path or config_path()
    if not target.exists():
        return Settings()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    if not isinstance(raw, dict):
        return Settings()

    known = {item.name for item in fields(Settings)}
    data: dict[str, Any] = {key: value for key, value in raw.items() if key in known}
    profile_data = data.pop("profiles", [])
    settings = Settings(**data)
    if not isinstance(settings.exclude, list):
        settings.exclude = list(DEFAULT_EXCLUDES)
    if isinstance(profile_data, list):
        for item in profile_data:
            if not isinstance(item, dict) or not item.get("workspace_id"):
                continue
            try:
                folders: list[SyncFolder] = []
                raw_folders = item.get("folders", [])
                if isinstance(raw_folders, list):
                    for raw_folder in raw_folders:
                        if not isinstance(raw_folder, dict):
                            continue
                        local_folder = str(raw_folder.get("local_folder", ""))
                        if not local_folder:
                            continue
                        folders.append(
                            SyncFolder(
                                id=str(
                                    raw_folder.get("id")
                                    or f"folder-{uuid4().hex}"
                                ),
                                local_folder=local_folder,
                                remote_folder_id=str(
                                    raw_folder.get("remote_folder_id", "")
                                )
                                .replace("\\", "/")
                                .strip("/"),
                            )
                        )
                profile = SyncProfile(
                    id=str(item.get("id") or item.get("workspace_id")),
                    workspace_id=str(item["workspace_id"]),
                    workspace_name=str(item.get("workspace_name", "")),
                    local_folder=str(item.get("local_folder", "")),
                    remote_folder_id=str(item.get("remote_folder_id", "")),
                    sync_interval_seconds=max(
                        15, int(item.get("sync_interval_seconds", 60))
                    ),
                    sync_deletions=bool(item.get("sync_deletions", False)),
                    enabled=bool(item.get("enabled", True)),
                    sync_mode=str(item.get("sync_mode", "single")),
                    folders=folders,
                )
            except (TypeError, ValueError):
                continue
            if profile.sync_mode not in {"single", "multi"}:
                profile.sync_mode = "single"
            if any(
                existing.workspace_id == profile.workspace_id
                for existing in settings.profiles
            ):
                continue
            settings.profiles.append(profile)
    settings.ensure_profiles()
    settings.sync_legacy_fields()
    return settings


def save_settings(settings: Settings, path: Path | None = None) -> None:
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    settings.ensure_profiles()
    settings.sync_legacy_fields()
    payload = json.dumps(asdict(settings), indent=2, ensure_ascii=False) + "\n"
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(payload, encoding="utf-8")
    os.replace(temp, target)
