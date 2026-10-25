from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from tarklone.crypto import (
    HEADER_V1,
    HEADER_SIZE,
    KEY_AAD_V1,
    KEYFILE_NAME,
    MAGIC,
    CryptoError,
    EncryptionManager,
    _b64,
    _derive_legacy_kek,
    parse_file_header,
    redact_encrypted_path,
)


def make_manager(root: Path) -> EncryptionManager:
    return EncryptionManager(root, iterations=10_000, cache_in_keyring=False)


def test_set_password_creates_folder_and_keyfile(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    assert not manager.enabled
    manager.set_password("a-long-unique-passphrase")
    assert manager.enabled
    assert (tmp_path / "encrypted" / KEYFILE_NAME).is_file()
    data = json.loads((tmp_path / "encrypted" / KEYFILE_NAME).read_text(encoding="utf-8"))
    assert data["version"] == 2
    assert data["kdf"] == "argon2id"
    assert "wrapped_key" in data
    assert "master" not in data


def test_wrong_password_is_rejected(tmp_path: Path) -> None:
    make_manager(tmp_path).set_password("a-long-unique-passphrase")
    other = make_manager(tmp_path)
    with pytest.raises(CryptoError):
        other.unlock("wrong-password")


def test_encrypt_decrypt_roundtrip(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.set_password("a-long-unique-passphrase")
    source = tmp_path / "plain.bin"
    source.write_bytes(b"hello tarkle " * 1000)
    encrypted = tmp_path / "cipher.bin"
    decrypted = tmp_path / "decrypted.bin"

    manager.encrypt_file(
        source, encrypted, logical_path="encrypted/test.bin"
    )
    assert encrypted.read_bytes()[:5] == MAGIC
    assert encrypted.stat().st_size == HEADER_SIZE + source.stat().st_size + 16

    manager.decrypt_file(
        encrypted, decrypted, logical_path="encrypted/test.bin"
    )
    assert decrypted.read_bytes() == source.read_bytes()


def test_empty_file_roundtrip(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.set_password("a-long-unique-passphrase")
    source = tmp_path / "empty"; source.write_bytes(b"")
    encrypted = tmp_path / "empty.enc"
    decrypted = tmp_path / "empty.out"
    manager.encrypt_file(
        source, encrypted, logical_path="encrypted/empty"
    )
    manager.decrypt_file(
        encrypted, decrypted, logical_path="encrypted/empty"
    )
    assert decrypted.read_bytes() == b""


def test_password_change_does_not_reencrypt_files(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.set_password("a-long-unique-passphrase")
    source = tmp_path / "plain.txt"; source.write_bytes(b"secret content")
    encrypted = tmp_path / "cipher.bin"
    manager.encrypt_file(
        source, encrypted, logical_path="encrypted/plain.txt"
    )
    before = hashlib.sha256(encrypted.read_bytes()).hexdigest()

    manager.change_password(
        "a-long-unique-passphrase", "another-long-unique-passphrase"
    )
    after = hashlib.sha256(encrypted.read_bytes()).hexdigest()
    assert before == after

    fresh = make_manager(tmp_path)
    with pytest.raises(CryptoError):
        fresh.unlock("a-long-unique-passphrase")
    fresh.unlock("another-long-unique-passphrase")
    decrypted = tmp_path / "decrypted.txt"
    fresh.decrypt_file(
        encrypted, decrypted, logical_path="encrypted/plain.txt"
    )
    assert decrypted.read_bytes() == source.read_bytes()


def test_tampered_ciphertext_is_rejected(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.set_password("a-long-unique-passphrase")
    source = tmp_path / "plain.txt"; source.write_bytes(b"secret")
    encrypted = tmp_path / "cipher.bin"
    manager.encrypt_file(
        source, encrypted, logical_path="encrypted/plain.txt"
    )
    data = bytearray(encrypted.read_bytes())
    data[-1] ^= 0xFF
    encrypted.write_bytes(data)
    with pytest.raises(CryptoError):
        manager.decrypt_file(
            encrypted,
            tmp_path / "out",
            logical_path="encrypted/plain.txt",
        )
    assert not (tmp_path / "out").exists()
    assert not list(tmp_path.glob(".tarklone-decrypt-*.tmp"))


def test_content_ciphertext_is_bound_to_logical_path(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.set_password("a-long-unique-passphrase")
    source = tmp_path / "plain.txt"
    source.write_bytes(b"contents for one path")
    encrypted = tmp_path / "cipher.bin"
    manager.encrypt_file(
        source,
        encrypted,
        logical_path="encrypted/private/one.txt",
        generation=17,
    )
    header = encrypted.read_bytes()[:HEADER_SIZE]
    version, _size, file_id, _nonce, _header_size, generation = parse_file_header(header)
    assert version == 2
    assert generation == 17
    assert int.from_bytes(file_id[:8], "big") == 17

    with pytest.raises(CryptoError):
        manager.decrypt_file(
            encrypted,
            tmp_path / "wrong-path.out",
            logical_path="encrypted/private/two.txt",
        )
    assert not (tmp_path / "wrong-path.out").exists()
    manager.decrypt_file(
        encrypted,
        tmp_path / "right-path.out",
        logical_path="encrypted/private/one.txt",
    )
    assert (tmp_path / "right-path.out").read_bytes() == source.read_bytes()


def _write_legacy_keyfile(root: Path, password: str, master: bytes) -> None:
    manager = make_manager(root)
    manager.ensure_folder()
    salt = os.urandom(16)
    nonce = os.urandom(12)
    iterations = 10_000
    kek = _derive_legacy_kek(password, salt, iterations)
    wrapped = AESGCM(kek).encrypt(nonce, master, KEY_AAD_V1)
    data = {
        "version": 1,
        "kdf": "pbkdf2-sha256",
        "iterations": iterations,
        "salt": _b64(salt),
        "nonce": _b64(nonce),
        "wrapped_key": _b64(wrapped),
        "name_encryption": 1,
    }
    manager.keyfile.write_text(json.dumps(data), encoding="utf-8")


def test_legacy_keyfile_migrates_and_legacy_content_still_decrypts(
    tmp_path: Path,
) -> None:
    password = "legacy-pass"
    master = os.urandom(32)
    _write_legacy_keyfile(tmp_path, password, master)
    plaintext = b"older encrypted content"
    nonce = os.urandom(12)
    header = HEADER_V1.pack(MAGIC, 1, len(plaintext), nonce)
    encryptor = Cipher(algorithms.AES(master), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(header)
    cipher = header + encryptor.update(plaintext) + encryptor.finalize() + encryptor.tag
    source = tmp_path / "legacy-v1.bin"
    source.write_bytes(cipher)

    manager = make_manager(tmp_path)
    manager.unlock(password)
    upgraded = json.loads(manager.keyfile.read_text(encoding="utf-8"))
    assert upgraded["version"] == 2
    assert upgraded["kdf"] == "argon2id"
    output = tmp_path / "legacy.out"
    manager.decrypt_file(
        source,
        output,
        logical_path="encrypted/legacy.txt",
    )
    assert output.read_bytes() == plaintext


def test_remote_keyfile_kdf_cost_is_bounded(tmp_path: Path) -> None:
    _write_legacy_keyfile(tmp_path, "legacy-pass", os.urandom(32))
    data = json.loads((tmp_path / "encrypted" / KEYFILE_NAME).read_text())
    data["iterations"] = 2_000_001
    (tmp_path / "encrypted" / KEYFILE_NAME).write_text(json.dumps(data))
    with pytest.raises(CryptoError, match="KDF-Parameter"):
        make_manager(tmp_path).unlock("legacy-pass")

    argon_root = tmp_path / "argon"
    manager = make_manager(argon_root)
    manager.set_password("a-long-unique-passphrase")
    data = json.loads(manager.keyfile.read_text(encoding="utf-8"))
    data["memory_kib"] = 10**9
    manager.keyfile.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CryptoError, match="Grenzen"):
        make_manager(argon_root).unlock("a-long-unique-passphrase")


def test_set_password_can_allow_existing_files_after_reset(tmp_path: Path) -> None:
    encrypted_dir = tmp_path / "encrypted"
    encrypted_dir.mkdir()
    (encrypted_dir / "local.txt").write_text("local plaintext", encoding="utf-8")
    manager = make_manager(tmp_path)
    with pytest.raises(CryptoError):
        manager.set_password("a-long-unique-passphrase")
    manager.set_password("a-long-unique-passphrase", allow_existing=True)
    assert manager.enabled


def test_lock_clears_cached_master_but_keeps_keyfile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keyring

    values: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        keyring,
        "get_keyring",
        lambda: type("TestKeyring", (), {"priority": 1})(),
    )
    monkeypatch.setattr(keyring, "set_password", lambda s, u, v: values.__setitem__((s, u), v))
    monkeypatch.setattr(keyring, "get_password", lambda s, u: values.get((s, u)))
    monkeypatch.setattr(keyring, "delete_password", lambda s, u: values.pop((s, u), None))

    manager = EncryptionManager(tmp_path, iterations=10_000)
    manager.set_password("a-long-unique-passphrase")
    assert values.get(("tarklone", "crypto-master"))
    manager.lock(clear_cache=True)
    assert not manager.unlocked
    assert not values.get(("tarklone", "crypto-master"))
    assert manager.keyfile.is_file()


def test_custom_keyring_username_per_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keyring

    values: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        keyring,
        "get_keyring",
        lambda: type("TestKeyring", (), {"priority": 1})(),
    )
    monkeypatch.setattr(keyring, "set_password", lambda s, u, v: values.__setitem__((s, u), v))
    monkeypatch.setattr(keyring, "get_password", lambda s, u: values.get((s, u)))
    monkeypatch.setattr(keyring, "delete_password", lambda s, u: values.pop((s, u), None))

    manager = EncryptionManager(
        tmp_path,
        iterations=10_000,
        keyring_username="crypto-master-ws-1",
    )
    manager.set_password("a-long-unique-passphrase")
    assert values.get(("tarklone", "crypto-master-ws-1"))
    assert not values.get(("tarklone", "crypto-master"))
    manager.lock(clear_cache=True)
    assert not values.get(("tarklone", "crypto-master-ws-1"))


def test_cached_master_is_bound_to_keyfile_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keyring

    values: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        keyring,
        "get_keyring",
        lambda: type("TestKeyring", (), {"priority": 1})(),
    )
    monkeypatch.setattr(keyring, "set_password", lambda s, u, v: values.__setitem__((s, u), v))
    monkeypatch.setattr(keyring, "get_password", lambda s, u: values.get((s, u)))
    monkeypatch.setattr(keyring, "delete_password", lambda s, u: values.pop((s, u), None))

    manager = EncryptionManager(tmp_path, iterations=10_000)
    manager.set_password("a-long-unique-passphrase")
    fresh = EncryptionManager(tmp_path, iterations=10_000)
    assert fresh.load_cached_key()

    manager.keyfile.write_text(
        manager.keyfile.read_text(encoding="utf-8") + " ",
        encoding="utf-8",
    )
    stale = EncryptionManager(tmp_path, iterations=10_000)
    assert not stale.load_cached_key()


def test_lock_reports_keyring_delete_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keyring

    values: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        keyring,
        "get_keyring",
        lambda: type("TestKeyring", (), {"priority": 1})(),
    )
    monkeypatch.setattr(keyring, "set_password", lambda s, u, v: values.__setitem__((s, u), v))
    monkeypatch.setattr(keyring, "get_password", lambda s, u: values.get((s, u)))
    monkeypatch.setattr(keyring, "delete_password", lambda *_args: None)

    manager = EncryptionManager(tmp_path, iterations=10_000)
    manager.set_password("a-long-unique-passphrase")
    assert manager.lock(clear_cache=True) is False
    assert not manager.unlocked
    assert values.get(("tarklone", "crypto-master"))


def test_reset_key_removes_keyfile_and_master(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.set_password("a-long-unique-passphrase")
    (tmp_path / "encrypted" / "keep.txt").write_text("keep", encoding="utf-8")
    manager.reset_key()
    assert not manager.keyfile.exists()
    assert not manager.unlocked
    assert (tmp_path / "encrypted" / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_enabling_requires_empty_encrypted_folder(tmp_path: Path) -> None:
    encrypted_dir = tmp_path / "encrypted"; encrypted_dir.mkdir()
    (encrypted_dir / "existing.txt").write_text("plain", encoding="utf-8")
    manager = make_manager(tmp_path)
    with pytest.raises(CryptoError):
        manager.set_password("a-long-unique-passphrase")
    (encrypted_dir / "existing.txt").unlink()
    manager.set_password("a-long-unique-passphrase")


def test_name_encryption_roundtrip(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.set_password("a-long-unique-passphrase")
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
    manager.set_password("a-long-unique-passphrase")
    path = "encrypted/Ordner/Datei.txt"
    before = manager.encrypt_path(path)
    manager.change_password(
        "a-long-unique-passphrase", "another-long-unique-passphrase"
    )
    assert manager.encrypt_path(path) == before
    assert manager.decrypt_path(before) == path


def test_too_long_name_is_rejected(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.set_password("a-long-unique-passphrase")
    with pytest.raises(CryptoError):
        manager.encrypt_path("encrypted/" + "x" * 200)


@pytest.mark.parametrize(
    "name",
    ["file:stream.txt", "CON", "nul.txt", "trailing.", "trailing "],
)
def test_nonportable_or_device_names_are_rejected(
    tmp_path: Path,
    name: str,
) -> None:
    manager = make_manager(tmp_path)
    manager.set_password("a-long-unique-passphrase")
    with pytest.raises(CryptoError):
        manager.encrypt_path(f"encrypted/{name}")


def test_keyfile_contains_name_encryption_marker(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.set_password("a-long-unique-passphrase")
    data = json.loads(manager.keyfile.read_text(encoding="utf-8"))
    assert data["name_encryption"] == 1


def test_path_classification(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    assert manager.is_encrypted_path("encrypted/foo.txt")
    assert manager.is_encrypted_path("encrypted/sub/foo.txt")
    assert manager.is_encrypted_path("Encrypted/foo.txt")
    with pytest.raises(CryptoError):
        manager.encrypt_path("Encrypted/foo.txt")
    assert not manager.is_encrypted_path("normal/foo.txt")
    assert not manager.is_encrypted_path(f"encrypted/{KEYFILE_NAME}")
    assert manager.is_keyfile(f"encrypted/{KEYFILE_NAME}")


def test_encrypted_path_redaction_hides_nested_names() -> None:
    assert redact_encrypted_path("encrypted/private/report.txt") == (
        "encrypted/<redacted>"
    )
    assert redact_encrypted_path("Encrypted/private/report.txt") == (
        "encrypted/<redacted>"
    )
    assert redact_encrypted_path("normal/report.txt") == "normal/report.txt"
