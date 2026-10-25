from __future__ import annotations

import json

import pytest

from tarklone.webapi import (
    TarkleWeb,
    _file_array,
    _format_size,
    _item_type,
    _parse_time,
    _rsc_text,
    _storage_usage_from_text,
    _workspace_array,
    WebApiError,
)


def rsc_html(payload: dict) -> str:
    encoded = json.dumps(json.dumps(payload, ensure_ascii=False))
    return f'<script>self.__next_f.push([1,{encoded}])</script>'


def test_rsc_text_and_files() -> None:
    payload = {
        "files": [
            {
                "id": 1,
                "name": "a.txt",
                "type": "document",
                "file_id": "00000000-0000-0000-0000-000000000001",
                "size": "5",
            }
        ]
    }
    html = rsc_html(payload)
    assert '"files"' in _rsc_text(html)
    files = _file_array(html)
    assert len(files) == 1
    assert files[0]["name"] == "a.txt"


def test_rsc_workspaces() -> None:
    payload = {
        "workspaces": [
            {"id": "ws-1", "name": "Workspace", "workspace_key": "abc123"}
        ]
    }
    workspaces = _workspace_array(rsc_html(payload))
    assert workspaces == [
        {"id": "ws-1", "name": "Workspace", "workspace_key": "abc123"}
    ]


def test_parse_time_formats() -> None:
    assert _parse_time("10/12/2026 6:08 AM") > 0
    assert _parse_time("2026-10-12T06:08:39.398194+00:00") > 0
    assert _parse_time("") == 0
    assert _parse_time("not a date") == 0


def test_format_size() -> None:
    assert _format_size(0) == "0b"
    assert _format_size(1024) == "1.00kb"
    assert _format_size(24583) == "24.01kb"


def test_parse_storage_usage() -> None:
    text = (
        '"storageLeft":4988831599,"generalWorkspaceUsedBytes":11168401,'
        '"workspaceUsedBytes":11168401,"maxStorage":5000000000'
    )
    usage = _storage_usage_from_text(text)
    assert usage["used"] == 11168401
    assert usage["limit"] == 5000000000
    assert usage["left"] == 4988831599
    assert usage["percent"] == 0


def test_download_range_streams_only_requested_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        status_code = 200

        def iter_bytes(self):
            yield b"ab"
            yield b"cde"
            yield b"fghijkl"

    class FakeStream:
        def __enter__(self):
            return FakeResponse()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("tarklone.webapi.httpx.stream", lambda *a, **k: FakeStream())
    web = object.__new__(TarkleWeb)
    web.download_url = lambda *_args: "https://storage.example/signed"  # type: ignore[method-assign]
    assert web.download_range({}, "ws", 2, 4) == b"cdef"


def test_download_range_rejects_unbounded_requests() -> None:
    web = object.__new__(TarkleWeb)
    with pytest.raises(WebApiError, match="Download-Bereich"):
        web.download_range({}, "ws", 0, 2 * 1024 * 1024)


def test_item_type() -> None:
    assert _item_type("bild.png") == "photo"
    assert _item_type("video.mp4") == "video"
    assert _item_type("ton.mp3") == "audio"
    assert _item_type("text.pdf") == "document"
    assert _item_type("code.ts") == "document"
