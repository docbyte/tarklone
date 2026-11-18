from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from tarklone.api import TarkleApi, TarkleApiError, TarkleAuthError, _iso_mtime

KEY = "tk_test_1234567890abcdef"


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> TarkleApi:
    return TarkleApi(KEY, http=httpx.Client(transport=httpx.MockTransport(handler)))


def problem(status: int, code: str, detail: str, **details: Any) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "type": f"https://tarkle.tech/docs/api/errors/{code}",
            "title": code,
            "status": status,
            "detail": detail,
            "code": code,
            "details": details,
        },
        headers={"Content-Type": "application/problem+json"},
    )


def test_list_workspaces_and_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        if request.url.path == "/api/v1/workspaces":
            return httpx.Response(
                200,
                json={
                    "workspaces": [
                        {
                            "workspace_key": "abcd1234",
                            "name": "Acme",
                            "url": "acme",
                            "relationship": "owner",
                        }
                    ]
                },
            )
        if request.url.path == "/api/v1/usage":
            assert request.headers["X-Workspace-Key"] == "abcd1234"
            return httpx.Response(
                200,
                json={
                    "storage": {
                        "used_bytes": 50,
                        "limit_bytes": 200,
                        "free_bytes": 150,
                    }
                },
            )
        return httpx.Response(404)

    api = make_client(handler)
    try:
        workspaces = api.list_workspaces()
        assert workspaces == [
            {
                "id": "abcd1234",
                "workspace_key": "abcd1234",
                "name": "Acme",
                "url": "acme",
                "role": "owner",
            }
        ]
        assert api.usage("abcd1234") == {
            "used": 50,
            "limit": 200,
            "left": 150,
            "percent": 25,
        }
    finally:
        api.close()


def test_list_tree_with_path_and_pagination() -> None:
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        calls.append(params)
        if request.url.path == "/api/v1/files/by-path":
            return httpx.Response(
                200,
                json={
                    "id": "folder-uuid",
                    "type": "folder",
                    "name": "Projekt",
                    "path": "/Kunden/Projekt",
                },
            )
        if request.url.path == "/api/v1/files":
            assert params["recursive"] == "true"
            if params["type"] == "folder":
                return httpx.Response(
                    200,
                    json={
                        "entries": [
                            {
                                "id": "sub-uuid",
                                "type": "folder",
                                "name": "Sub",
                                "path": "/Kunden/Projekt/Sub",
                                "parent_id": "folder-uuid",
                            }
                        ],
                        "next_cursor": None,
                    },
                )
            if not params.get("cursor"):
                return httpx.Response(
                    200,
                    json={
                        "entries": [
                            {
                                "id": "1",
                                "type": "file",
                                "name": "a.txt",
                                "size": 3,
                                "path": "/Kunden/Projekt/a.txt",
                                "parent_id": "folder-uuid",
                                "sha256": "abc",
                            }
                        ],
                        "next_cursor": "next",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "entries": [
                        {
                            "id": "2",
                            "type": "file",
                            "name": "b.txt",
                            "size": 5,
                            "path": "/Kunden/Projekt/Sub/b.txt",
                            "parent_id": "sub-uuid",
                            "sha256": "def",
                        }
                    ],
                    "next_cursor": None,
                },
            )
        return httpx.Response(404)

    api = make_client(handler)
    try:
        entries, root_path, root_id = api.list_tree("abcd1234", "Kunden/Projekt")
    finally:
        api.close()
    assert root_path == "Kunden/Projekt"
    assert root_id == "folder-uuid"
    assert [(entry["rel_path"], entry["type"]) for entry in entries] == [
        ("Sub", "folder"),
        ("a.txt", "file"),
        ("Sub/b.txt", "file"),
    ]
    assert any(call.get("cursor") == "next" for call in calls)


