from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from .api import TarkleApi, TarkleApiError, _parse_time
from .config import Settings, is_workspace_key
from .sync import RemoteEntry, SyncError


class TarkleRemote:
    """Tarkle-Anbindung über die offizielle API v1."""

    # Die API kann leere Ordner direkt anlegen.
    supports_empty_folders = True

    def __init__(self, settings: Settings, api_key: str):
        workspace_key = (settings.workspace_id or "").strip()
        if not is_workspace_key(workspace_key):
            raise SyncError(
                "Kein gültiger Tarkle-Workspace-Key (8 Zeichen) im Profil."
            )
        self.settings = settings
        self.workspace_key = workspace_key
        self.api = TarkleApi(api_key)
        self._items: dict[str, dict[str, Any]] = {}
        self._folder_ids: dict[str, str] = {"": ""}
        self._target_path = ""

    def close(self) -> None:
        self.api.close()

    @property
    def target_path(self) -> str:
        return self._target_path

    def list_workspaces(self) -> list[dict[str, str]]:
        return self.api.list_workspaces()

    def list_tree(self) -> list[RemoteEntry]:
        raw_items, target_path, root_id = self.api.list_tree(
            self.workspace_key,
            self.settings.remote_folder_id,
        )
        self._target_path = target_path
        self._items.clear()
        self._folder_ids = {"": root_id}
        entries: list[RemoteEntry] = []
        for item in raw_items:
            path = str(item.get("rel_path") or "")
            if not path:
                continue
            entry_id = str(item.get("id") or "")
            parent_id = str(item.get("parent_id") or "")
            if item.get("type") == "folder":
                self._folder_ids[path] = entry_id
                entries.append(
                    RemoteEntry(
                        path=path,
                        kind="dir",
                        entry_id=entry_id,
                        parent_id=parent_id,
                    )
                )
                continue
            self._items[entry_id] = item
            entries.append(
                RemoteEntry(
                    path=path,
                    kind="file",
                    size=int(item.get("size") or 0),
                    mtime=_parse_time(
                        item.get("modified_at") or item.get("updated_at")
                    ),
                    entry_id=entry_id,
                    content_hash=str(item.get("sha256") or ""),
                    parent_id=parent_id,
                )
            )
        return entries

    def _find_item(self, entry: RemoteEntry) -> dict[str, Any]:
        item = self._items.get(entry.entry_id)
        if item:
            return item
        item = self.api.get_file(self.workspace_key, entry.entry_id)
        if item.get("type") != "file":
            raise TarkleApiError(f"Kein Datei-Eintrag für {entry.path}.")
        self._items[entry.entry_id] = item
        return item

    def download(self, entry: RemoteEntry, destination: Path) -> None:
        item = self._find_item(entry)
        self.api.download_to(self.workspace_key, item, destination)

    def download_range(self, entry: RemoteEntry, start: int, length: int) -> bytes:
        return self.api.download_range(
            self.workspace_key,
            entry.entry_id,
            start,
            length,
        )

    def _full_remote_path(self, remote_path: str) -> str:
        path = remote_path.replace("\\", "/").strip("/")
        return f"{self._target_path}/{path}" if self._target_path else path

    def _ensure_folder(self, remote_dir: str) -> str:
        relative = remote_dir.replace("\\", "/").strip("/")
        if not relative:
            return self._folder_ids.get("", "")
        cached = self._folder_ids.get(relative)
        if cached:
            return cached
        parent_id = self._folder_ids.get("", "")
        current = ""
        for part in relative.split("/"):
            if not part:
                continue
            current = f"{current}/{part}" if current else part
            existing = self._folder_ids.get(current)
            if existing:
                parent_id = existing
                continue
            full_path = self._full_remote_path(current)
            try:
                entry = self.api.create_folder(
                    self.workspace_key,
                    part,
                    parent_id or None,
                )
            except TarkleApiError as exc:
                if exc.code != "name_taken":
                    raise
                entry = self.api.get_by_path(
                    self.workspace_key,
                    full_path,
                )
                if entry.get("type") != "folder":
                    raise SyncError(
                        f"Remote-Pfad ist keine Ordner: {full_path}"
                    ) from exc
            parent_id = str(entry.get("id") or "")
            self._folder_ids[current] = parent_id
            self._items[parent_id] = entry
        return parent_id

    def upload(
        self,
        local_path: Path,
        remote_path: str,
        *,
        mtime: float | None = None,
        remote_entry: RemoteEntry | None = None,
        crypto_generation: int = 0,
    ) -> RemoteEntry:
        remote = str(PurePosixPath(remote_path.replace("\\", "/")))
        relative_parent = str(PurePosixPath(remote).parent)
        if relative_parent == ".":
            relative_parent = ""
        if remote_entry is not None and remote_entry.parent_id:
            folder_id = remote_entry.parent_id
        else:
            folder_id = self._ensure_folder(relative_parent)
        full_path = self._full_remote_path(remote)
        item = self.api.upload_file(
            self.workspace_key,
            local_path,
            full_path,
            folder_id=folder_id or None,
            modified_at=mtime,
            existing_entry_id=(
                remote_entry.entry_id if remote_entry is not None else None
            ),
        )
        entry_id = str(item.get("id") or "")
        if entry_id:
            self._items[entry_id] = item
        if item.get("type") == "folder":
            raise TarkleApiError(f"Upload lieferte einen Ordner: {remote}")
        return RemoteEntry(
            path=remote_path,
            kind="file",
            size=int(item.get("size") or local_path.stat().st_size),
            # 0 bedeutet für die Sync-Engine "nicht vergleichen"; Inhalte
            # werden über Größe und SHA-256 erkannt.
            mtime=0.0,
            entry_id=entry_id,
            content_hash=str(item.get("sha256") or ""),
            parent_id=str(item.get("parent_id") or folder_id or ""),
        )

    def delete(self, entry: RemoteEntry) -> None:
        if not entry.entry_id:
            raise SyncError(f"Remote-Eintrag ohne ID: {entry.path}")
        self.api.delete_file(self.workspace_key, entry.entry_id)
        self._items.pop(entry.entry_id, None)
        self._folder_ids.pop(entry.path, None)

    def create_folder(self, remote_path: str) -> RemoteEntry:
        entry_id = self._ensure_folder(remote_path)
        relative = remote_path.replace("\\", "/").strip("/")
        parent = str(PurePosixPath(relative).parent)
        if parent == ".":
            parent = ""
        return RemoteEntry(
            path=remote_path,
            kind="dir",
            entry_id=entry_id,
            parent_id=self._folder_ids.get(parent, ""),
        )


def probe_api(settings: Settings, api_key: str) -> dict[str, Any]:
    """Prüft API-Key, Workspaces und Speicherplatz ohne den Key auszugeben."""
    api = TarkleApi(api_key)
    try:
        workspaces = api.list_workspaces()
        report: dict[str, Any] = {"workspaces": workspaces}
        usage: dict[str, dict[str, Any]] = {}
        for workspace in workspaces:
            key = str(workspace.get("workspace_key") or "")
            if not key:
                continue
            try:
                usage[str(workspace.get("name") or key)] = api.usage(key)
            except TarkleApiError as exc:
                usage[str(workspace.get("name") or key)] = {"error": str(exc)}
        report["usage"] = usage
        return report
    finally:
        api.close()
