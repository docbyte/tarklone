from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tarklone.crypto import (
    HEADER_SIZE,
    KEYFILE_NAME,
    MAGIC,
    CryptoError,
    EncryptionManager,
)


def make_manager(root: Path) -> EncryptionManager:
    return EncryptionManager(root, iterations=10_000, cache_in_keyring=False)


def test_set_password_creates_folder_and_keyfile(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    assert not manager.enabled
    manager.set_password("password123")
    assert manager.enabled
    assert (tmp_path / "encrypted" / KEYFILE_NAME).is_file()
    data = json.loads((tmp_path / "encrypted" / KEYFILE_NAME).read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["kdf"] == "pbkdf2-sha256"
    assert "wrapped_key" in data
    assert "master" not in data


def test_wrong_password_is_rejected(tmp_path: Path) -> None:
    make_manager(tmp_path).set_password("password123")
    other = make_manager(tmp_path)
    with pytest.raises(CryptoError):
        other.unlock("wrong-password")


def test_encrypt_decrypt_roundtrip(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.set_password("password123")
    source = tmp_path / "plain.bin"
    source.write_bytes(b"hello tarkle " * 1000)
    encrypted = tmp_path / "cipher.bin"
    decrypted = tmp_path / "decrypted.bin"

    manager.encrypt_file(source, encrypted)
    assert encrypted.read_bytes()[:5] == MAGIC
    assert encrypted.stat().st_size == HEADER_SIZE + source.stat().st_size + 16

    manager.decrypt_file(encrypted, decrypted)
    assert decrypted.read_bytes() == source.read_bytes()


def test_empty_file_roundtrip(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.set_password("password123")
    source = tmp_path / "empty"; source.write_bytes(b"")
    encrypted = tmp_path / "empty.enc"
    decrypted = tmp_path / "empty.out"
    manager.encrypt_file(source, encrypted)
    manager.decrypt_file(encrypted, decrypted)
    assert decrypted.read_bytes() == b""


def test_password_change_does_not_reencrypt_files(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.set_password("password123")
    source = tmp_path / "plain.txt"; source.write_bytes(b"secret content")
    encrypted = tmp_path / "cipher.bin"
    manager.encrypt_file(source, encrypted)
    before = hashlib.sha256(encrypted.read_bytes()).hexdigest()

    manager.change_password("password123", "newpassword456")
    after = hashlib.sha256(encrypted.read_bytes()).hexdigest()
    assert before == after

    fresh = make_manager(tmp_path)
    with pytest.raises(CryptoError):
        fresh.unlock("password123")
    fresh.unlock("newpassword456")
    decrypted = tmp_path / "decrypted.txt"
    fresh.decrypt_file(encrypted, decrypted)
    assert decrypted.read_bytes() == source.read_bytes()


def test_tampered_ciphertext_is_rejected(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.set_password("password123")
    source = tmp_path / "plain.txt"; source.write_bytes(b"secret")
    encrypted = tmp_path / "cipher.bin"
    manager.encrypt_file(source, encrypted)
    data = bytearray(encrypted.read_bytes())
    data[-1] ^= 0xFF
    encrypted.write_bytes(data)
    with pytest.raises(CryptoError):
        manager.decrypt_file(encrypted, tmp_path / "out")


def test_enabling_requires_empty_encrypted_folder(tmp_path: Path) -> None:
    encrypted_dir = tmp_path / "encrypted"; encrypted_dir.mkdir()
    (encrypted_dir / "existing.txt").write_text("plain", encoding="utf-8")
    manager = make_manager(tmp_path)
    with pytest.raises(CryptoError):
        manager.set_password("password123")
    (encrypted_dir / "existing.txt").unlink()
    manager.set_password("password123")


def test_name_encryption_roundtrip(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.set_password("password123")
    path = "encrypted/Urlaub/Foto 1.jpg"
    encrypted = manager.encrypt_path(path)
    assert encrypted.startswith("encrypted/")
    assert "Urlaub" not in encrypted
    assert "Foto" not in encrypted
    assert manager.decrypt_path(encrypted) == path
    assert manager.encrypt_path(path) == encrypted
    other = manager.encrypt_path("encrypted/Andere/Foto 1.jpg")
    assert other != encrypted


def test_name_tokens_survive_password_change(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.set_password("password123")
    path = "encrypted/Ordner/Datei.txt"
    before = manager.encrypt_path(path)
    manager.change_password("password123", "newpassword456")
    assert manager.encrypt_path(path) == before
    assert manager.decrypt_path(before) == path


def test_too_long_name_is_rejected(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.set_password("password123")
    with pytest.raises(CryptoError):
        manager.encrypt_path("encrypted/" + "x" * 200)


def test_keyfile_contains_name_encryption_marker(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.set_password("password123")
    data = json.loads(manager.keyfile.read_text(encoding="utf-8"))
    assert data["name_encryption"] == 1


def test_path_classification(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    assert manager.is_encrypted_path("encrypted/foo.txt")
    assert manager.is_encrypted_path("encrypted/sub/foo.txt")
    assert not manager.is_encrypted_path("normal/foo.txt")
    assert not manager.is_encrypted_path(f"encrypted/{KEYFILE_NAME}")
    assert manager.is_keyfile(f"encrypted/{KEYFILE_NAME}")
