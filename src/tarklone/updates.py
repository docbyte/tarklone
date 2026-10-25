from __future__ import annotations

import hashlib
import hmac
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import __version__

GITHUB_REPO = "docbyte/tarklone"
LATEST_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_ALLOWED_RELEASE_HOSTS = {
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
}


class UpdateError(RuntimeError):
    pass


@dataclass
class UpdateInfo:
    version: str
    tag: str
    name: str
    html_url: str
    asset_name: str
    asset_url: str
    checksum_url: str = ""
    notes: str = ""


def parse_version(value: str) -> tuple[int, ...]:
    numbers = [int(part) for part in re.findall(r"\d+", value)]
    return tuple(numbers) if numbers else (0,)


def is_newer(latest: str, current: str = __version__) -> bool:
    left = parse_version(latest)
    right = parse_version(current)
    length = max(len(left), len(right))
    left += (0,) * (length - len(left))
    right += (0,) * (length - len(right))
    return left > right


def select_asset(assets: list[dict[str, Any]], platform: str | None = None) -> dict[str, Any] | None:
    platform = platform or sys.platform
    for asset in assets:
        name = str(asset.get("name", ""))
        low = name.lower()
        if "cli" in low:
            continue
        if platform == "win32":
            if "windows" in low and low.endswith(".exe"):
                return asset
        elif platform == "darwin":
            if "macos" in low and low.endswith(".zip"):
                return asset
        else:
            if "ubuntu" in low or "linux" in low:
                return asset
    return None


def select_checksum_asset(assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    for asset in assets:
        if str(asset.get("name", "")).lower().startswith("sha256sums"):
            return asset
    return None


def _validate_release_url(url: str, *, checksum: bool = False) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise UpdateError("Ungültige GitHub-Release-URL.") from exc
    expected_prefix = f"/{GITHUB_REPO}/releases/download/"
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith(expected_prefix)
    ):
        label = "Prüfsummen" if checksum else "Update"
        raise UpdateError(f"{label}-URL ist kein gültiger tarklone-GitHub-Release-Link.")
    return url


def _validate_asset_name(name: str) -> str:
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or Path(name).name != name
    ):
        raise UpdateError("Release enthält einen ungültigen Dateinamen.")
    return name


def check_for_update(
    current_version: str | None = None,
    *,
    timeout: float = 20.0,
) -> UpdateInfo | None:
    current = current_version or __version__
    try:
        response = httpx.get(
            LATEST_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"tarklone/{__version__}",
            },
            timeout=timeout,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise UpdateError(f"GitHub ist nicht erreichbar: {exc}") from exc
    if urlsplit(str(response.url)).hostname != "api.github.com":
        raise UpdateError("GitHub-API hat auf einen nicht erlaubten Host umgeleitet.")
    if response.status_code == 404:
        raise UpdateError(
            "Kein öffentliches Release gefunden. Ist das Repository öffentlich "
            "und ein Release veröffentlicht?"
        )
    if response.status_code == 403:
        raise UpdateError("GitHub-API-Limit erreicht. Bitte später erneut versuchen.")
    if response.status_code >= 400:
        raise UpdateError(f"GitHub-API-Fehler: HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise UpdateError("GitHub lieferte keine gültige Release-Antwort.") from exc
    tag = str(data.get("tag_name", "")).strip()
    version = tag.lstrip("vV")
    if not version or not is_newer(version, current):
        return None
    assets = data.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("Release enthält keine Assets.")
    asset = select_asset(assets)
    if asset is None:
        raise UpdateError(
            f"Für diese Plattform ({sys.platform}) gibt es kein passendes Update-Asset."
        )
    checksum = select_checksum_asset(assets)
    if checksum is None:
        raise UpdateError("Release enthält keine SHA-256-Prüfsummendatei.")
    asset_name = _validate_asset_name(str(asset.get("name") or ""))
    checksum_name = _validate_asset_name(str(checksum.get("name") or ""))
    if not checksum_name.lower().startswith("sha256sums"):
        raise UpdateError("Ungültiger Name der Prüfsummendatei.")
    asset_url = _validate_release_url(str(asset.get("browser_download_url") or ""))
    checksum_url = _validate_release_url(
        str(checksum.get("browser_download_url") or ""), checksum=True
    )
    return UpdateInfo(
        version=version,
        tag=tag,
        name=str(data.get("name") or tag),
        html_url=str(data.get("html_url") or ""),
        asset_name=asset_name,
        asset_url=asset_url,
        checksum_url=checksum_url,
        notes=str(data.get("body") or ""),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _checksum_for(checksum_text: str, asset_name: str) -> str:
    for line in checksum_text.splitlines():
        parts = line.strip().split()
        if (
            len(parts) >= 2
            and parts[-1].lstrip("*") == asset_name
            and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0])
        ):
            return parts[0].lower()
    return ""


def default_download_dir() -> Path:
    downloads = Path.home() / "Downloads"
    if downloads.is_dir():
        return downloads
    return Path(tempfile.gettempdir())


def download_update(info: UpdateInfo, destination_dir: Path | None = None) -> Path:
    _validate_asset_name(info.asset_name)
    _validate_release_url(info.asset_url)
    _validate_release_url(info.checksum_url, checksum=True)
    target_dir = Path(destination_dir) if destination_dir else default_download_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    destination = target_dir / info.asset_name
    fd, temp_name = tempfile.mkstemp(
        prefix=".tarklone-update-", suffix=".part", dir=target_dir
    )
    temp = Path(temp_name)
    try:
        try:
            checksum_response = httpx.get(
                info.checksum_url,
                headers={"User-Agent": f"tarklone/{__version__}"},
                timeout=30.0,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise UpdateError(f"Prüfsummendatei konnte nicht geladen werden: {exc}") from exc
        if checksum_response.status_code != 200:
            raise UpdateError(
                f"Prüfsummendownload fehlgeschlagen: HTTP {checksum_response.status_code}"
            )
        if urlsplit(str(checksum_response.url)).hostname not in _ALLOWED_RELEASE_HOSTS:
            raise UpdateError("Prüfsummendownload wurde auf einen nicht erlaubten Host umgeleitet.")
        expected = _checksum_for(checksum_response.text, info.asset_name)
        if not expected:
            raise UpdateError("Prüfsummendatei enthält keinen gültigen Hash für das Asset.")
        with httpx.stream(
            "GET",
            info.asset_url,
            headers={"User-Agent": f"tarklone/{__version__}"},
            timeout=180.0,
            follow_redirects=True,
        ) as response:
            if response.status_code >= 400:
                raise UpdateError(
                    f"Download fehlgeschlagen: HTTP {response.status_code}"
                )
            if urlsplit(str(response.url)).hostname not in _ALLOWED_RELEASE_HOSTS:
                raise UpdateError("Update wurde auf einen nicht erlaubten Host umgeleitet.")
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                for chunk in response.iter_bytes(1024 * 1024):
                    handle.write(chunk)
        actual = _sha256(temp)
        if not hmac.compare_digest(actual, expected):
            raise UpdateError("SHA-256-Prüfsumme des Updates stimmt nicht.")
        os.replace(temp, destination)
        return destination
    finally:
        if fd >= 0:
            os.close(fd)
        temp.unlink(missing_ok=True)