def test_upload_single_uses_version_conflict_and_checksum(tmp_path: Path) -> None:
    local = tmp_path / "a.txt"
    local.write_bytes(b"hello")
    digest = hashlib.sha256(b"hello").hexdigest()
    put_body: list[bytes] = []
    start_bodies: list[dict[str, Any]] = []
    complete_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/uploads" and request.method == "POST":
            body = json.loads(request.content)
            start_bodies.append(body)
            assert request.headers["X-Workspace-Key"] == "abcd1234"
            assert request.headers["Idempotency-Key"]
            return httpx.Response(
                201,
                json={
                    "action": "upload",
                    "id": "upload-uuid",
                    "status": "pending",
                    "name": "a.txt",
                    "size": 5,
                    "folder_id": "folder-uuid",
                    "mode": "single",
                    "part_count": 1,
                    "part_size": 5,
                    "will_be_renamed": False,
                    "content_type": "text/plain",
                    "uploads": [
                        {"partNumber": 1, "url": "https://storage.test/one"}
                    ],
                },
            )
        if request.url.host == "storage.test":
            put_body.append(request.content)
            return httpx.Response(200)
        if request.url.path == "/api/v1/uploads/upload-uuid/complete":
            complete_bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "id": "7",
                    "type": "file",
                    "name": "a.txt",
                    "size": 5,
                    "parent_id": "folder-uuid",
                    "sha256": digest,
                    "path": "/Docs/a.txt",
                },
            )
        return httpx.Response(404)

    api = make_client(handler)
    try:
        entry = api.upload_file(
            "abcd1234",
            local,
            "Docs/a.txt",
            folder_id="folder-uuid",
            modified_at=1_700_000_000,
            existing_entry_id="7",
        )
    finally:
        api.close()
    assert entry["id"] == "7"
    assert put_body == [b"hello"]
    assert start_bodies[0]["on_conflict"] == "version"
    assert start_bodies[0]["sha256"] == digest
    assert start_bodies[0]["folder_id"] == "folder-uuid"
    assert start_bodies[0]["modified_at"].endswith("Z")
    assert complete_bodies[0]["sha256"] == digest


def test_upload_falls_back_to_separate_when_version_not_offered(
    tmp_path: Path,
) -> None:
    local = tmp_path / "a.txt"
    local.write_bytes(b"hello")
    deleted = False
    start_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal deleted, start_count
        if request.url.path == "/api/v1/uploads":
            start_count += 1
            body = json.loads(request.content)
            if start_count == 1:
                assert body["on_conflict"] == "version"
                return problem(
                    409,
                    "conflict",
                    "Name exists",
                    choices=["separate", "skip"],
                )
            assert body["on_conflict"] == "separate"
            return httpx.Response(
                201,
                json={
                    "action": "upload",
                    "id": "upload-uuid",
                    "status": "pending",
                    "name": "a.txt",
                    "size": 5,
                    "mode": "single",
                    "part_count": 1,
                    "part_size": 5,
                    "will_be_renamed": False,
                    "content_type": "text/plain",
                    "uploads": [
                        {"partNumber": 1, "url": "https://storage.test/one"}
                    ],
                },
            )
        if (
            request.url.path == "/api/v1/files/7"
            and request.method == "DELETE"
        ):
            deleted = True
            return httpx.Response(200, json={"deleted": True})
        if request.url.host == "storage.test":
            return httpx.Response(200)
        if request.url.path == "/api/v1/uploads/upload-uuid/complete":
            return httpx.Response(
                200,
                json={"id": "8", "type": "file", "name": "a.txt", "size": 5},
            )
        return httpx.Response(404)

    api = make_client(handler)
    try:
        entry = api.upload_file(
            "abcd1234",
            local,
            "a.txt",
            existing_entry_id="7",
        )
    finally:
        api.close()
    assert deleted is True
    assert start_count == 2
    assert entry["id"] == "8"


