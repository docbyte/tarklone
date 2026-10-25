from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

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

DEFAULT_SERVER_URL = "https://tarkle.com"
# Die MCP-Beta der Tarkle-Web-App läuft unter dieser Hostname; das OAuth-Token
# wird mit dieser Audience ausgestellt. mcp.tarkle.com lehnt es ab.
DEFAULT_MCP_URL = "https://mcp.tarklesend.com/mcp"
LEGACY_MCP_URL = "https://mcp.tarkle.com/mcp"


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
        directory.mkdir(parents=True, exist_ok=True)


@dataclass
class SyncProfile:
    id: str
    workspace_id: str
    workspace_name: str = ""
    local_folder: str = ""
    remote_folder_id: str = ""
    sync_interval_seconds: int = 60
    sync_deletions: bool = False
    enabled: bool = True

    @property
    def display_name(self) -> str:
        return self.workspace_name or self.workspace_id or self.id


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

    oauth_client_id: str = ""
    oauth_redirect_uri: str = ""
    oauth_redirect_port: int = 53682
    account_email: str = ""

    server_url: str = DEFAULT_SERVER_URL
    mcp_url: str = DEFAULT_MCP_URL

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
        first = self.profiles[0]
        self.local_folder = first.local_folder
        self.workspace_id = first.workspace_id
        self.workspace_name = first.workspace_name
        self.remote_folder_id = first.remote_folder_id
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
            profile.local_folder and profile.workspace_id
            for profile in self.profiles
        )

    @property
    def enabled_profiles(self) -> list[SyncProfile]:
        return [
            profile
            for profile in self.profiles
            if profile.enabled and profile.local_folder and profile.workspace_id
        ]

    def validation_errors(self) -> list[str]:
        errors: list[str] = []
        self.ensure_profiles()
        if not self.profiles:
            errors.append("Kein Workspace-Profil eingerichtet.")
        for profile in self.profiles:
            label = profile.display_name
            if not profile.local_folder:
                errors.append(f"[{label}] Kein lokaler Ordner ausgewählt.")
            elif not Path(profile.local_folder).is_dir():
                errors.append(
                    f"[{label}] Lokaler Ordner existiert nicht: {profile.local_folder}"
                )
            if not profile.workspace_id:
                errors.append(f"[{label}] Kein Tarkle-Workspace ausgewählt.")
            if profile.sync_interval_seconds < 15:
                errors.append(
                    f"[{label}] Das Sync-Intervall muss mindestens 15 Sekunden betragen."
                )
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
                )
            except (TypeError, ValueError):
                continue
            if any(
                existing.workspace_id == profile.workspace_id
                for existing in settings.profiles
            ):
                continue
            settings.profiles.append(profile)
    if settings.mcp_url in ("", LEGACY_MCP_URL):
        settings.mcp_url = DEFAULT_MCP_URL
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
