from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest

from tarklone.crypto import MAGIC, EncryptionManager
from tarklone.encrypted_remote import EncryptionAdapter
from tarklone.state import SyncState
from tarklone.sync import RemoteEntry, SyncEngine, SyncError


class FakeRemote:
    supports_empty_folders = False

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.mtimes: dict[str, float] = {}
        self.ids: dict[str, str] = {}
        self._next_id = 1

    def _id(self, path: str) -> str:
        if path not in self.ids:
            self.ids[path] = f"r{self._next_id}"
            self._next_id += 1
        return self.ids[path]

    def close(self) -> None:
        pass

    def list_tree(self) -> list[RemoteEntry]:
        return [
            RemoteEntry(
                path=path,
                kind="file",
                size=len(data),
                mtime=self.mtimes.get(path, 0.0),
                entry_id=self._id(path),
            )
            for path, data in self.files.items()
        ]

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
        self.mtimes[remote_path] = time.time()
        return RemoteEntry(
            path=remote_path,
            size=len(data),
            mtime=self.mtimes[remote_path],
            entry_id=self._id(remote_path),
        )

    def download_range(self, entry: RemoteEntry, start: int, length: int) -> bytes:
        return self.files[entry.path][start : start + length]

    def delete(self, entry: RemoteEntry) -> None:
        self.files.pop(entry.path, None)

    def create_folder(self, remote_path: str) -> None:
        return None


def test_encrypted_sync_roundtrip_and_password_change(tmp_path: Path) -> None:
    local = tmp_path / "local"
    local.mkdir()
    manager = EncryptionManager(local, iterations=10_000, cache_in_keyring=False)
    manager.set_password("password123")
    (local / "encrypted" / "test.txt").write_text("secret data", encoding="utf-8")
    (local / "normal.txt").write_text("normal data", encoding="utf-8")

    remote = FakeRemote()
    adapter = EncryptionAdapter(remote, manager)
    state = SyncState(tmp_path / "state.sqlite3")
    try:
        engine = SyncEngine(local, adapter, state, exclude=[".tarklone/**"])
        first = engine.sync()
        assert first.errors == []
        assert "normal.txt" in remote.files
        assert remote.files["normal.txt"] == b"normal data"
        assert remote.files["encrypted/test.txt"][:5] == MAGIC
        assert b"secret data" not in remote.files["encrypted/test.txt"]
        assert "encrypted/.tarklone-key.json" in remote.files

        second = engine.sync()
        assert second.changed == 0
        assert second.errors == []

        before = hashlib.sha256(remote.files["encrypted/test.txt"]).hexdigest()
        manager.change_password("password123", "newpassword456")
        third = engine.sync()
        after = hashlib.sha256(remote.files["encrypted/test.txt"]).hexdigest()
        assert third.errors == []
        assert third.uploaded == 1  # nur die Schlüsseldatei
        assert before == after

        encrypted_entry = [
            entry for entry in adapter.list_tree() if entry.path == "encrypted/test.txt"
        ][0]
        target = tmp_path / "decrypted.txt"
        adapter.download(encrypted_entry, target)
        assert target.read_text(encoding="utf-8") == "secret data"
        assert encrypted_entry.logical_size == len("secret data")
    finally:
        state.close()
        adapter.close()


def test_locked_adapter_does_not_download_ciphertext(tmp_path: Path) -> None:
    manager = EncryptionManager(tmp_path / "local", iterations=10_000, cache_in_keyring=False)
    remote = FakeRemote()
    remote.files["encrypted/.tarklone-key.json"] = b'{"version":1}'
    remote.files["encrypted/secret.txt"] = b"TKLE1" + b"x" * 100
    remote.files["normal.txt"] = b"normal"
    adapter = EncryptionAdapter(remote, manager)
    try:
        paths = [entry.path for entry in adapter.list_tree()]
        assert "encrypted/.tarklone-key.json" in paths
        assert "encrypted/secret.txt" not in paths
        assert "normal.txt" in paths
        with pytest.raises(SyncError):
            adapter.download(
                RemoteEntry(
                    path="encrypted/secret.txt",
                    size=len(remote.files["encrypted/secret.txt"]),
                ),
                tmp_path / "out",
            )
    finally:
        adapter.close()
