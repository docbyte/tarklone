from __future__ import annotations

from pathlib import Path

import pytest

from tarklone.config import Settings
from tarklone.sync import RemoteEntry
from tarklone.tarkle import TarkleRemote


class FakeApi:
    def __init__(self) -> None:
        self.created: list[tuple[str, str | None]] = []
        self.uploads: list[dict] = []
        self.deleted: list[str] = []
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def list_tree(self, workspace_key: str, path: str):
        assert workspace_key == "abcd1234"
        assert path == "Team"
        return (
            [
                {
                    "id": "sub-uuid",
                    "type": "folder",
                    "name": "Sub",
                    "parent_id": "root-uuid",
                    "rel_path": "Sub",
                    "absolute_path": "Team/Sub",
                },
                {
                    "id": "1",
                    "type": "file",
                    "name": "a.txt",
                    "size": 5,
                    "parent_id": "sub-uuid",
                    "sha256": "abc",
                    "modified_at": "2026-11-18T10:00:00Z",
                    "rel_path": "Sub/a.txt",
                    "absolute_path": "Team/Sub/a.txt",
                },
            ],
            "Team",
            "root-uuid",
        )

    def create_folder(self, workspace_key: str, name: str, parent_id=None):
        self.created.append((name, parent_id))
        return {
            "id": f"{name}-uuid",
            "type": "folder",
            "name": name,
            "parent_id": parent_id,
            "path": f"/Team/{name}",
        }

    def upload_file(
        self,
        workspace_key: str,
        local_path: Path,
        path: str,
        *,
        folder_id=None,
        modified_at=None,
        existing_entry_id=None,
    ):
        self.uploads.append(
            {
                "path": path,
                "folder_id": folder_id,
                "existing": existing_entry_id,
                "size": local_path.stat().st_size,
            }
        )
        return {
            "id": "2",
            "type": "file",
            "name": Path(path).name,
            "size": local_path.stat().st_size,
            "parent_id": folder_id,
            "sha256": "def",
        }

    def delete_file(self, workspace_key: str, entry_id: str) -> None:
        self.deleted.append(entry_id)


def make_remote(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[TarkleRemote, FakeApi]:
    fake = FakeApi()
    monkeypatch.setattr("tarklone.tarkle.TarkleApi", lambda _key: fake)
    settings = Settings(
        workspace_id="abcd1234",
        workspace_name="Acme",
        remote_folder_id="Team",
    )
    return TarkleRemote(settings, "tk_test_1234567890abcdef"), fake


def test_adapter_maps_tree_and_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    remote, _fake = make_remote(monkeypatch, tmp_path)
    entries = {entry.path: entry for entry in remote.list_tree()}
    assert remote.target_path == "Team"
    assert entries["Sub"].kind == "dir"
    assert entries["Sub"].entry_id == "sub-uuid"
    assert entries["Sub/a.txt"].kind == "file"
    assert entries["Sub/a.txt"].content_hash == "abc"
    assert entries["Sub/a.txt"].mtime > 0


def test_adapter_upload_uses_existing_parent_and_full_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    remote, fake = make_remote(monkeypatch, tmp_path)
    remote.list_tree()
    local = tmp_path / "new.txt"
    local.write_bytes(b"hello")
    entry = remote.upload(
        local,
        "Sub/new.txt",
        mtime=1_700_000_000,
        remote_entry=RemoteEntry(
            path="Sub/new.txt",
            kind="file",
            entry_id="1",
            parent_id="sub-uuid",
        ),
    )
    assert fake.uploads == [
        {
            "path": "Team/Sub/new.txt",
            "folder_id": "sub-uuid",
            "existing": "1",
            "size": 5,
        }
    ]
    assert entry.entry_id == "2"
    assert entry.parent_id == "sub-uuid"


def test_adapter_creates_nested_folders_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    remote, fake = make_remote(monkeypatch, tmp_path)
    remote.list_tree()
    entry = remote.create_folder("A/B")
    assert fake.created == [("A", "root-uuid"), ("B", "A-uuid")]
    assert entry.entry_id == "B-uuid"
    assert entry.parent_id == "A-uuid"
    remote.create_folder("A/B")
    assert len(fake.created) == 2


def test_adapter_delete_uses_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    remote, fake = make_remote(monkeypatch, tmp_path)
    remote.delete(RemoteEntry(path="a.txt", kind="file", entry_id="1"))
    assert fake.deleted == ["1"]
