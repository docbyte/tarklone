from __future__ import annotations

import json
import os
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


def log_path() -> Path:
    return log_dir() / "tarklone.log"


def ensure_dirs() -> None:
    for directory in (config_dir(), data_dir(), log_dir()):
        directory.mkdir(parents=True, exist_ok=True)


@dataclass
class Settings:
    local_folder: str = ""
    workspace_id: str = ""
    workspace_name: str = ""
    remote_folder_id: str = ""
    remote_folder_name: str = ""

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

    @property
    def is_configured(self) -> bool:
        return bool(self.local_folder and self.workspace_id)

    def validation_errors(self) -> list[str]:
        errors: list[str] = []
        if not self.local_folder:
            errors.append("Kein lokaler Ordner ausgewählt.")
        elif not Path(self.local_folder).is_dir():
            errors.append(f"Lokaler Ordner existiert nicht: {self.local_folder}")
        if not self.workspace_id:
            errors.append("Kein Tarkle-Workspace ausgewählt.")
        if self.sync_interval_seconds < 15:
            errors.append("Das Sync-Intervall muss mindestens 15 Sekunden betragen.")
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
    settings = Settings(**data)
    if not isinstance(settings.exclude, list):
        settings.exclude = list(DEFAULT_EXCLUDES)
    if settings.mcp_url in ("", LEGACY_MCP_URL):
        settings.mcp_url = DEFAULT_MCP_URL
    return settings


def save_settings(settings: Settings, path: Path | None = None) -> None:
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(settings), indent=2, ensure_ascii=False) + "\n"
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(payload, encoding="utf-8")
    os.replace(temp, target)
