from __future__ import annotations

import time
from pathlib import Path

import pytest

from tarklone.state import SyncState
from tarklone.sync import (
    LocalScanner,
    RemoteEntry,
    SyncEngine,
    SyncError,
    normalize_rel,
)


class MemoryRemote:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = set()
        self.mtimes: dict[str, float] = {}
        self.ids: dict[str, str] = {}
        self._next_id = 1

    def _id(self, path: str) -> str:
        if path not in self.ids:
            self.ids[path] = f"r{self._next_id}"
            self._next_id += 1
        return self.ids[path]

    def list_tree(self) -> list[RemoteEntry]:
        entries = [RemoteEntry(path=path, kind="dir", entry_id=self._id(path)) for path in self.dirs]
        entries.extend(
            RemoteEntry(
                path=path,
                kind="file",
                size=len(data),
                mtime=self.mtimes.get(path, 0.0),
                entry_id=self._id(path),
            )
            for path, data in self.files.items()
        )
        return entries

    def download(self, entry: RemoteEntry, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.files[entry.path])

    def upload(
        self,
        local_path: Path,
        remote_path: str,
        *,
        mtime: float | None = None,
        remote_entry: RemoteEntry | None = None,
    ) -> RemoteEntry:
        data = local_path.read_bytes()
        self.files[remote_path] = data
        self.mtimes[remote_path] = mtime or time.time()
        parent = remote_path.rsplit("/", 1)[0] if "/" in remote_path else ""
        while parent:
            self.dirs.add(parent)
            parent = parent.rsplit("/", 1)[0] if "/" in parent else ""
        return RemoteEntry(
            path=remote_path,
            kind="file",
            size=len(data),
            mtime=self.mtimes[remote_path],
            entry_id=self._id(remote_path),
        )

    def delete(self, entry: RemoteEntry) -> None:
        if entry.kind == "dir":
            self.dirs.discard(entry.path)
            return
        self.files.pop(entry.path, None)
        self.mtimes.pop(entry.path, None)
        self.ids.pop(entry.path, None)

    def create_folder(self, remote_path: str) -> RemoteEntry:
        self.dirs.add(remote_path)
        return RemoteEntry(path=remote_path, kind="dir", entry_id=self._id(remote_path))


def make_engine(tmp_path: Path, remote: MemoryRemote, **kwargs) -> tuple[SyncEngine, SyncState]:
    local = tmp_path / "local"
    local.mkdir()
    state = SyncState(tmp_path / "state.sqlite3")
    engine = SyncEngine(local, remote, state, **kwargs)
    return engine, state


def set_mtime(path: Path, seconds_ahead: int = 5) -> None:
    future = time.time() + seconds_ahead
    stat = path.stat()
    path.write_bytes(path.read_bytes())
    time.sleep(0.01)
    import os

    os.utime(path, (future, future))
    assert path.stat().st_mtime_ns != stat.st_mtime_ns


def test_normalize_rel_rejects_traversal() -> None:
    assert normalize_rel("a/b.txt") == "a/b.txt"
    with pytest.raises(SyncError):
        normalize_rel("../evil.txt")


def test_sync_reports_progress(tmp_path: Path) -> None:
    remote = MemoryRemote()
    events = []
    engine, state = make_engine(tmp_path, remote, on_progress=events.append)
    try:
        (engine.local_root / "hello.txt").write_text("hello", encoding="utf-8")
        result = engine.sync()
    finally:
        state.close()

    assert result.errors == []
    assert events
    assert events[0].current == 0
    assert events[-1].current == events[-1].total
    assert events[-1].percent == 100
    assert events[-1].eta_seconds == 0


def test_initial_upload(tmp_path: Path) -> None:
    remote = MemoryRemote()
    engine, state = make_engine(tmp_path, remote)
    (engine.local_root / "hello.txt").write_text("hello", encoding="utf-8")
    (engine.local_root / "sub").mkdir()
    (engine.local_root / "sub" / "world.txt").write_text("world", encoding="utf-8")

    result = engine.sync()

    assert result.errors == []
    assert remote.files["hello.txt"] == b"hello"
    assert remote.files["sub/world.txt"] == b"world"
    assert state.get("hello.txt") is not None
    assert state.get("sub/world.txt") is not None


def test_initial_download(tmp_path: Path) -> None:
    remote = MemoryRemote()
    remote.files["cloud.txt"] = b"cloud"
    remote.mtimes["cloud.txt"] = time.time()
    engine, state = make_engine(tmp_path, remote)

    result = engine.sync()

    assert result.errors == []
    assert (engine.local_root / "cloud.txt").read_bytes() == b"cloud"
    assert state.get("cloud.txt") is not None


def test_local_change_uploads(tmp_path: Path) -> None:
    remote = MemoryRemote()
    engine, _ = make_engine(tmp_path, remote)
    local_file = engine.local_root / "note.txt"
    local_file.write_text("one", encoding="utf-8")
    engine.sync()

    local_file.write_text("two", encoding="utf-8")
    set_mtime(local_file)
    result = engine.sync()

    assert result.uploaded == 1
    assert remote.files["note.txt"] == b"two"