def test_multipart_upload_signs_missing_parts(tmp_path: Path) -> None:
    local = tmp_path / "big.bin"
    local.write_bytes(b"abcdefgh")
    parts: dict[int, bytes] = {}
    part_calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/uploads":
            return httpx.Response(
                201,
                json={
                    "action": "upload",
                    "id": "upload-uuid",
                    "status": "pending",
                    "name": "big.bin",
                    "size": 8,
                    "mode": "multipart",
                    "part_count": 2,
                    "part_size": 4,
                    "will_be_renamed": False,
                    "content_type": "application/octet-stream",
                    "uploads": [
                        {"partNumber": 1, "url": "https://storage.test/1"}
                    ],
                },
            )
        if request.url.path == "/api/v1/uploads/upload-uuid/parts":
            body = json.loads(request.content)
            part_calls.append(body)
            return httpx.Response(
                200,
                json={
                    "uploads": [
                        {"partNumber": 2, "url": "https://storage.test/2"}
                    ]
                },
            )
        if request.url.host == "storage.test":
            number = int(str(request.url).rsplit("/", 1)[-1])
            parts[number] = request.content
            return httpx.Response(200)
        if request.url.path == "/api/v1/uploads/upload-uuid/complete":
            return httpx.Response(
                200,
                json={"id": "9", "type": "file", "name": "big.bin", "size": 8},
            )
        return httpx.Response(404)

    api = make_client(handler)
    try:
        entry = api.upload_file("abcd1234", local, "big.bin")
    finally:
        api.close()
    assert part_calls == [{"from_part": 2, "count": 1}]
    assert parts == {1: b"abcd", 2: b"efgh"}
    assert entry["id"] == "9"


def test_download_to_uses_signed_url(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/files/5/download":
            assert request.headers["X-Workspace-Key"] == "abcd1234"
            return httpx.Response(
                200,
                json={
                    "id": "5",
                    "name": "a.txt",
                    "size": 5,
                    "download_url": "https://storage.test/file",
                    "expires_in": 900,
                    "links_expire": True,
                },
            )
        if request.url.host == "storage.test":
            return httpx.Response(200, content=b"hello")
        return httpx.Response(404)

    destination = tmp_path / "out.txt"
    api = make_client(handler)
    try:
        api.download_to("abcd1234", {"id": "5", "size": 5}, destination)
    finally:
        api.close()
    assert destination.read_bytes() == b"hello"


def test_unauthorized_raises_auth_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return problem(401, "unauthorized", "Key invalid")

    api = make_client(handler)
    try:
        with pytest.raises(TarkleAuthError):
            api.list_workspaces()
    finally:
        api.close()


def test_api_error_carries_code_and_details() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return problem(403, "no_access", "No access", kind="private")

    api = make_client(handler)
    try:
        with pytest.raises(TarkleApiError) as excinfo:
            api.usage("abcd1234")
    finally:
        api.close()
    assert excinfo.value.status == 403
    assert excinfo.value.code == "no_access"
    assert excinfo.value.details == {"kind": "private"}


def test_follows_safe_api_redirect() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "tarkle.tech":
            return httpx.Response(
                307,
                headers={
                    "Location": "https://tarkle.com/api/v1/workspaces",
                },
            )
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        return httpx.Response(200, json={"workspaces": []})

    api = make_client(handler)
    try:
        assert api.list_workspaces() == []
    finally:
        api.close()
    assert hosts == ["tarkle.tech", "tarkle.com"]


def test_rejects_unsafe_api_redirect() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            307,
            headers={"Location": "https://attacker.example/api/v1/workspaces"},
        )

    api = make_client(handler)
    try:
        with pytest.raises(TarkleApiError, match="nicht erlaubtes Ziel"):
            api.list_workspaces()
    finally:
        api.close()


def test_iso_mtime_rejects_pre_1970_and_clamps_future() -> None:
    assert _iso_mtime(None) is None
    assert _iso_mtime(10) is None
    value = _iso_mtime(1_700_000_000)
    assert value is not None and value.endswith("Z")
