from __future__ import annotations

import pytest

from tarklone.updates import (
    UpdateError,
    _checksum_for,
    check_for_update,
    is_newer,
    parse_version,
    select_asset,
    select_checksum_asset,
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
    text = "abc123  tarklone-1.1-windows-x64.exe\n"
    assert _checksum_for(text, "tarklone-1.1-windows-x64.exe") == "abc123"
    assert _checksum_for(text, "other.exe") == ""


class FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

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
                "browser_download_url": "https://example.invalid/app.exe",
            },
            {
                "name": "SHA256SUMS-1.1.txt",
                "browser_download_url": "https://example.invalid/sums.txt",
            },
        ],
    }
    monkeypatch.setattr(
        "tarklone.updates.httpx.get",
        lambda *args, **kwargs: FakeResponse(200, payload),
    )
    info = check_for_update("1.0")
    assert info is not None
    assert info.version == "1.1"
    assert info.asset_name == "tarklone-1.1-windows-x64.exe"
    assert info.checksum_url.endswith("sums.txt") or info.checksum_url.endswith("sums.txt")


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
