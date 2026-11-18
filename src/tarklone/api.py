"""Client für die offizielle Tarkle-API (https://tarkle.tech/api/v1).

Die API authentifiziert mit einem Bearer-API-Key (``tk_...``) und erwartet bei
allen Workspace-Aufrufen zusätzlich den 8-stelligen Workspace-Key im Header
``X-Workspace-Key``. ``tarkle.com/api/v1`` leitet auf ``tarkle.tech`` weiter.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from . import __version__
from .config import DEFAULT_API_BASE, normalize_api_key, validate_api_base

USER_AGENT = f"tarklone/{__version__}"
_WRITE_METHODS = {"POST", "PATCH", "PUT", "DELETE"}
_RETRY_STATUS = {502, 503, 504}


class TarkleApiError(RuntimeError):
    """Fehler der Tarkle-API mit stabilem ``code`` und HTTP-Status."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        code: str = "",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details or {}


class TarkleAuthError(TarkleApiError):
    """API-Key fehlt, ist ungültig oder wurde widerrufen."""


def normalize_remote_path(path: str) -> str:
    """Normalisiert einen Workspace-Pfad ohne führenden/abschließenden Slash."""
    value = (path or "").replace("\\", "/").strip()
    parts = [part for part in value.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise TarkleApiError("Ungültiger Workspace-Pfad.")
    return "/".join(parts)


def _parse_time(value: Any) -> float:
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if number > 10_000_000_000 else number
    text = str(value)
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def _content_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _iso_mtime(mtime: float | None) -> str | None:
    """Wandelt eine lokale mtime in den von der Upload-API erwarteten ISO-Wert."""
    if not mtime:
        return None
    try:
        moment = datetime.fromtimestamp(float(mtime), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
    now = datetime.now(UTC)
    if moment.timestamp() <= 86400:
        return None
    if moment > now + timedelta(days=1):
        moment = now
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _problem(response: httpx.Response) -> tuple[str, str, dict[str, Any]]:
    code = ""
    detail = ""
    details: dict[str, Any] = {}
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if isinstance(payload, dict):
        code = str(payload.get("code") or "")
        detail = str(payload.get("detail") or payload.get("title") or "")
        raw_details = payload.get("details")
        if isinstance(raw_details, dict):
            details = raw_details
        legacy = payload.get("error")
        if isinstance(legacy, dict):
            code = code or str(legacy.get("code") or "")
            detail = detail or str(legacy.get("message") or "")
            legacy_details = legacy.get("details")
            if isinstance(legacy_details, dict) and not details:
                details = legacy_details
    if not detail:
        detail = response.text[:300].strip() or response.reason_phrase
    return code, detail, details


class TarkleApi:
    """Synchroner Client für die öffentliche Tarkle-API v1."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_API_BASE,
        http: httpx.Client | None = None,
    ):
        key = normalize_api_key(api_key)
        if not key:
            raise TarkleAuthError("Kein Tarkle-API-Key gespeichert.")
        self.api_key = key
        self.base_url = validate_api_base(base_url).rstrip("/")
        self.http = http or httpx.Client(
            timeout=httpx.Timeout(120.0, connect=20.0),
            follow_redirects=False,
        )
        self._owns_http = http is None
        self.retries = 3

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    # --- HTTP ------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        workspace_key: str = "",
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        retries: int | None = None,
    ) -> dict[str, Any]:
        method = method.upper()
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if workspace_key:
            headers["X-Workspace-Key"] = workspace_key
        if method in _WRITE_METHODS:
            headers["Idempotency-Key"] = idempotency_key or uuid.uuid4().hex
        url = f"{self.base_url}{path}"
        attempts = max(1, retries if retries is not None else self.retries)
        response: httpx.Response | None = None
        redirects_left = 3
        attempt = 0
        while attempt < attempts:
            try:
                response = self.http.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                raise TarkleApiError(f"Tarkle-API nicht erreichbar: {exc}") from exc

            if response.status_code in (307, 308):
                location = response.headers.get("location", "")
                target = urljoin(url, location) if location else ""
                parsed = urlsplit(target)
                host = (parsed.hostname or "").lower()
                if (
                    redirects_left > 0
                    and parsed.scheme == "https"
                    and parsed.port in (None, 443)
                    and parsed.username is None
                    and parsed.password is None
                    and host
                    in {"tarkle.com", "tarkle.tech", "www.tarkle.com", "www.tarkle.tech"}
                ):
                    redirects_left -= 1
                    url = target
                    if parsed.query:
                        params = None
                    continue
                raise TarkleApiError(
                    "Tarkle-API leitete auf ein nicht erlaubtes Ziel weiter.",
                    status=response.status_code,
                    code="unsafe_redirect",
                )

            if response.status_code == 401:
                code, detail, details = _problem(response)
                if code == "legacy_token":
                    message = (
                        "Dieser Tarkle-API-Key ist ein alter Workspace-Key und "
                        "wird von der API v1 nicht mehr akzeptiert. Bitte in "
                        "Tarkle unter Einstellungen → API einen neuen Key "
                        "erstellen und eintragen."
                    )
                else:
                    message = (
                        "Der Tarkle-API-Key ist ungültig oder wurde gelöscht. "
                        "Bitte in den Einstellungen einen neuen Key eintragen."
                    )
                raise TarkleAuthError(
                    message + (f" ({detail})" if detail and code != "legacy_token" else ""),
                    status=401,
                    code=code or "unauthorized",
                    details=details,
                )

            if response.status_code == 429:
                code, detail, details = _problem(response)
                if code == "quota_exceeded" or attempt >= attempts - 1:
                    raise TarkleApiError(
                        detail or "Tarkle-API-Limit erreicht.",
                        status=429,
                        code=code or "rate_limited",
                        details=details,
                    )
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = max(1.0, min(float(retry_after), 60.0))
                except ValueError:
                    delay = 2.0 * (attempt + 1)
                time.sleep(delay)
                attempt += 1
                continue

            if response.status_code in _RETRY_STATUS and attempt < attempts - 1:
                time.sleep(1.0 * (attempt + 1))
                attempt += 1
                continue
            break

        assert response is not None
        if 300 <= response.status_code < 400:
            raise TarkleApiError(
                "Die Tarkle-API antwortete mit einer unerwarteten Weiterleitung.",
                status=response.status_code,
                code="unexpected_redirect",
            )
        if response.status_code >= 400:
            code, detail, details = _problem(response)
            raise TarkleApiError(
                f"Tarkle-API-Fehler {response.status_code}: {detail}",
                status=response.status_code,
                code=code,
                details=details,
            )
        if response.status_code == 204 or not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise TarkleApiError(
                "Tarkle-API lieferte keine gültige JSON-Antwort."
            ) from exc
        return payload if isinstance(payload, dict) else {"data": payload}

    def _stream(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return self.http.stream(method, url, follow_redirects=True, **kwargs)
        except httpx.HTTPError as exc:
            raise TarkleApiError(f"Tarkle-Download nicht erreichbar: {exc}") from exc

    # --- Workspaces / Usage ---------------------------------------------

    def list_workspaces(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/workspaces")
        workspaces = data.get("workspaces")
        if not isinstance(workspaces, list):
            return []
        result: list[dict[str, Any]] = []
        for item in workspaces:
            if not isinstance(item, dict) or not item.get("workspace_key"):
                continue
            result.append(
                {
                    "id": str(item["workspace_key"]),
                    "workspace_key": str(item["workspace_key"]),
                    "name": str(item.get("name") or item["workspace_key"]),
                    "url": str(item.get("url") or ""),
                    "role": str(item.get("relationship") or ""),
                }
            )
        return result

    def usage(self, workspace_key: str) -> dict[str, int]:
        data = self._request("GET", "/usage", workspace_key=workspace_key)
        storage = data.get("storage")
        if not isinstance(storage, dict):
            storage = {}
        used = int(storage.get("used_bytes") or 0)
        limit = int(storage.get("limit_bytes") or 0)
        left = int(storage.get("free_bytes") or 0)
        if limit and not left:
            left = max(0, limit - used)
        percent = round(used * 100 / limit) if limit else 0
        return {
            "used": used,
            "limit": limit,
            "left": left,
            "percent": max(0, min(100, percent)),
        }

    # --- Files / Folders -------------------------------------------------

    def get_file(self, workspace_key: str, entry_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/files/{quote(str(entry_id), safe='')}",
            workspace_key=workspace_key,
        )

    def get_by_path(self, workspace_key: str, path: str) -> dict[str, Any]:
        normalized = "/" + normalize_remote_path(path)
        return self._request(
            "GET",
            "/files/by-path",
            workspace_key=workspace_key,
            params={"path": normalized},
        )

    def _walk_tree(
        self,
        workspace_key: str,
        root_id: str,
        root_path: str,
    ) -> list[dict[str, Any]]:
        """Fallback für Bäume, die die rekursive API als zu groß ablehnt."""
        entries: list[dict[str, Any]] = []
        seen_folders: set[str] = set()

        def walk(folder_id: str, prefix: str) -> None:
            if folder_id:
                if folder_id in seen_folders:
                    return
                seen_folders.add(folder_id)
            cursor = ""
            while True:
                params: dict[str, Any] = {"limit": 1000}
                if folder_id:
                    params["folder"] = folder_id
                if cursor:
                    params["cursor"] = cursor
                data = self._request(
                    "GET",
                    "/files",
                    workspace_key=workspace_key,
                    params=params,
                )
                batch = data.get("entries")
                children: list[dict[str, Any]] = []
                if isinstance(batch, list):
                    for raw in batch:
                        if not isinstance(raw, dict) or raw.get("locked"):
                            continue
                        if raw.get("protection_kind") == "system":
                            continue
                        name = str(raw.get("name") or "")
                        if not name or name == "File requests":
                            continue
                        absolute = f"{prefix}/{name}" if prefix else name
                        relative = (
                            absolute[len(root_path) + 1 :]
                            if root_path and absolute.startswith(root_path + "/")
                            else absolute
                        )
                        item = dict(raw)
                        item["rel_path"] = relative
                        item["absolute_path"] = absolute
                        entries.append(item)
                        if item.get("type") == "folder":
                            children.append(item)
                next_cursor = data.get("next_cursor")
                cursor = str(next_cursor) if next_cursor else ""
                if not cursor:
                    break
            for child in children:
                walk(str(child.get("id") or ""), str(child["absolute_path"]))

        walk(root_id, root_path)
        return entries

    def list_tree(
        self,
        workspace_key: str,
        path: str = "",
    ) -> tuple[list[dict[str, Any]], str, str]:
        """Listet den gesamten Baum unter ``path``.

        Returns ``(entries, root_path, root_id)``. Jeder Eintrag enthält
        zusätzlich ``rel_path`` relativ zum gewählten Wurzelordner.
        """
        root_path = normalize_remote_path(path)
        root_id = ""
        if root_path:
            folder = self.get_by_path(workspace_key, root_path)
            if folder.get("type") != "folder":
                raise TarkleApiError(
                    f"Remote-Pfad ist kein Ordner: {root_path}",
                    code="not_a_folder",
                )
            root_path = normalize_remote_path(str(folder.get("path") or root_path))
            root_id = str(folder.get("id") or "")

        entries: list[dict[str, Any]] = []
        try:
            for kind in ("folder", "file"):
                cursor = ""
                while True:
                    params: dict[str, Any] = {
                        "recursive": "true",
                        "type": kind,
                        "limit": 1000,
                    }
                    if root_id:
                        params["folder"] = root_id
                    if cursor:
                        params["cursor"] = cursor
                    data = self._request(
                        "GET",
                        "/files",
                        workspace_key=workspace_key,
                        params=params,
                    )
                    batch = data.get("entries")
                    if isinstance(batch, list):
                        for raw in batch:
                            if not isinstance(raw, dict) or raw.get("locked"):
                                continue
                            if raw.get("protection_kind") == "system":
                                continue
                            if (
                                raw.get("type") == "folder"
                                and raw.get("name") == "File requests"
                            ):
                                continue
                            absolute = normalize_remote_path(
                                str(raw.get("path") or "")
                            )
                            if not absolute:
                                continue
                            if root_path:
                                if absolute == root_path:
                                    continue
                                if not absolute.startswith(root_path + "/"):
                                    continue
                                relative = absolute[len(root_path) + 1 :]
                            else:
                                relative = absolute
                            item = dict(raw)
                            item["rel_path"] = relative
                            item["absolute_path"] = absolute
                            entries.append(item)
                    next_cursor = data.get("next_cursor")
                    cursor = str(next_cursor) if next_cursor else ""
                    if not cursor:
                        break
        except TarkleApiError as exc:
            if exc.status == 413 and exc.code == "too_large":
                return (
                    self._walk_tree(workspace_key, root_id, root_path),
                    root_path,
                    root_id,
                )
            raise
        return entries, root_path, root_id

    def create_folder(
        self,
        workspace_key: str,
        name: str,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        if name.startswith("."):
            raise TarkleApiError(
                "Die Tarkle-API entfernt führende Punkte aus Ordnernamen. "
                f"Ordner kann nicht angelegt werden: {name}",
                code="unsupported_name",
            )
        body: dict[str, Any] = {"name": name, "visibility": "workspace"}
        if parent_id:
            body["parent_id"] = parent_id
        return self._request(
            "POST",
            "/folders",
            workspace_key=workspace_key,
            json_body=body,
        )

    def delete_file(
        self,
        workspace_key: str,
        entry_id: str,
        *,
        confirm_delete_transfers: bool = False,
    ) -> dict[str, Any]:
        params = (
            {"confirm_delete_transfers": "true"}
            if confirm_delete_transfers
            else None
        )
        return self._request(
            "DELETE",
            f"/files/{quote(str(entry_id), safe='')}",
            workspace_key=workspace_key,
            params=params,
        )

    # --- Download --------------------------------------------------------

    def download_url(self, workspace_key: str, entry_id: str) -> str:
        data = self._request(
            "GET",
            f"/files/{quote(str(entry_id), safe='')}/download",
            workspace_key=workspace_key,
        )
        url = data.get("download_url")
        if not isinstance(url, str) or not url:
            raise TarkleApiError("Tarkle lieferte keine Download-URL.")
        return url

    def download_range(
        self,
        workspace_key: str,
        entry_id: str,
        start: int,
        length: int,
    ) -> bytes:
        if start < 0 or length <= 0 or length > 1024 * 1024:
            raise TarkleApiError("Ungültiger Download-Bereich.")
        url = self.download_url(workspace_key, entry_id)
        end = start + length - 1
        with self._stream(
            "GET",
            url,
            headers={"Range": f"bytes={start}-{end}"},
            timeout=60.0,
        ) as response:
            if response.status_code not in (200, 206):
                raise TarkleApiError(
                    f"Range-Download fehlgeschlagen: HTTP {response.status_code}"
                )
            wanted_start = 0 if response.status_code == 206 else start
            wanted_end = length if response.status_code == 206 else start + length
            position = 0
            result = bytearray()
            try:
                for chunk in response.iter_bytes():
                    chunk_end = position + len(chunk)
                    if chunk_end > wanted_start and position < wanted_end:
                        left = max(0, wanted_start - position)
                        right = min(len(chunk), wanted_end - position)
                        result.extend(chunk[left:right])
                        if len(result) >= length:
                            break
                    position = chunk_end
            except httpx.HTTPError as exc:
                raise TarkleApiError(
                    f"Range-Download fehlgeschlagen: {exc}"
                ) from exc
            if len(result) != length:
                raise TarkleApiError("Range-Download lieferte nicht genug Bytes.")
            return bytes(result)

    def download_to(
        self,
        workspace_key: str,
        item: dict[str, Any],
        destination: Path,
    ) -> None:
        url = self.download_url(workspace_key, str(item.get("id") or ""))
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=".tarklone-download-",
            suffix=".part",
            dir=destination.parent,
        )
        temp = Path(temp_name)
        try:
            with self._stream("GET", url, timeout=300.0) as response:
                if response.status_code >= 400:
                    raise TarkleApiError(
                        f"Download fehlgeschlagen: HTTP {response.status_code}"
                    )
                with os.fdopen(fd, "wb") as handle:
                    fd = -1
                    for chunk in response.iter_bytes(1024 * 1024):
                        handle.write(chunk)
            expected = item.get("size")
            if expected not in (None, ""):
                actual = temp.stat().st_size
                if int(expected) != actual:
                    raise TarkleApiError(
                        f"Download unvollständig: {actual} statt {expected} Bytes."
                    )
            os.replace(temp, destination)
        finally:
            if fd >= 0:
                os.close(fd)
            temp.unlink(missing_ok=True)

    # --- Upload ----------------------------------------------------------

    def _put_file(
        self,
        url: str,
        path: Path,
        content_type: str,
    ) -> None:
        last_error = ""
        for attempt in range(3):
            try:
                with path.open("rb") as handle:
                    response = self.http.put(
                        url,
                        content=handle,
                        headers={"Content-Type": content_type},
                        timeout=300.0,
                        follow_redirects=True,
                    )
            except httpx.HTTPError as exc:
                last_error = str(exc)
                time.sleep(1.0 * (attempt + 1))
                continue
            if response.status_code < 400:
                return
            last_error = f"HTTP {response.status_code}"
            if (
                response.status_code == 429
                or response.status_code in _RETRY_STATUS
            ) and attempt < 2:
                time.sleep(1.0 * (attempt + 1))
                continue
            break
        raise TarkleApiError(f"Upload fehlgeschlagen: {last_error}")

    def _put_bytes(
        self,
        url: str,
        data: bytes,
        content_type: str,
    ) -> None:
        last_error = ""
        for attempt in range(3):
            try:
                response = self.http.put(
                    url,
                    content=data,
                    headers={"Content-Type": content_type},
                    timeout=300.0,
                    follow_redirects=True,
                )
            except httpx.HTTPError as exc:
                last_error = str(exc)
                time.sleep(1.0 * (attempt + 1))
                continue
            if response.status_code < 400:
                return
            last_error = f"HTTP {response.status_code}"
            if (
                response.status_code == 429
                or response.status_code in _RETRY_STATUS
            ) and attempt < 2:
                time.sleep(1.0 * (attempt + 1))
                continue
            break
        raise TarkleApiError(f"Upload fehlgeschlagen: {last_error}")

    def _part_urls(
        self,
        workspace_key: str,
        upload_id: str,
        from_part: int,
        count: int,
        multipart_id: str = "",
    ) -> dict[int, str]:
        body: dict[str, Any] = {"from_part": from_part, "count": count}
        if multipart_id:
            body["multipart_id"] = multipart_id
        data = self._request(
            "POST",
            f"/uploads/{quote(upload_id, safe='')}/parts",
            workspace_key=workspace_key,
            json_body=body,
        )
        result: dict[int, str] = {}
        uploads = data.get("uploads")
        if isinstance(uploads, list):
            for entry in uploads:
                if not isinstance(entry, dict):
                    continue
                try:
                    number = int(entry.get("partNumber"))
                except (TypeError, ValueError):
                    continue
                url = entry.get("url")
                if isinstance(url, str) and url:
                    result[number] = url
        return result

    def _abort_upload(self, workspace_key: str, upload_id: str) -> None:
        if not upload_id:
            return
        try:
            self._request(
                "DELETE",
                f"/uploads/{quote(upload_id, safe='')}",
                workspace_key=workspace_key,
            )
        except TarkleApiError:
            pass

    def _send_upload(
        self,
        workspace_key: str,
        started: dict[str, Any],
        local_path: Path,
        digest: str,
        modified_at: str | None,
    ) -> dict[str, Any]:
        upload_id = str(started.get("id") or "")
        if not upload_id:
            raise TarkleApiError("Tarkle lieferte keine Upload-ID.")
        mode = str(started.get("mode") or "single")
        content_type = str(
            started.get("content_type")
            or _content_type(str(started.get("name") or local_path.name))
        )
        multipart_id = str(started.get("multipart_id") or "")
        part_size = int(started.get("part_size") or 0)
        part_count = int(started.get("part_count") or 1)
        part_urls: dict[int, str] = {}
        uploads = started.get("uploads")
        if isinstance(uploads, list):
            for entry in uploads:
                if not isinstance(entry, dict):
                    continue
                try:
                    number = int(entry.get("partNumber"))
                except (TypeError, ValueError):
                    continue
                url = entry.get("url")
                if isinstance(url, str) and url:
                    part_urls[number] = url

        size = local_path.stat().st_size
        if mode == "single":
            url = part_urls.get(1, "")
            if not url:
                raise TarkleApiError("Tarkle lieferte keine Upload-URL.")
            self._put_file(url, local_path, content_type)
        else:
            if part_size <= 0:
                raise TarkleApiError("Tarkle lieferte eine ungültige Part-Größe.")
            with local_path.open("rb") as handle:
                offset = 0
                for number in range(1, part_count + 1):
                    length = min(part_size, max(0, size - offset))
                    handle.seek(offset)
                    chunk = handle.read(length)
                    if len(chunk) != length:
                        raise TarkleApiError("Lokale Datei ist während des Uploads kürzer geworden.")
                    if number not in part_urls:
                        first = number
                        count = min(100, part_count - first + 1)
                        part_urls.update(
                            self._part_urls(
                                workspace_key,
                                upload_id,
                                first,
                                count,
                                multipart_id,
                            )
                        )
                    url = part_urls.get(number)
                    if not url:
                        raise TarkleApiError(
                            f"Tarkle lieferte keine Upload-URL für Teil {number}."
                        )
                    self._put_bytes(url, chunk, content_type)
                    offset += length
                    part_urls.pop(number, None)

        body: dict[str, Any] = {"sha256": digest}
        if modified_at:
            body["modified_at"] = modified_at
        if multipart_id:
            body["multipart_id"] = multipart_id

        for attempt in range(3):
            try:
                return self._request(
                    "POST",
                    f"/uploads/{quote(upload_id, safe='')}/complete",
                    workspace_key=workspace_key,
                    json_body=body,
                )
            except TarkleApiError as exc:
                if exc.status != 409 or exc.code != "incomplete" or attempt >= 2:
                    raise
                missing = exc.details.get("missing_parts")
                if not isinstance(missing, list) or not missing:
                    raise
                missing_numbers = sorted(
                    {int(number) for number in missing if isinstance(number, (int, float))}
                )
                if len(missing_numbers) > 100:
                    missing_numbers = missing_numbers[:100]
                part_urls.update(
                    self._part_urls(
                        workspace_key,
                        upload_id,
                        missing_numbers[0],
                        min(100, missing_numbers[-1] - missing_numbers[0] + 1),
                        multipart_id,
                    )
                )
                with local_path.open("rb") as handle:
                    for number in missing_numbers:
                        url = part_urls.get(number)
                        if not url:
                            continue
                        offset = (number - 1) * part_size
                        length = min(part_size, max(0, size - offset))
                        handle.seek(offset)
                        chunk = handle.read(length)
                        if len(chunk) != length:
                            raise TarkleApiError("Lokale Datei ist während des Uploads kürzer geworden.")
                        self._put_bytes(url, chunk, content_type)
                        part_urls.pop(number, None)
        raise TarkleApiError("Upload konnte nicht abgeschlossen werden.")

    def upload_file(
        self,
        workspace_key: str,
        local_path: Path,
        remote_path: str,
        *,
        folder_id: str | None = None,
        modified_at: float | None = None,
        existing_entry_id: str | None = None,
    ) -> dict[str, Any]:
        path = normalize_remote_path(remote_path)
        name = PurePosixPath(path).name
        if not name:
            raise TarkleApiError("Ungültiger Remote-Dateiname.")
        if name.startswith("."):
            raise TarkleApiError(
                "Die Tarkle-API entfernt führende Punkte aus Dateinamen. "
                f"Datei kann nicht hochgeladen werden: {path}",
                code="unsupported_name",
            )
        size = local_path.stat().st_size
        digest = sha256_file(local_path)
        modified = _iso_mtime(modified_at)
        on_conflict = "version" if existing_entry_id else "separate"
        deleted_existing = False
        abort_id = str(existing_entry_id or "")

        while True:
            body: dict[str, Any] = {
                "name": name,
                "size": size,
                "content_type": _content_type(name),
                "sha256": digest,
            }
            if folder_id:
                body["folder_id"] = folder_id
            if modified:
                body["modified_at"] = modified
            if on_conflict:
                body["on_conflict"] = on_conflict
            try:
                started = self._request(
                    "POST",
                    "/uploads",
                    workspace_key=workspace_key,
                    json_body=body,
                    idempotency_key=uuid.uuid4().hex,
                )
            except TarkleApiError as exc:
                if exc.status != 409:
                    raise
                choices = [
                    str(choice)
                    for choice in (exc.details.get("choices") or [])
                    if isinstance(choice, str)
                ]
                if existing_entry_id and "version" in choices and on_conflict != "version":
                    on_conflict = "version"
                    continue
                if (
                    abort_id
                    and "separate" in choices
                    and not deleted_existing
                ):
                    # Free-Plan oder leere Datei: alte Fassung in den Papierkorb
                    # und die neue Datei unter demselben Namen anlegen.
                    self.delete_file(workspace_key, abort_id)
                    deleted_existing = True
                    on_conflict = "separate"
                    continue
                raise

            action = str(started.get("action") or "")

            if started.get("will_be_renamed"):
                self._abort_upload(workspace_key, str(started.get("id") or ""))
                created = started.get("file")
                if isinstance(created, dict) and created.get("id"):
                    try:
                        self.delete_file(
                            workspace_key, str(created["id"])
                        )
                    except TarkleApiError:
                        pass
                if abort_id and not deleted_existing:
                    self.delete_file(workspace_key, abort_id)
                    deleted_existing = True
                    on_conflict = "separate"
                    continue
                raise TarkleApiError(
                    f"Remote-Name ist bereits vergeben: {path}",
                    status=409,
                    code="name_taken",
                )

            if action == "skip":
                existing = started.get("existing")
                entry_id = ""
                if isinstance(existing, dict):
                    entry_id = str(existing.get("id") or "")
                if entry_id:
                    return self.get_file(workspace_key, entry_id)
                return self.get_by_path(workspace_key, path)

            if action == "created":
                created = started.get("file")
                if isinstance(created, dict):
                    return created
                return self.get_by_path(workspace_key, path)

            upload_id = str(started.get("id") or "")
            try:
                completed = self._send_upload(
                    workspace_key,
                    started,
                    local_path,
                    digest,
                    modified,
                )
            except Exception:
                self._abort_upload(workspace_key, upload_id)
                raise
            if isinstance(completed, dict) and completed.get("id"):
                return completed
            return self.get_by_path(workspace_key, path)
