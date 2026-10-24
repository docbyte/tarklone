from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterator
from urllib.parse import urlencode

import httpx

from .auth import ANON_KEY, SUPABASE_URL, Token, jwt_claims
from .config import Settings


class WebApiError(RuntimeError):
    pass


class WebAuthError(WebApiError):
    pass


def _format_size(size: int) -> str:
    value = float(size)
    for unit, factor in (("tb", 1024**4), ("gb", 1024**3), ("mb", 1024**2), ("kb", 1024)):
        if value >= factor:
            return f"{value / factor:.2f}{unit}"
    return f"{int(size)}b"


def _content_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _item_type(name: str) -> str:
    mime = _content_type(name)
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if extension in {"ts", "tsx"}:
        return "document"
    if "image" in mime:
        return "photo"
    if "video" in mime:
        return "video"
    if "audio" in mime:
        return "audio"
    return "document"


_RSC_PATTERN = re.compile(r'self\.__next_f\.push\(\[1,("(?:\\.|[^"\\])*")\]\)')


def _rsc_text(html: str) -> str:
    chunks: list[str] = []
    for match in _RSC_PATTERN.finditer(html):
        try:
            chunks.append(json.loads(match.group(1)))
        except ValueError:
            continue
    return "".join(chunks)


def _json_arrays(text: str, key: str) -> Iterator[list[Any]]:
    decoder = json.JSONDecoder()
    pattern = re.compile(rf'"{re.escape(key)}"\s*:\s*')
    position = 0
    while True:
        match = pattern.search(text, position)
        if not match:
            return
        start = match.end()
        try:
            value, end = decoder.raw_decode(text[start:])
        except ValueError:
            position = start
            continue
        if isinstance(value, list):
            yield value
        position = start + end


def _file_array(html: str) -> list[dict[str, Any]]:
    for array in _json_arrays(_rsc_text(html), "files"):
        if array and isinstance(array[0], dict) and "name" in array[0]:
            return [item for item in array if isinstance(item, dict)]
    return []


def _workspace_array(html: str) -> list[dict[str, Any]]:
    for array in _json_arrays(_rsc_text(html), "workspaces"):
        if array and isinstance(array[0], dict) and "name" in array[0]:
            return [item for item in array if isinstance(item, dict)]
    return []