def test_remote_change_downloads(tmp_path: Path) -> None:
    remote = MemoryRemote()
    remote.files["note.txt"] = b"one"
    remote.mtimes["note.txt"] = time.time()
    engine, _ = make_engine(tmp_path, remote)
    engine.sync()

    remote.files["note.txt"] = b"two"
    remote.mtimes["note.txt"] = time.time() + 5
    result = engine.sync()

    assert result.downloaded == 1
    assert (engine.local_root / "note.txt").read_bytes() == b"two"


def test_conflict_creates_copy(tmp_path: Path) -> None:
    remote = MemoryRemote()
    engine, _ = make_engine(tmp_path, remote)
    local_file = engine.local_root / "note.txt"
    local_file.write_text("base", encoding="utf-8")
    engine.sync()

    local_file.write_text("local change", encoding="utf-8")
    set_mtime(local_file)
    remote.files["note.txt"] = b"remote change"
    remote.mtimes["note.txt"] = time.time() + 5
    result = engine.sync()

    assert result.conflicts == 1
    assert local_file.read_bytes() == b"remote change"
    conflicts = list(engine.local_root.glob("note (Konflikt *).txt"))
    assert len(conflicts) == 1
    assert conflicts[0].read_bytes() == b"local change"
    assert any("Konflikt" in path for path in remote.files)


def test_deletion_propagates_when_enabled(tmp_path: Path) -> None:
    remote = MemoryRemote()
    engine, state = make_engine(tmp_path, remote, sync_deletions=True)
    local_file = engine.local_root / "gone.txt"
    local_file.write_text("bye", encoding="utf-8")
    engine.sync()

    local_file.unlink()
    result = engine.sync()

    assert result.deleted_remote == 1
    assert "gone.txt" not in remote.files
    assert state.get("gone.txt") is None


def test_deletion_restores_when_disabled(tmp_path: Path) -> None:
    remote = MemoryRemote()
    engine, _ = make_engine(tmp_path, remote, sync_deletions=False)
    local_file = engine.local_root / "keep.txt"
    local_file.write_text("keep", encoding="utf-8")
    engine.sync()

    local_file.unlink()
    result = engine.sync()

    assert result.downloaded == 1
    assert local_file.read_bytes() == b"keep"


def test_remote_deletion_restores_remote(tmp_path: Path) -> None:
    remote = MemoryRemote()
    engine, _ = make_engine(tmp_path, remote, sync_deletions=False)
    local_file = engine.local_root / "keep.txt"
    local_file.write_text("keep", encoding="utf-8")
    engine.sync()

    remote.files.pop("keep.txt")
    remote.mtimes.pop("keep.txt", None)
    result = engine.sync()

    assert result.uploaded == 1
    assert remote.files["keep.txt"] == b"keep"
    assert local_file.read_bytes() == b"keep"


def test_scanner_excludes_tarklone_and_patterns(tmp_path: Path) -> None:
    local = tmp_path / "local"
    local.mkdir()
    (local / ".tarklone").mkdir()
    (local / ".tarklone" / "state.json").write_text("x", encoding="utf-8")
    (local / "keep.txt").write_text("x", encoding="utf-8")
    (local / "skip.tmp").write_text("x", encoding="utf-8")
    scanner = LocalScanner(local, [".tarklone/**", "*.tmp"])
    entries, errors = scanner.scan()
    assert errors == []
    assert "keep.txt" in entries
    assert "skip.tmp" not in entries
    assert not any(path.startswith(".tarklone") for path in entries)


def test_logical_size_is_used_for_encrypted_entries(tmp_path: Path) -> None:
    class LogicalRemote(MemoryRemote):
        def list_tree(self) -> list[RemoteEntry]:
            entries = super().list_tree()
            for entry in entries:
                if entry.path == "enc.bin":
                    entry.logical_size = 5
            return entries

    remote = LogicalRemote()
    remote.files["enc.bin"] = b"x" * 42
    remote.mtimes["enc.bin"] = time.time()
    engine, _ = make_engine(tmp_path, remote)
    local_file = engine.local_root / "enc.bin"
    local_file.write_bytes(b"hello")

    result = engine.sync()

    assert result.conflicts == 0
    assert result.uploaded == 0
    assert result.downloaded == 0


def test_empty_folders_are_skipped_when_unsupported(tmp_path: Path) -> None:
    class NoEmptyFolders(MemoryRemote):
        supports_empty_folders = False

    remote = NoEmptyFolders()
    engine, _ = make_engine(tmp_path, remote)
    (engine.local_root / "empty").mkdir()

    first = engine.sync()
    second = engine.sync()

    assert first.created_folders == 0
    assert second.created_folders == 0
    assert first.changed == 0
    assert second.changed == 0


def test_traversal_remote_entry_is_reported(tmp_path: Path) -> None:
    remote = MemoryRemote()
    remote.files["../evil.txt"] = b"evil"
    engine, _ = make_engine(tmp_path, remote)
    result = engine.sync()
    assert any("Ungültiger relativer Pfad" in error for error in result.errors)
