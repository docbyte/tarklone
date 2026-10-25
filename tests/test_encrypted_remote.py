from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from tarklone.crypto import HEADER_SIZE, HEADER_V1, MAGIC, EncryptionManager, parse_file_header
from tarklone.encrypted_remote import EncryptedStateAdapter, EncryptionAdapter
from tarklone.state import EntryState, SyncState
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
        destination.write_bytes(self.files[entry.remote_path or entry.path])

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
        return self.files[entry.remote_path or entry.path][start : start + length]

    def delete(self, entry: RemoteEntry) -> None:
        self.files.pop(entry.path, None)

    def create_folder(self, remote_path: str) -> None:
        return None


def test_encrypted_sync_roundtrip_and_password_change(tmp_path: Path) -> None:
    local = tmp_path / "local"
    local.mkdir()
    manager = EncryptionManager(local, iterations=10_000, cache_in_keyring=False)
    manager.set_password("a-long-unique-passphrase")
    (local / "encrypted" / "test.txt").write_text("secret data", encoding="utf-8")
    (local / "normal.txt").write_text("normal data", encoding="utf-8")

    token_path = manager.encrypt_path("encrypted/test.txt")
    remote = FakeRemote()
    adapter = EncryptionAdapter(remote, manager)
    raw_state = SyncState(tmp_path / "state.sqlite3")
    state = EncryptedStateAdapter(raw_state, manager)
    try:
        engine = SyncEngine(local, adapter, state, exclude=[".tarklone/**"])
        first = engine.sync()
        assert first.errors == []
        assert "normal.txt" in remote.files
        assert remote.files["normal.txt"] == b"normal data"
        assert token_path in remote.files
        assert "encrypted/test.txt" not in remote.files
        assert remote.files[token_path][:5] == MAGIC
        assert b"secret data" not in remote.files[token_path]
        assert "encrypted/.tarklone-key.json" in remote.files
        assert "encrypted/test.txt" in state.all()
        stored_paths = raw_state.all()
        assert "encrypted/test.txt" not in stored_paths
        assert any(
            path.startswith("encrypted/") and "test.txt" not in path
            for path in stored_paths
        )

        second = engine.sync()
        assert second.changed == 0
        assert second.errors == []

        before = hashlib.sha256(remote.files[token_path]).hexdigest()
        manager.change_password(
            "a-long-unique-passphrase", "another-long-unique-passphrase"
        )
        third = engine.sync()
        after = hashlib.sha256(remote.files[token_path]).hexdigest()
        assert third.errors == []
        assert third.uploaded == 1  # nur die Schlüsseldatei
        assert before == after
        assert manager.encrypt_path("encrypted/test.txt") == token_path

        encrypted_entry = [
            entry for entry in adapter.list_tree() if entry.path == "encrypted/test.txt"
        ][0]
        target = tmp_path / "decrypted.txt"
        adapter.download(encrypted_entry, target)
        assert target.read_text(encoding="utf-8") == "secret data"
        assert encrypted_entry.logical_size == len("secret data")
    finally:
        raw_state.close()
        adapter.close()


def test_encrypted_file_revision_increments_and_rollback_is_rejected(
    tmp_path: Path,
) -> None:
    local = tmp_path / "local"
    local.mkdir()
    manager = EncryptionManager(local, cache_in_keyring=False)
    manager.set_password("a-long-unique-passphrase")
    source = local / "encrypted" / "revision.txt"
    source.write_text("first version", encoding="utf-8")
    token_path = manager.encrypt_path("encrypted/revision.txt")
    remote = FakeRemote()
    adapter = EncryptionAdapter(remote, manager)
    raw_state = SyncState(tmp_path / "state.sqlite3")
    state = EncryptedStateAdapter(raw_state, manager)
    try:
        engine = SyncEngine(local, adapter, state, exclude=[".tarklone/**"])
        first = engine.sync()
        assert first.errors == []
        old_blob = remote.files[token_path]
        header = old_blob[:HEADER_SIZE]
        assert parse_file_header(header)[-1] == 1

        source.write_text("second version", encoding="utf-8")
        future = time.time() + 5
        os.utime(source, (future, future))
        second = engine.sync()
        assert second.errors == []
        new_blob = remote.files[token_path]
        assert parse_file_header(new_blob[:HEADER_SIZE])[-1] == 2
        assert new_blob != old_blob

        # Simulate the server replaying an authenticated older blob with the
        # same file id and size metadata. The generation check must block it.
        remote.files[token_path] = old_blob
        rollback = engine.sync()
        assert rollback.errors
        assert "rückläufig" in rollback.errors[0]
        assert source.read_text(encoding="utf-8") == "second version"
        assert remote.files[token_path] == old_blob
    finally:
        raw_state.close()
        adapter.close()