def _parse_time(value: Any) -> float:
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value)
    for fmt in ("%m/%d/%Y %I:%M %p", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class TarkleWeb:
    """Zugriff auf die inoffizielle Tarkle-Web-API mit Supabase-Session-Cookie."""

    def __init__(
        self,
        settings: Settings,
        token: Token,
        http: httpx.Client | None = None,
    ):
        self.settings = settings
        self.token = token
        self.http = http or httpx.Client(
            base_url=settings.server_url.rstrip("/"),
            timeout=60.0,
            follow_redirects=False,
        )
        self.user_id = jwt_claims(token.access_token).get("sub", "")
        self._cookie = self._build_cookie()
        self._headers = {
            "Cookie": self._cookie,
            "Accept": "application/json",
            "User-Agent": "tarklone/0.1",
        }
        self._workspace_map: dict[str, dict[str, Any]] | None = None
        self._default_slug = ""

    def close(self) -> None:
        self.http.close()

    def _build_cookie(self) -> str:
        try:
            response = self.http.get(
                f"{SUPABASE_URL}/auth/v1/user",
                headers={
                    "apikey": ANON_KEY,
                    "Authorization": f"Bearer {self.token.access_token}",
                },
            )
        except httpx.HTTPError as exc:
            raise WebApiError(f"Tarkle-Sitzung konnte nicht geladen werden: {exc}") from exc
        if response.status_code != 200:
            raise WebAuthError(
                "Tarkle-Sitzung ist abgelaufen. Bitte in tarklone erneut anmelden."
            )
        user = response.json()
        claims = jwt_claims(self.token.access_token)
        expires_in = max(0, int(claims.get("exp", 0)) - int(claims.get("iat", 0)))
        session = {
            "access_token": self.token.access_token,
            "token_type": "bearer",
            "expires_in": expires_in,
            "expires_at": self.token.expires_at,
            "refresh_token": self.token.refresh_token,
            "user": user,
        }
        raw = json.dumps(session, separators=(",", ":"), ensure_ascii=False)
        value = "base64-" + base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode()
        name = "sb-dzvfdojgelkzppeaqncy-auth-token"
        # @supabase/ssr liest maximal 3180 Zeichen pro Cookie und hängt
        # nummerierte Chunks an.
        if len(value) <= 3180:
            return f"{name}={value}"
        chunks = []
        for index in range(0, len(value), 3180):
            chunks.append(f"{name}.{index // 3180}={value[index:index + 3180]}")
        return "; ".join(chunks)

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = dict(self._headers)
        json_body = kwargs.pop("json_body", None)
        if json_body is not None:
            kwargs["json"] = json_body
        kwargs["headers"] = {**headers, **kwargs.get("headers", {})}
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise WebApiError(f"Tarkle-Web-API nicht erreichbar: {exc}") from exc
        if response.status_code == 401:
            raise WebAuthError(
                "Tarkle-Web-API hat die Sitzung abgelehnt. Bitte erneut anmelden."
            )
        if response.status_code >= 400:
            detail = response.text[:300]
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            if isinstance(payload, dict):
                detail = str(payload.get("error") or payload.get("message") or detail)
            raise WebApiError(f"Tarkle-Web-API Fehler {response.status_code}: {detail}")
        return response

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self._request(method, path, **kwargs)
        try:
            data = response.json()
        except ValueError as exc:
            raise WebApiError("Tarkle-Web-API lieferte keine gültige JSON-Antwort.") from exc
        return data if isinstance(data, dict) else {"data": data}

    def search_files(
        self,
        workspace_id: str,
        query: str = "",
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._json(
                "POST",
                "/api/search-files",
                json_body={
                    "workspaceId": workspace_id,
                    "searchQuery": query,
                    "page": page,
                    "pageSize": page_size,
                },
            )
            batch = data.get("files")
            if not isinstance(batch, list):
                break
            items.extend(item for item in batch if isinstance(item, dict))
            total_pages = int(data.get("totalPages") or 1)
            if page >= total_pages:
                break
            page += 1
        return items

    def workspace_map(self) -> dict[str, dict[str, Any]]:
        if self._workspace_map is not None:
            return self._workspace_map
        response = self._request("GET", "/files")
        location = response.headers.get("location") or ""
        if not location:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            if isinstance(payload, dict):
                location = str(payload.get("redirect") or "")
        match = re.search(r"/app/([^/]+)/files", location)
        if not match:
            raise WebApiError("Tarkle-Workspace-Pfad konnte nicht ermittelt werden.")
        self._default_slug = match.group(1)
        page = self._request("GET", f"/app/{self._default_slug}/files")
        workspaces = _workspace_array(page.text)
        self._workspace_map = {
            str(workspace.get("id")): workspace
            for workspace in workspaces
            if isinstance(workspace, dict) and workspace.get("id")
        }
        return self._workspace_map

    def workspace_key(self, workspace_id: str) -> str:
        workspace = self.workspace_map().get(workspace_id)
        if workspace:
            key = workspace.get("workspace_key")
            if isinstance(key, str) and key:
                return key
        if self._default_slug:
            return self._default_slug
        raise WebApiError(f"Workspace {workspace_id} wurde nicht gefunden.")

    def _list_folder(self, key: str, folder_row_id: str | None) -> list[dict[str, Any]]:
        path = f"/app/{key}/files" if not folder_row_id else f"/app/{key}/folder/{folder_row_id}"
        page = self._request("GET", path)
        items = _file_array(page.text)
        text = _rsc_text(page.text)
        total_match = re.search(r'"totalItems":(\d+)', text)
        size_match = re.search(r'"pageSize":(\d+)', text)
        total = int(total_match.group(1)) if total_match else len(items)
        page_size = int(size_match.group(1)) if size_match else max(len(items), 50)
        if total > len(items) and page_size > 0:
            page_number = 2
            while len(items) < total:
                next_page = self._request("GET", f"{path}?fi={page_number}")
                batch = _file_array(next_page.text)
                if not batch:
                    break
                items.extend(batch)
                page_number += 1
        return items

    def list_tree_items(self, workspace_id: str) -> Iterator[tuple[str, str, dict[str, Any]]]:
        """Liefert (Pfad, "file"|"dir", Rohobjekt) rekursiv aus der Web-App."""
        key = self.workspace_key(workspace_id)
        seen: set[str] = set()

        def walk(folder_row_id: str | None, prefix: str) -> Iterator[tuple[str, str, dict[str, Any]]]:
            for item in self._list_folder(key, folder_row_id):
                name = str(item.get("name", ""))
                if not name or name == "File requests":
                    continue
                path = f"{prefix}/{name}" if prefix else name
                if path in seen:
                    continue
                seen.add(path)
                is_file = bool(item.get("type")) and bool(item.get("file_id"))
                is_folder = not item.get("type") and not item.get("file_id")
                if is_file:
                    yield path, "file", item
                elif is_folder:
                    yield path, "dir", item
                    child_id = item.get("id")
                    if child_id:
                        yield from walk(str(child_id), path)

        yield from walk(None, "")

    def download_url(self, item: dict[str, Any], workspace_id: str) -> str:
        data = self._json(
            "POST",
            "/api/download-file",
            json_body={"item": item, "workspace_id": workspace_id},
        )
        url = data.get("downloadUrl")
        if isinstance(url, str) and url:
            return url
        storage_path = data.get("storagePath")
        if isinstance(storage_path, str) and storage_path:
            query = urlencode(
                {
                    "path": storage_path,
                    "fileId": str(item.get("id", "")),
                    "workspaceId": workspace_id,
                }
            )
            presign = self._json("GET", f"/api/download-folder?{query}")
            presigned = presign.get("presignedUrl")
            if isinstance(presigned, str) and presigned:
                return presigned
        raise WebApiError("Tarkle lieferte keine Download-URL.")

    def download_to(
        self,
        item: dict[str, Any],
        workspace_id: str,
        destination: Path,
    ) -> None:
        url = self.download_url(item, workspace_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(destination.name + ".tarklone-part")
        try:
            with httpx.stream("GET", url, timeout=120.0, follow_redirects=True) as response:
                if response.status_code >= 400:
                    raise WebApiError(f"Download fehlgeschlagen: HTTP {response.status_code}")
                with temp.open("wb") as handle:
                    for chunk in response.iter_bytes(1024 * 1024):
                        handle.write(chunk)
            expected = item.get("size")
            if expected not in (None, ""):
                actual = temp.stat().st_size
                if int(expected) != actual:
                    raise WebApiError(
                        f"Download unvollständig: {actual} statt {expected} Bytes."
                    )
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)

    def upload_file(
        self,
        local_path: Path,
        remote_path: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        remote = str(PurePosixPath(remote_path.replace("\\", "/")))
        name = PurePosixPath(remote).name
        if not name:
            raise WebApiError("Ungültiger Remote-Dateiname.")
        size = local_path.stat().st_size
        mime = _content_type(name)
        file_id = str(uuid.uuid4())
        parent_path = str(PurePosixPath(remote).parent)
        if parent_path == ".":
            parent_path = ""

        if parent_path:
            data = self._json(
                "POST",
                "/api/presign-folder",
                json_body={
                    "workspace_id": workspace_id,
                    "folder_id": "",
                    "fileDetails": [
                        {
                            "path": remote,
                            "size": size,
                            "type": mime,
                            "uuid": file_id,
                        }
                    ],
                },
            )
            payload = data.get("data") if isinstance(data.get("data"), dict) else data
            folder_id = (payload.get("folderPathToId") or {}).get(parent_path, "")
            presigned_files = payload.get("presignedFiles") or []
        else:
            data = self._json(
                "POST",
                "/api/presign-files",
                json_body={
                    "file_ids": [file_id],
                    "folder_id": "",
                    "workspace_id": workspace_id,
                    "is_master_file": False,
                    "file_size": size,
                },
            )
            folder_id = ""
            presigned_files = data.get("presignedFiles") or []

        presigned_url = ""
        for entry in presigned_files:
            if isinstance(entry, dict) and (
                entry.get("uuid") in (None, file_id)
                or entry.get("fileId") in (None, file_id)
            ):
                presigned_url = str(entry.get("presignedUrl") or "")
                if presigned_url:
                    break
        if not presigned_url:
            raise WebApiError("Tarkle lieferte keine Upload-URL.")

        with local_path.open("rb") as handle:
            upload = httpx.put(
                presigned_url,
                content=handle,
                headers={"Content-Type": mime},
                timeout=300.0,
            )
        if upload.status_code >= 400:
            raise WebApiError(f"Upload fehlgeschlagen: HTTP {upload.status_code}")

        row = {
            "folder_id": folder_id or None,
            "file_id": file_id,
            "name": name,
            "display_size": _format_size(size),
            "size": size,
            "type": _item_type(name),
            "workspace_id": workspace_id,
        }
        self._json(
            "POST",
            "/api/files",
            json_body={
                "bulkData": [row],
                "workspace_id": workspace_id,
                "source": "library",
                "user_id": self.user_id,
            },
        )

        for item in self.search_files(workspace_id, query=name):
            if str(item.get("file_id") or "") == file_id:
                return item
        return {
            "id": file_id,
            "file_id": file_id,
            "name": name,
            "type": _item_type(name),
            "size": str(size),
            "folder_id": folder_id or None,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

    def update_file(
        self,
        item: dict[str, Any],
        local_path: Path,
        workspace_id: str,
    ) -> dict[str, Any]:
        """Ersetzt den Inhalt einer bestehenden Datei als neue Version."""
        row_id = item.get("id")
        if row_id is None:
            raise WebApiError("Datei-ID fehlt für das Versions-Update.")
        size = local_path.stat().st_size
        name = str(item.get("name") or local_path.name)
        mime = _content_type(name)
        digest = ""
        import hashlib

        hasher = hashlib.sha256()
        with local_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()

        try:
            prepared = self._json(
                "POST",
                "/api/file-versions/prepare",
                json_body={
                    "workspace_id": workspace_id,
                    "file_row_id": row_id,
                    "size": size,
                    "type": mime,
                    "allow_transfer": False,
                },
            )
        except WebApiError as exc:
            if "IDENTICAL_CONTENT" in str(exc):
                return item
            raise
        if prepared.get("multipart") or not prepared.get("url"):
            raise WebApiError(
                "Dieses Update benötigt einen Multipart-Upload; "
                "große Dateien werden noch nicht unterstützt."
            )
        upload_url = str(prepared.get("url"))
        upload_id = str(prepared.get("upload_id") or "")
        if not upload_id:
            raise WebApiError("Tarkle lieferte keine Upload-ID für das Update.")
        with local_path.open("rb") as handle:
            upload = httpx.put(
                upload_url,
                content=handle,
                headers={"Content-Type": mime},
                timeout=300.0,
            )
        if upload.status_code >= 400:
            raise WebApiError(f"Versions-Upload fehlgeschlagen: HTTP {upload.status_code}")
        committed = self._json(
            "POST",
            "/api/file-versions/commit",
            json_body={
                "workspace_id": workspace_id,
                "upload_id": upload_id,
                "sha256": digest,
                "type": None,
            },
        )
        if isinstance(committed, dict) and committed.get("file"):
            candidate = committed.get("file")
            if isinstance(candidate, dict):
                return candidate
        for candidate in self.search_files(workspace_id, query=name):
            if str(candidate.get("id")) == str(row_id):
                return candidate
        return {
            **item,
            "size": str(size),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

    def move_to_bin(self, item: dict[str, Any]) -> None:
        is_folder = not item.get("type") and not item.get("file_id")
        path = "/api/move-folder-to-bin" if is_folder else "/api/move-file-to-bin"
        self._json("POST", path, json_body={"item": item, "deleted": True})
