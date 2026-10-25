from __future__ import annotations

from contextlib import nullcontext

import pytest

from tarklone.updates import (
    UpdateInfo,
    UpdateError,
    _checksum_for,
    check_for_update,
    is_newer,
    parse_version,
    select_asset,
    select_checksum_asset,
    download_update,
)


def test_parse_and_compare_versions() -> None:
    assert parse_version("v1.2.3") == (1, 2, 3)
    assert is_newer("1.2", "1.1")
    assert is_newer("v1.1.1", "1.1")
    assert not is_newer("1.1", "1.1")
    assert not is_newer("1.0.9", "1.1")


def test_select_asset_per_platform() -> None:
    assets = [
        {"name": "tarklone-1.1-windows-x64.exe", "browser_download_url": "w"},
        {"name": "tarklone-1.1-ubuntu-x86_64", "browser_download_url": "l"},
        {"name": "tarklone-1.1-macos-arm64.zip", "browser_download_url": "m"},
        {"name": "tarklone-cli-1.1-windows-x64.exe", "browser_download_url": "c"},
        {"name": "SHA256SUMS-1.1.txt", "browser_download_url": "s"},
    ]
    assert select_asset(assets, "win32")["browser_download_url"] == "w"
    assert select_asset(assets, "linux")["browser_download_url"] == "l"
    assert select_asset(assets, "darwin")["browser_download_url"] == "m"
    assert select_checksum_asset(assets)["browser_download_url"] == "s"


def test_checksum_parser() -> None:
    digest = "a" * 64
    text = f"{digest}  tarklone-1.1-windows-x64.exe\n"
    assert _checksum_for(text, "tarklone-1.1-windows-x64.exe") == digest
    assert _checksum_for("abc123  app.exe", "app.exe") == ""
    assert _checksum_for(text, "other.exe") == ""


class FakeResponse:
    def __init__(self, status_code: int, payload=None, *, text: str = "", url: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.url = url or "https://api.github.com/repos/docbyte/tarklone/releases/latest"

    def json(self):
        return self._payload


def test_check_for_update(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "tag_name": "v1.1",
        "name": "tarklone 1.1",
        "html_url": "https://example.invalid/release",
        "body": "notes",
        "assets": [
            {
                "name": "tarklone-1.1-windows-x64.exe",
                "browser_download_url": (
                    "https://github.com/docbyte/tarklone/releases/download/v1.1/"
                    "tarklone-1.1-windows-x64.exe"
                ),
            },
            {
                "name": "SHA256SUMS-1.1.txt",
                "browser_download_url": (
                    "https://github.com/docbyte/tarklone/releases/download/v1.1/"
                    "SHA256SUMS-1.1.txt"
                ),
            },
        ],
    }
    monkeypatch.setattr(
        "tarklone.updates.httpx.get",
        lambda *args, **kwargs: FakeResponse(200, payload),
    )
    info = check_for_update("1.0", platform="win32")
    assert info is not None
    assert info.version == "1.1"
    assert info.asset_name == "tarklone-1.1-windows-x64.exe"
    assert info.checksum_url.endswith("SHA256SUMS-1.1.txt")


def test_check_for_update_no_new_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tarklone.updates.httpx.get",
        lambda *args, **kwargs: FakeResponse(200, {"tag_name": "v1.1"}),
    )
    assert check_for_update("1.1") is None


def test_check_for_update_private_or_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tarklone.updates.httpx.get",
        lambda *args, **kwargs: FakeResponse(404),
    )
    with pytest.raises(UpdateError):
        check_for_update("1.0")


def test_check_for_update_requires_checksum_asset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tarklone.updates.httpx.get",
        lambda *args, **kwargs: FakeResponse(
            200,
            {
                "tag_name": "v1.2",
                "assets": [
                    {
                        "name": "tarklone-1.2-windows-x64.exe",
                        "browser_download_url": (
                            "https://github.com/docbyte/tarklone/releases/download/"
                            "v1.2/tarklone-1.2-windows-x64.exe"
                        ),
                    }
                ],
            },
        ),
    )
    with pytest.raises(UpdateError, match="Prüfsummendatei"):
        check_for_update("1.0", platform="win32")


def test_update_download_fails_closed_on_bad_checksum(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    asset_name = "tarklone-1.2-windows-x64.exe"
    url = (
        "https://github.com/docbyte/tarklone/releases/download/"
        f"v1.2/{asset_name}"
    )
    checksum_url = (
        "https://github.com/docbyte/tarklone/releases/download/v1.2/SHA256SUMS-1.2.txt"
    )

    class AssetResponse:
        status_code = 200
        url = "https://release-assets.githubusercontent.com/download/asset"

        @staticmethod
        def iter_bytes(_chunk_size):
            yield b"malicious or corrupted asset"

    monkeypatch.setattr(
        "tarklone.updates.httpx.get",
        lambda *args, **kwargs: FakeResponse(
            200,
            text=f"{'0' * 64}  {asset_name}\n",
            url=checksum_url,
        ),
    )
    monkeypatch.setattr(
        "tarklone.updates.httpx.stream",
        lambda *args, **kwargs: nullcontext(AssetResponse()),
    )
    info = UpdateInfo(
        version="1.2",
        tag="v1.2",
        name="tarklone 1.2",
        html_url="https://github.com/docbyte/tarklone/releases/tag/v1.2",
        asset_name=asset_name,
        asset_url=url,
        checksum_url=checksum_url,
    )
    with pytest.raises(UpdateError, match="Prüfsumme"):
        download_update(info, tmp_path)
    assert not (tmp_path / asset_name).exists()
