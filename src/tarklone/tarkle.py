from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from .auth import Token
from .config import Settings
from .mcp import McpClient, tool_json
from .sync import RemoteEntry, RemoteUnsupported, SyncError
from .webapi import TarkleWeb, WebApiError, _parse_time

_CONTEXT = (
    "Synchronizing a user's local folder with their Tarkle workspace "
    "to keep both locations up to date."
)


class TarkleRemote:
    """Tarkle-Anbindung über MCP (Liste/Upload) und die Web-API (Download/Löschen)."""

    # Tarkle legt Ordner erst beim ersten Upload einer Datei an.
    supports_empty_folders = False

    def __init__(self, settings: Settings, token: Token | str):
        if isinstance(token, str):
            from .auth import jwt_claims

            claims = jwt_claims(token)
            token = Token(
                access_token=token,
                expires_at=float(claims.get("exp", 0) or 0),
                email=str(claims.get("email", "")),
            )
        self.settings = settings
        self.mcp = McpClient(settings.mcp_url, token.access_token)
        self.web = TarkleWeb(settings, token)
        self._items: dict[str, dict[str, Any]] = {}
        self._target_path = ""

    def close(self) -> None:
        self.mcp.close()
        self.web.close()

    def _mcp_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        payload = dict(arguments)
        payload.setdefault("context", _CONTEXT)
        payload.setdefault("llm_model", "unknown")
        result = self.mcp.call_tool(name, payload)
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        parsed = tool_json(result)
        return parsed if isinstance(parsed, dict) else {}

    def list_workspaces(self) -> list[dict[str, str]]:
        self.mcp.initialize()
        data = self._mcp_call("tarkle_list_workspaces", {})
        workspaces = data.get("workspaces")
        if not isinstance(workspaces, list):
            return []
        return [
            {
                "id": str(item.get("workspaceId", "")),
                "name": str(item.get("name", "")),
                "role": str(item.get("role", "")),
            }
            for item in workspaces
            if isinstance(item, dict) and item.get("workspaceId")
        ]

    def list_tree(self) -> list[RemoteEntry]:
        if not self.settings.workspace_id:
            raise RemoteUnsupported("Kein Tarkle-Workspace konfiguriert.")
        raw_items = list(self.web.list_tree_items(self.settings.workspace_id))
        self._items.clear()

        target_path = ""
        target = (
            self.settings.remote_folder_id.strip().replace("\\", "/").strip("/")
        )
        if target:
            for path, kind, item in raw_items:
                if kind != "dir":
                    continue
                if (
                    path == target
                    or str(item.get("id")) == target
                    or str(item.get("folder_id")) == target
                ):
                    target_path = path
                    break
            if not target_path:
                raise SyncError(f"Remote-Ordner nicht gefunden: {target}")

        self._target_path = target_path
        entries: list[RemoteEntry] = []
        for path, kind, item in raw_items:
            if target_path:
                if not path.startswith(target_path + "/"):
                    continue
                relative = path[len(target_path) + 1 :]
            else:
                relative = path
            if kind == "file":
                entry_id = str(item.get("id", ""))
                self._items[entry_id] = item
                entries.append(
                    RemoteEntry(
                        path=relative,
                        kind="file",
                        size=int(item.get("size") or 0),
                        mtime=_parse_time(item.get("updated_at")),
                        entry_id=entry_id,
                        parent_id=str(item.get("folder_id") or ""),
                    )
                )
            else:
                entries.append(
                    RemoteEntry(
                        path=relative,
                        kind="dir",
                        entry_id=str(item.get("id") or ""),
                        parent_id=str(item.get("parent_folder") or ""),
                    )
                )
        return entries

    def _find_item(self, entry: RemoteEntry) -> dict[str, Any]:
        item = self._items.get(entry.entry_id)
        if item:
            return item
        lookup_path = entry.remote_path or entry.path
        for candidate in self.web.search_files(
            self.settings.workspace_id,
            query=PurePosixPath(lookup_path).name,
        ):
            if str(candidate.get("id")) == entry.entry_id or str(
                candidate.get("file_id")
            ) == entry.entry_id:
                self._items[entry.entry_id] = candidate
                return candidate
        raise WebApiError(f"Datei nicht in Tarkle gefunden: {entry.path}")

    def download(self, entry: RemoteEntry, destination: Path) -> None:
        item = self._find_item(entry)
        self.web.download_to(item, self.settings.workspace_id, destination)

    def download_range(self, entry: RemoteEntry, start: int, length: int) -> bytes:
        item = self._find_item(entry)
        return self.web.download_range(
            item,
            self.settings.workspace_id,
            start,
            length,
        )

    def upload(
        self,
        local_path: Path,
        remote_path: str,
        *,
        mtime: float | None = None,
        remote_entry: RemoteEntry | None = None,
    ) -> RemoteEntry:
        existing = self._items.get(remote_entry.entry_id) if remote_entry else None
        full_path = f"{self._target_path}/{remote_path}" if self._target_path else remote_path
        if existing is not None:
            try:
                item = self.web.update_file(existing, local_path, self.settings.workspace_id)
            except WebApiError as exc:
                if "Pro feature" not in str(exc) and "402" not in str(exc):
                    raise
                # Free-Plan: keine Dateiversionen. Alte Fassung in den
                # Papierkorb legen und die neue unter demselben Namen hochladen.
                self.web.move_to_bin(existing)
                self._items.pop(remote_entry.entry_id, None)
                item = self.web.upload_file(local_path, full_path, self.settings.workspace_id)
        else:
            item = self.web.upload_file(local_path, full_path, self.settings.workspace_id)
        entry_id = str(item.get("id", remote_entry.entry_id if remote_entry else ""))
        if entry_id:
            self._items[entry_id] = item
        # Die Zeitstempel der Web-App sind nicht einheitlich formatiert. Ein
        # mtime-Wert von 0 bedeutet für die Sync-Engine "nicht vergleichen";
        # Größe und Datei-ID werden weiterhin geprüft.
        return RemoteEntry(
            path=remote_path,
            kind="file",
            size=int(item.get("size") or local_path.stat().st_size),
            mtime=0.0,
            entry_id=entry_id,
            parent_id=str(item.get("folder_id") or ""),
        )

    def delete(self, entry: RemoteEntry) -> None:
        item = self._find_item(entry)
        self.web.move_to_bin(item)
        self._items.pop(entry.entry_id, None)

    def create_folder(self, remote_path: str) -> RemoteEntry | None:
        # Leere Ordner legt die Web-API nicht direkt an. Sobald eine Datei
        # darin hochgeladen wird, erzeugt /api/presign-folder den Ordner.
        return RemoteEntry(path=remote_path, kind="dir", entry_id="")


def probe_web_api(settings: Settings, token: Token) -> dict[str, Any]:
    """Prüft die Web-API-Anbindung und zeigt die Token-Claims (ohne Token)."""
    from .auth import jwt_claims

    claims = jwt_claims(token.access_token)
    results: dict[str, Any] = {
        "token": {
            "kind": "jwt" if claims else "opaque",
            "aud": claims.get("aud"),
            "role": claims.get("role"),
            "email": claims.get("email"),
            "exp": claims.get("exp"),
        }
    }
    web = None
    try:
        web = TarkleWeb(settings, token)
        results["cookie_auth"] = True
        if settings.workspace_id:
            items = web.search_files(settings.workspace_id, page_size=5)
            results["search_files"] = {"status": 200, "items": len(items)}
        results["download"] = "available"
    except Exception as exc:  # noqa: BLE001 - Diagnose
        results["cookie_auth"] = False
        results["error"] = str(exc)
    finally:
        if web is not None:
            web.close()
    return results
