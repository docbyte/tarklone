from __future__ import annotations

import base64
import json
import os
import struct
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM, AESSIV
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

KEYFILE_NAME = ".tarklone-key.json"
ENCRYPTED_DIR = "encrypted"
MAGIC = b"TKLE1"
VERSION = 1
HEADER = struct.Struct(">5sBQ12s")  # magic, version, plaintext size, nonce
HEADER_SIZE = HEADER.size
TAG_SIZE = 16
CHUNK_SIZE = 1024 * 1024
DEFAULT_ITERATIONS = 600_000
KEY_AAD = b"tarklone-key-v1"
NAME_KEY_SALT = b"tarklone-name-v1"
NAME_KEY_INFO = b"aes-siv"
MAX_NAME_TOKEN_LEN = 240


def _normalize_rel(path: str) -> str:
    value = path.replace("\\", "/").strip("/")
    parts = [part for part in value.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise CryptoError(f"Ungültiger Pfad: {path!r}")
    return "/".join(parts)


class CryptoError(RuntimeError):
    pass


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise CryptoError("Ungültige Schlüsseldatei.") from exc


def _derive_kek(password: str, salt: bytes, iterations: int) -> bytes:
    if not password:
        raise CryptoError("Das Passwort darf nicht leer sein.")
    if iterations < 10_000:
        raise CryptoError("Ungültige KDF-Parameter.")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(password.encode("utf-8"))


class EncryptionManager:
    """AES-256-GCM für den Unterordner ``encrypted``.

    Die Dateien werden mit einem zufälligen Master-Key verschlüsselt. Das
    Passwort verpackt nur diesen Master-Key in ``encrypted/.tarklone-key.json``.
    Eine Passwortänderung muss daher keine Datei neu verschlüsseln.
    """

    def __init__(
        self,
        local_root: Path,
        *,
        iterations: int = DEFAULT_ITERATIONS,
        cache_in_keyring: bool = True,
    ):
        self.local_root = Path(local_root).resolve()
        self.encrypted_dir = self.local_root / ENCRYPTED_DIR
        self.keyfile = self.encrypted_dir / KEYFILE_NAME
        self.iterations = iterations
        self.cache_in_keyring = cache_in_keyring
        self.master_key: bytes | None = None
        self._name_key_cache: bytes | None = None

    @property
    def enabled(self) -> bool:
        return self.keyfile.is_file()

    @property
    def unlocked(self) -> bool:
        return self.master_key is not None

    def is_keyfile(self, path: str) -> bool:
        normalized = path.replace("\\", "/").strip("/")
        return normalized == f"{ENCRYPTED_DIR}/{KEYFILE_NAME}"

    def is_encrypted_path(self, path: str) -> bool:
        normalized = path.replace("\\", "/").strip("/")
        return normalized.startswith(ENCRYPTED_DIR + "/") and not self.is_keyfile(
            normalized
        )

    def ensure_folder(self) -> Path:
        self.encrypted_dir.mkdir(parents=True, exist_ok=True)
        return self.encrypted_dir

    def set_password(self, password: str) -> None:
        if self.keyfile.exists():
            raise CryptoError("Es ist bereits ein Passwort gesetzt.")
        if len(password) < 8:
            raise CryptoError("Das Passwort muss mindestens 8 Zeichen lang sein.")
        if self.encrypted_dir.exists():
            existing = [
                path
                for path in self.encrypted_dir.rglob("*")
                if path.is_file() and path.name != KEYFILE_NAME
            ]
            if existing:
                raise CryptoError(
                    "Der Ordner encrypted enthält bereits Dateien. Bitte ihn vor "
                    "dem Setzen des Passworts leeren, damit keine Klartextdateien "
                    "unverschlüsselt hochgeladen werden."
                )
        master = os.urandom(32)
        self._write_keyfile(master, password)
        self.master_key = master
        self._name_key_cache = None
        self._cache_master(master)

    def change_password(self, old_password: str, new_password: str) -> None:
        if not self.keyfile.exists():
            raise CryptoError("Es ist noch kein Passwort gesetzt.")
        if len(new_password) < 8:
            raise CryptoError("Das neue Passwort muss mindestens 8 Zeichen lang sein.")
        master = self._unwrap_keyfile(old_password)
        self._write_keyfile(master, new_password)
        self.master_key = master
        self._name_key_cache = None
        self._cache_master(master)

    def unlock(self, password: str) -> None:
        if not self.keyfile.exists():
            raise CryptoError("Es ist noch kein Passwort gesetzt.")
        master = self._unwrap_keyfile(password)
        self.master_key = master
        self._name_key_cache = None
        self._cache_master(master)

    def load_cached_key(self) -> bool:
        if not self.keyfile.exists():
            return False
        try:
            import keyring

            raw = keyring.get_password("tarklone", "crypto-master")
        except Exception:
            return False
        if not raw:
            return False
        try:
            master = _unb64(raw)
        except CryptoError:
            return False
        if len(master) != 32:
            return False
        self.master_key = master
        self._name_key_cache = None
        return True

    def lock(self) -> None:
        self.master_key = None
        self._name_key_cache = None

    def _cache_master(self, master: bytes) -> None:
        if not self.cache_in_keyring:
            return
        try:
            import keyring

            keyring.set_password("tarklone", "crypto-master", _b64(master))
        except Exception:
            # Der Schlüssel bleibt für die laufende Sitzung im Speicher.
            pass

    def _read_keyfile(self) -> dict[str, Any]:
        try:
            data = json.loads(self.keyfile.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CryptoError("Schlüsseldatei konnte nicht gelesen werden.") from exc
        if not isinstance(data, dict) or data.get("version") != 1:
            raise CryptoError("Unbekanntes Format der Schlüsseldatei.")
        if data.get("kdf") != "pbkdf2-sha256":
            raise CryptoError("Nicht unterstütztes KDF-Verfahren.")
        return data

    def _unwrap_keyfile(self, password: str) -> bytes:
        data = self._read_keyfile()
        try:
            iterations = int(data["iterations"])
            salt = _unb64(str(data["salt"]))
            nonce = _unb64(str(data["nonce"]))
            wrapped = _unb64(str(data["wrapped_key"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise CryptoError("Schlüsseldatei ist unvollständig.") from exc
        kek = _derive_kek(password, salt, iterations)
        try:
            master = AESGCM(kek).decrypt(nonce, wrapped, KEY_AAD)
        except InvalidTag as exc:
            raise CryptoError("Falsches Passwort oder beschädigte Schlüsseldatei.") from exc
        if len(master) != 32:
            raise CryptoError("Ungültige Schlüssellänge.")
        return master

    def _write_keyfile(self, master: bytes, password: str) -> None:
        self.ensure_folder()
        salt = os.urandom(16)
        nonce = os.urandom(12)
        kek = _derive_kek(password, salt, self.iterations)
        wrapped = AESGCM(kek).encrypt(nonce, master, KEY_AAD)
        now = datetime.now().isoformat(timespec="seconds")
        data = {
            "version": 1,
            "kdf": "pbkdf2-sha256",
            "iterations": self.iterations,
            "salt": _b64(salt),
            "nonce": _b64(nonce),
            "wrapped_key": _b64(wrapped),
            "name_encryption": 1,
            "updated_at": now,
        }
        temp = self.keyfile.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, self.keyfile)

    def _name_key(self) -> bytes:
        if self.master_key is None:
            raise CryptoError("Die Verschlüsselung ist gesperrt. Bitte Passwort eingeben.")
        if self._name_key_cache is None:
            hkdf = HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=NAME_KEY_SALT,
                info=NAME_KEY_INFO,
            )
            self._name_key_cache = hkdf.derive(self.master_key)
        return self._name_key_cache

    @staticmethod
    def _validate_name_component(name: str) -> None:
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            raise CryptoError("Ungültiger Datei- oder Ordnername.")

    def encrypt_name_component(self, name: str, parent: str) -> str:
        self._validate_name_component(name)
        parent_norm = _normalize_rel(parent)
        aad = parent_norm.encode("utf-8")
        ciphertext = AESSIV(self._name_key()).encrypt(name.encode("utf-8"), [aad])
        token = base64.urlsafe_b64encode(ciphertext).rstrip(b"=").decode("ascii")
        if len(token) > MAX_NAME_TOKEN_LEN:
            raise CryptoError(
                f"Datei- oder Ordnername ist zu lang für verschlüsselte Namen: {name!r}"
            )
        return token

    def decrypt_name_component(self, token: str, parent: str) -> str:
        parent_norm = _normalize_rel(parent)
        aad = parent_norm.encode("utf-8")
        padded = token + "=" * (-len(token) % 4)
        try:
            ciphertext = base64.urlsafe_b64decode(padded.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise CryptoError("Ungültiger verschlüsselter Name.") from exc
        try:
            raw = AESSIV(self._name_key()).decrypt(ciphertext, [aad])
        except InvalidTag as exc:
            raise CryptoError(
                "Datei-/Ordnername konnte nicht entschlüsselt werden "
                "(falscher Schlüssel oder alter Klartextname)."
            ) from exc
        try:
            name = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CryptoError("Entschlüsselter Name ist kein gültiges UTF-8.") from exc
        self._validate_name_component(name)
        return name

    def encrypt_path(self, path: str) -> str:
        normalized = _normalize_rel(path)
        if normalized == ENCRYPTED_DIR:
            return normalized
        if not normalized.startswith(ENCRYPTED_DIR + "/"):
            raise CryptoError("Nur Pfade unterhalb von encrypted/ können verschlüsselt werden.")
        parent = ENCRYPTED_DIR
        result = [ENCRYPTED_DIR]
        for component in normalized.split("/")[1:]:
            result.append(self.encrypt_name_component(component, parent))
            parent = f"{parent}/{component}"
        return "/".join(result)

    def decrypt_path(self, path: str) -> str:
        normalized = _normalize_rel(path)
        if normalized == ENCRYPTED_DIR:
            return normalized
        if not normalized.startswith(ENCRYPTED_DIR + "/"):
            raise CryptoError("Nur Pfade unterhalb von encrypted/ können entschlüsselt werden.")
        parent = ENCRYPTED_DIR
        result = [ENCRYPTED_DIR]
        for token in normalized.split("/")[1:]:
            name = self.decrypt_name_component(token, parent)
            result.append(name)
            parent = f"{parent}/{name}"
        return "/".join(result)

    def _require_key(self) -> bytes:
        if self.master_key is None:
            raise CryptoError("Die Verschlüsselung ist gesperrt. Bitte Passwort eingeben.")
        return self.master_key

    def encrypt_file(self, source: Path, destination: Path) -> int:
        key = self._require_key()
        plain_size = source.stat().st_size
        nonce = os.urandom(12)
        header = HEADER.pack(MAGIC, VERSION, plain_size, nonce)
        encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
        encryptor.authenticate_additional_data(header)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as src, destination.open("wb") as dst:
            dst.write(header)
            while True:
                chunk = src.read(CHUNK_SIZE)
                if not chunk:
                    break
                dst.write(encryptor.update(chunk))
            dst.write(encryptor.finalize())
            dst.write(encryptor.tag)
        return plain_size

    def decrypt_file(self, source: Path, destination: Path) -> int:
        key = self._require_key()
        size = source.stat().st_size
        if size < HEADER_SIZE + TAG_SIZE:
            raise CryptoError("Verschlüsselte Datei ist zu kurz.")
        with source.open("rb") as src:
            header = src.read(HEADER_SIZE)
            magic, version, plain_size, nonce = HEADER.unpack(header)
            if magic != MAGIC or version != VERSION:
                raise CryptoError("Unbekanntes Format der verschlüsselten Datei.")
            expected = HEADER_SIZE + plain_size + TAG_SIZE
            if size != expected:
                raise CryptoError("Verschlüsselte Datei hat eine unerwartete Größe.")
            src.seek(size - TAG_SIZE)
            tag = src.read(TAG_SIZE)
            src.seek(HEADER_SIZE)
            decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
            decryptor.authenticate_additional_data(header)
            destination.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            remaining = plain_size
            try:
                with destination.open("wb") as dst:
                    while remaining > 0:
                        chunk = src.read(min(CHUNK_SIZE, remaining))
                        if not chunk:
                            raise CryptoError("Verschlüsselte Datei ist unvollständig.")
                        dst.write(decryptor.update(chunk))
                        written += len(chunk)
                        remaining -= len(chunk)
                    dst.write(decryptor.finalize())
            except InvalidTag as exc:
                destination.unlink(missing_ok=True)
                raise CryptoError(
                    "Entschlüsselung fehlgeschlagen: Datei wurde manipuliert oder "
                    "der Schlüssel ist falsch."
                ) from exc
            if written != plain_size:
                destination.unlink(missing_ok=True)
                raise CryptoError("Entschlüsselte Datei hat eine unerwartete Größe.")
        return plain_size