def test_legacy_plaintext_names_are_migrated(tmp_path: Path) -> None:
    local = tmp_path / "local"
    local.mkdir()
    manager = EncryptionManager(local, iterations=10_000, cache_in_keyring=False)
    manager.set_password("a-long-unique-passphrase")

    source = local / "encrypted" / "legacy.txt"
    source.write_text("legacy content", encoding="utf-8")
    legacy_bytes = source.read_bytes()
    nonce = os.urandom(12)
    header = HEADER_V1.pack(MAGIC, 1, len(legacy_bytes), nonce)
    encryptor = Cipher(
        algorithms.AES(manager.master_key), modes.GCM(nonce)
    ).encryptor()
    encryptor.authenticate_additional_data(header)
    cipher = (
        header
        + encryptor.update(legacy_bytes)
        + encryptor.finalize()
        + encryptor.tag
    )

    remote = FakeRemote()
    remote.files["encrypted/legacy.txt"] = cipher
    remote.files["encrypted/.tarklone-key.json"] = manager.keyfile.read_bytes()
    adapter = EncryptionAdapter(remote, manager)
    raw_state = SyncState(tmp_path / "state.sqlite3")
    state = EncryptedStateAdapter(raw_state, manager)
    try:
        entries = adapter.list_tree()
        paths = {entry.path for entry in entries}
        assert "encrypted/legacy.txt" in paths
        token_path = manager.encrypt_path("encrypted/legacy.txt")
        assert token_path in remote.files
        assert "encrypted/legacy.txt" not in remote.files
        assert remote.files[token_path][:5] == MAGIC
        assert remote.files[token_path][5] == 1
        engine = SyncEngine(local, adapter, state, exclude=[".tarklone/**"])
        result = engine.sync()
        assert result.errors == []
        assert result.uploaded == 1
        assert remote.files[token_path][5] == 2
        target = tmp_path / "legacy-decrypted.txt"
        refreshed_entry = next(
            entry for entry in adapter.list_tree()
            if entry.path == "encrypted/legacy.txt"
        )
        adapter.download(refreshed_entry, target)
        assert target.read_text(encoding="utf-8") == "legacy content"
    finally:
        raw_state.close()
        adapter.close()


def test_encrypted_state_migrates_plaintext_paths_to_tokens(tmp_path: Path) -> None:
    local = tmp_path / "local"
    local.mkdir()
    manager = EncryptionManager(local, cache_in_keyring=False)
    manager.set_password("a-long-unique-passphrase")
    raw_state = SyncState(tmp_path / "state.sqlite3")
    raw_state.upsert(
        EntryState(
            path="encrypted/private/name.txt",
            kind="file",
            local_size=7,
            remote_size=7,
        )
    )
    state = EncryptedStateAdapter(raw_state, manager)
    try:
        logical = state.all()
        assert "encrypted/private/name.txt" in logical
        stored = raw_state.all()
        assert "encrypted/private/name.txt" not in stored
        assert len(stored) == 1
        assert manager.decrypt_path(next(iter(stored))) == "encrypted/private/name.txt"
        assert b"private/name.txt" not in raw_state.path.read_bytes()
    finally:
        raw_state.close()


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


def test_mis_cased_remote_encryption_root_fails_closed(tmp_path: Path) -> None:
    manager = EncryptionManager(
        tmp_path / "local", cache_in_keyring=False
    )
    manager.set_password("a-long-unique-passphrase")
    remote = FakeRemote()
    remote.files["Encrypted/.tarklone-key.json"] = manager.keyfile.read_bytes()
    remote.files["Encrypted/secret-token"] = b"not safe to expose as plaintext"
    adapter = EncryptionAdapter(remote, manager)
    try:
        with pytest.raises(SyncError, match="exakt 'encrypted'"):
            adapter.list_tree()
    finally:
        adapter.close()


def test_remote_target_cannot_be_inside_encrypted_root(tmp_path: Path) -> None:
    manager = EncryptionManager(tmp_path / "local", cache_in_keyring=False)
    manager.set_password("a-long-unique-passphrase")
    remote = FakeRemote()
    remote.target_path = "encrypted/archive"
    adapter = EncryptionAdapter(remote, manager)
    try:
        with pytest.raises(SyncError, match="nicht innerhalb"):
            adapter.list_tree()
    finally:
        adapter.close()
