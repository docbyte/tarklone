from __future__ import annotations

import json

from tarklone.webapi import (
    _file_array,
    _format_size,
    _item_type,
    _parse_time,
    _rsc_text,
    _workspace_array,
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


def test_item_type() -> None:
    assert _item_type("bild.png") == "photo"
    assert _item_type("video.mp4") == "video"
    assert _item_type("ton.mp3") == "audio"
    assert _item_type("text.pdf") == "document"
    assert _item_type("code.ts") == "document"
