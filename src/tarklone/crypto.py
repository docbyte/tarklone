from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM, AESSIV
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

KEYFILE_NAME = ".tarklone-key.json"
ENCRYPTED_DIR = "encrypted"
MAGIC = b"TKLE1"
VERSION = 2
LEGACY_VERSION = 1
HEADER_PREFIX = struct.Struct(">5sB")
HEADER_V1 = struct.Struct(">5sBQ12s")  # magic, version, plaintext size, nonce
HEADER_V2 = struct.Struct(">5sBQQ8s12s")  # + revision, random file id, nonce
HEADER = HEADER_V2
HEADER_SIZE = HEADER_V2.size
TAG_SIZE = 16
CHUNK_SIZE = 1024 * 1024
MAX_KEYFILE_SIZE = 16 * 1024
MIN_PASSWORD_LENGTH = 12
DEFAULT_ITERATIONS = 600_000  # retained for compatibility with legacy keyfile tooling
LEGACY_PBKDF2_MIN_ITERATIONS = 10_000
LEGACY_PBKDF2_MAX_ITERATIONS = 2_000_000
ARGON2_MEMORY_KIB = 64 * 1024
ARGON2_ITERATIONS = 3
ARGON2_LANES = 4
ARGON2_MIN_MEMORY_KIB = 64 * 1024
ARGON2_MIN_ITERATIONS = 3
ARGON2_MAX_MEMORY_KIB = 128 * 1024
ARGON2_MAX_ITERATIONS = 4
ARGON2_MAX_LANES = 4
KEY_AAD_V1 = b"tarklone-key-v1"
KEY_AAD_V2 = b"tarklone-key-v2"
NAME_KEY_SALT = b"tarklone-name-v1"
NAME_KEY_INFO = b"aes-siv"
FILE_KEY_INFO = b"tarklone-file-content-v2"
MAX_NAME_TOKEN_LEN = 240


def _normalize_rel(path: str) -> str:
    value = path.replace("\\", "/").strip("/")
    parts = [part for part in value.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise CryptoError(f"Ungültiger Pfad: {path!r}")
    return "/".join(parts)


def redact_encrypted_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip("/")
    root, separator, _rest = normalized.partition("/")
    if root.casefold() == ENCRYPTED_DIR.casefold() and not separator:
        return ENCRYPTED_DIR
    if root.casefold() == ENCRYPTED_DIR.casefold() and separator:
        return f"{ENCRYPTED_DIR}/<redacted>"
    return path


class CryptoError(RuntimeError):
    pass


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise CryptoError("Ungültige Schlüsseldatei.") from exc


def _derive_legacy_kek(password: str, salt: bytes, iterations: int) -> bytes:
    if not password:
        raise CryptoError("Das Passwort darf nicht leer sein.")
    if not LEGACY_PBKDF2_MIN_ITERATIONS <= iterations <= LEGACY_PBKDF2_MAX_ITERATIONS:
        raise CryptoError("Ungültige KDF-Parameter.")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(password.encode("utf-8"))


def _derive_kek(password: str, salt: bytes) -> bytes:
    if not password:
        raise CryptoError("Das Passwort darf nicht leer sein.")
    try:
        return Argon2id(
            salt=salt,
            length=32,
            iterations=ARGON2_ITERATIONS,
            lanes=ARGON2_LANES,
            memory_cost=ARGON2_MEMORY_KIB,
        ).derive(password.encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise CryptoError("Argon2id konnte nicht initialisiert werden.") from exc


def parse_file_header(data: bytes) -> tuple[int, int, bytes, bytes, int, int]:
    """Returns (version, plaintext_size, file_id, nonce, header_size, revision)."""
    if len(data) < HEADER_PREFIX.size:
        raise CryptoError("Verschlüsselte Datei hat keinen vollständigen Header.")
    try:
        magic, version = HEADER_PREFIX.unpack(data[: HEADER_PREFIX.size])
    except struct.error as exc:
        raise CryptoError("Ungültiger Dateiheader.") from exc
    if magic != MAGIC:
        raise CryptoError("Unbekanntes Format der verschlüsselten Datei.")
    if version == LEGACY_VERSION:
        header_struct = HEADER_V1
    elif version == VERSION:
        header_struct = HEADER_V2
    else:
        raise CryptoError("Unbekannte Version der verschlüsselten Datei.")
    if len(data) < header_struct.size:
        raise CryptoError("Verschlüsselte Datei hat keinen vollständigen Header.")
    try:
        values = header_struct.unpack(data[: header_struct.size])
    except struct.error as exc:
        raise CryptoError("Ungültiger Dateiheader.") from exc
    if version == LEGACY_VERSION:
        _magic, _version, plain_size, nonce = values
        file_id = b""
        revision = 0
    else:
        _magic, _version, plain_size, revision, random_file_id, nonce = values
        file_id = struct.pack(">Q", revision) + random_file_id
    return version, plain_size, file_id, nonce, header_struct.size, revision


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
        keyring_username: str = "crypto-master",
    ):
        self.local_root = Path(local_root).resolve()
        self.encrypted_dir = self.local_root / ENCRYPTED_DIR
        self.keyfile = self.encrypted_dir / KEYFILE_NAME
        self.iterations = iterations
        self.cache_in_keyring = cache_in_keyring
        self.keyring_username = keyring_username
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
        return normalized.casefold() == f"{ENCRYPTED_DIR}/{KEYFILE_NAME}".casefold()

    def is_encrypted_path(self, path: str) -> bool:
        normalized = path.replace("\\", "/").strip("/")
        root, separator, _rest = normalized.partition("/")
        return (
            bool(separator)
            and root.casefold() == ENCRYPTED_DIR.casefold()
            and not self.is_keyfile(normalized)
        )

    def ensure_folder(self) -> Path:
        self.encrypted_dir.mkdir(parents=True, exist_ok=True)
        return self.encrypted_dir

    def set_password(self, password: str, *, allow_existing: bool = False) -> None:
        if self.keyfile.exists():
            raise CryptoError("Es ist bereits ein Passwort gesetzt.")
        self._validate_new_password(password)
        if self.encrypted_dir.exists() and not allow_existing:
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
        self._validate_new_password(new_password)
        master = self._unwrap_keyfile(old_password)
        self._write_keyfile(master, new_password)
        self.master_key = master
        self._name_key_cache = None
        self._cache_master(master)

    @staticmethod
    def _validate_new_password(password: str) -> None:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise CryptoError(
                f"Das Passwort muss mindestens {MIN_PASSWORD_LENGTH} Zeichen lang sein. "
                "Verwende ein einmaliges Passwort oder eine lange Passphrase."
            )

    def unlock(self, password: str) -> None:
        if not self.keyfile.exists():
            raise CryptoError("Es ist noch kein Passwort gesetzt.")
        data = self._read_keyfile()
        master = self._unwrap_keyfile(password, data)
        if data.get("version") == 1:
            # Upgrade the wrapped key after a successful legacy unlock. The
            # content key stays the same, so files and encrypted names remain
            # compatible while the password wrapper moves to Argon2id.
            try:
                self._write_keyfile(master, password)
            except OSError:
                # A valid legacy keyfile is still usable if the local volume is
                # temporarily read-only; a later password change can upgrade it.
                pass
        self.master_key = master
        self._name_key_cache = None
        self._cache_master(master)

    def load_cached_key(self) -> bool:
        if not self.keyfile.exists():
            return False
        try:
            self._read_keyfile()
            expected_fingerprint = self._keyfile_fingerprint()
            import keyring

            raw = keyring.get_password("tarklone", self.keyring_username)
        except Exception:
            return False
        if not raw:
            return False
        try:
            cached = json.loads(raw)
            if not isinstance(cached, dict) or cached.get("version") != 1:
                return False
            fingerprint = str(cached.get("keyfile_sha256", ""))
            if not hmac.compare_digest(fingerprint, expected_fingerprint):
                return False
            master = _unb64(str(cached.get("master_key", "")))
        except (CryptoError, OSError, ValueError, TypeError):
            return False
        if len(master) != 32:
            return False
        self.master_key = master
        self._name_key_cache = None
        return True

    def _keyfile_fingerprint(self) -> str:
        try:
            if self.keyfile.stat().st_size > MAX_KEYFILE_SIZE:
                raise CryptoError("Schlüsseldatei überschreitet die erlaubte Größe.")
            return hashlib.sha256(self.keyfile.read_bytes()).hexdigest()
        except OSError as exc:
            raise CryptoError("Schlüsseldatei konnte nicht gelesen werden.") from exc

    def clear_cached_key(self) -> bool:
        if not self.cache_in_keyring:
            return True
        try:
            import keyring

            try:
                keyring.delete_password("tarklone", self.keyring_username)
            except Exception:
                # Some backends raise when an item is already absent. Verify
                # the state below rather than treating that as success/failure.
                pass
            return keyring.get_password("tarklone", self.keyring_username) is None
        except Exception:
            return False

    def lock(self, *, clear_cache: bool = False) -> bool:
        self.master_key = None
        self._name_key_cache = None
        return self.clear_cached_key() if clear_cache else True

    def reset_key(self, *, cache_already_cleared: bool = False) -> None:
        """Löscht den lokalen Schlüssel und den Keyring-Cache.

        Die verschlüsselten Remote-Dateien werden vom Aufrufer entfernt; die
        lokalen Klartextdateien bleiben unangetastet.
        """
        if not cache_already_cleared and not self.clear_cached_key():
            raise CryptoError(
                "Der Schlüssel konnte nicht sicher aus dem System-Keyring entfernt "
                "werden. Das lokale Keyfile wurde beibehalten."
            )
        self.keyfile.unlink(missing_ok=True)
        self.master_key = None
        self._name_key_cache = None

    def _cache_master(self, master: bytes) -> bool:
        if not self.cache_in_keyring:
            return True
        try:
            import keyring

            cached = {
                "version": 1,
                "keyfile_sha256": self._keyfile_fingerprint(),
                "master_key": _b64(master),
            }
            raw = json.dumps(cached, separators=(",", ":"))
            keyring.set_password("tarklone", self.keyring_username, raw)
            return keyring.get_password("tarklone", self.keyring_username) == raw
        except Exception:
            # The key remains available for this process; next launch will ask
            # for the passphrase if the OS keyring is unavailable.
            return False

    def _read_keyfile(self) -> dict[str, Any]:
        try:
            if self.keyfile.stat().st_size > MAX_KEYFILE_SIZE:
                raise CryptoError("Schlüsseldatei überschreitet die erlaubte Größe.")
        except OSError as exc:
            raise CryptoError("Schlüsseldatei konnte nicht gelesen werden.") from exc
        try:
            data = json.loads(self.keyfile.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CryptoError("Schlüsseldatei konnte nicht gelesen werden.") from exc
        if not isinstance(data, dict) or data.get("version") not in (1, 2):
            raise CryptoError("Unbekanntes Format der Schlüsseldatei.")
        expected_kdf = "pbkdf2-sha256" if data["version"] == 1 else "argon2id"
        if data.get("kdf") != expected_kdf:
            raise CryptoError("Nicht unterstütztes KDF-Verfahren.")
        return data

    def _unwrap_keyfile(
        self, password: str, data: dict[str, Any] | None = None
    ) -> bytes:
        data = data or self._read_keyfile()
        try:
            salt = _unb64(str(data["salt"]))
            nonce = _unb64(str(data["nonce"]))
            wrapped = _unb64(str(data["wrapped_key"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise CryptoError("Schlüsseldatei ist unvollständig.") from exc
        if len(salt) != 16 or len(nonce) != 12 or len(wrapped) != 32 + TAG_SIZE:
            raise CryptoError("Ungültige Längen in der Schlüsseldatei.")
        if data["version"] == 1:
            try:
                iterations = int(data["iterations"])
            except (KeyError, TypeError, ValueError) as exc:
                raise CryptoError("Ungültige PBKDF2-Parameter.") from exc
            kek = _derive_legacy_kek(password, salt, iterations)
            aad = KEY_AAD_V1
        else:
            try:
                memory_kib = int(data["memory_kib"])
                iterations = int(data["iterations"])
                lanes = int(data["lanes"])
            except (KeyError, TypeError, ValueError) as exc:
                raise CryptoError("Ungültige Argon2id-Parameter.") from exc
            if not (
                max(ARGON2_MIN_MEMORY_KIB, 8 * lanes)
                <= memory_kib
                <= ARGON2_MAX_MEMORY_KIB
                and ARGON2_MIN_ITERATIONS
                <= iterations
                <= ARGON2_MAX_ITERATIONS
                and 1 <= lanes <= ARGON2_MAX_LANES
            ):
                raise CryptoError("Argon2id-Parameter liegen außerhalb sicherer Grenzen.")
            try:
                kek = Argon2id(
                    salt=salt,
                    length=32,
                    iterations=iterations,
                    lanes=lanes,
                    memory_cost=memory_kib,
                ).derive(password.encode("utf-8"))
            except (TypeError, ValueError) as exc:
                raise CryptoError("Ungültige Argon2id-Parameter.") from exc
            aad = KEY_AAD_V2
        try:
            master = AESGCM(kek).decrypt(nonce, wrapped, aad)
        except InvalidTag as exc:
            raise CryptoError("Falsches Passwort oder beschädigte Schlüsseldatei.") from exc
        if len(master) != 32:
            raise CryptoError("Ungültige Schlüssellänge.")
        return master

    def _write_keyfile(self, master: bytes, password: str) -> None:
        self.ensure_folder()
        salt = os.urandom(16)
        nonce = os.urandom(12)
        kek = _derive_kek(password, salt)
        wrapped = AESGCM(kek).encrypt(nonce, master, KEY_AAD_V2)
        now = datetime.now().isoformat(timespec="seconds")
        data = {
            "version": 2,
            "kdf": "argon2id",
            "memory_kib": ARGON2_MEMORY_KIB,
            "iterations": ARGON2_ITERATIONS,
            "lanes": ARGON2_LANES,
            "salt": _b64(salt),
            "nonce": _b64(nonce),
            "wrapped_key": _b64(wrapped),
            "name_encryption": 1,
            "updated_at": now,
        }
        payload = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        fd, temp_name = tempfile.mkstemp(
            prefix=".tarklone-key-", suffix=".tmp", dir=self.encrypted_dir
        )
        temp = Path(temp_name)
        try:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.keyfile)
            if os.name != "nt":
                self.keyfile.chmod(0o600)
        finally:
            temp.unlink(missing_ok=True)

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
        if any(ord(char) < 32 or char in '<>:"|?*' for char in name):
            raise CryptoError(
                "Datei- oder Ordnernamen mit Windows-Sonderzeichen werden nicht unterstützt."
            )
        if name.endswith((".", " ")):
            raise CryptoError(
                "Datei- oder Ordnernamen dürfen nicht mit Punkt oder Leerzeichen enden."
            )
        device_name = name.split(".", 1)[0].rstrip(" .").upper()
        if device_name in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}:
            raise CryptoError("Dieser Datei- oder Ordnername ist unter Windows reserviert.")
        if len(device_name) == 4 and device_name[:3] in {"COM", "LPT"}:
            if device_name[3] in "123456789¹²³":
                raise CryptoError("Dieser Datei- oder Ordnername ist unter Windows reserviert.")

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
        root, separator, _rest = normalized.partition("/")
        if not separator or root != ENCRYPTED_DIR:
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
        root, separator, _rest = normalized.partition("/")
        if not separator or root != ENCRYPTED_DIR:
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

    def _content_key(self, file_id: bytes) -> bytes:
        master = self._require_key()
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=file_id,
            info=FILE_KEY_INFO,
        )
        return hkdf.derive(master)

    @staticmethod
    def _content_aad(header: bytes, logical_path: str) -> bytes:
        normalized = _normalize_rel(logical_path)
        if not normalized.startswith(ENCRYPTED_DIR + "/"):
            raise CryptoError("Der Dateipfad muss unter encrypted/ liegen.")
        return header + b"\x00tarklone-path-v2\x00" + normalized.encode("utf-8")

    def encrypt_file(
        self,
        source: Path,
        destination: Path,
        *,
        logical_path: str,
        generation: int = 1,
    ) -> int:
        self._require_key()
        if not 1 <= generation <= 0xFFFFFFFFFFFFFFFF:
            raise CryptoError("Ungültige Verschlüsselungsgeneration.")
        plain_size = source.stat().st_size
        random_file_id = os.urandom(8)
        file_id = struct.pack(">Q", generation) + random_file_id
        nonce = os.urandom(12)
        header = HEADER_V2.pack(
            MAGIC,
            VERSION,
            plain_size,
            generation,
            random_file_id,
            nonce,
        )
        aad = self._content_aad(header, logical_path)
        key = self._content_key(file_id)
        encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
        encryptor.authenticate_additional_data(aad)
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

    def decrypt_file(
        self,
        source: Path,
        destination: Path,
        *,
        logical_path: str,
    ) -> int:
        master = self._require_key()
        size = source.stat().st_size
        if size < HEADER_V1.size + TAG_SIZE:
            raise CryptoError("Verschlüsselte Datei ist zu kurz.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd: int | None = None
        stage: Path | None = None
        try:
            with source.open("rb") as src:
                prefix = src.read(HEADER_PREFIX.size)
                if len(prefix) != HEADER_PREFIX.size:
                    raise CryptoError("Verschlüsselte Datei hat keinen vollständigen Header.")
                try:
                    magic, version = HEADER_PREFIX.unpack(prefix)
                except struct.error as exc:
                    raise CryptoError("Ungültiger Dateiheader.") from exc
                header_struct = HEADER_V1 if version == LEGACY_VERSION else HEADER_V2
                if magic != MAGIC or version not in (LEGACY_VERSION, VERSION):
                    raise CryptoError("Unbekanntes Format der verschlüsselten Datei.")
                header = prefix + src.read(header_struct.size - len(prefix))
                (
                    version,
                    plain_size,
                    file_id,
                    nonce,
                    header_size,
                    _generation,
                ) = parse_file_header(header)
                expected = header_size + plain_size + TAG_SIZE
                if size != expected:
                    raise CryptoError("Verschlüsselte Datei hat eine unerwartete Größe.")
                src.seek(size - TAG_SIZE)
                tag = src.read(TAG_SIZE)
                if len(tag) != TAG_SIZE:
                    raise CryptoError("Verschlüsselte Datei hat keinen vollständigen Auth-Tag.")
                src.seek(header_size)
                if version == LEGACY_VERSION:
                    key = master
                    aad = header
                else:
                    key = self._content_key(file_id)
                    aad = self._content_aad(header, logical_path)
                decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
                decryptor.authenticate_additional_data(aad)
                fd, stage_name = tempfile.mkstemp(
                    prefix=".tarklone-decrypt-",
                    suffix=".tmp",
                    dir=destination.parent,
                )
                stage = Path(stage_name)
                if os.name != "nt":
                    os.fchmod(fd, 0o600)
                dst = os.fdopen(fd, "wb")
                fd = None
                with dst:
                    written = 0
                    remaining = plain_size
                    while remaining > 0:
                        chunk = src.read(min(CHUNK_SIZE, remaining))
                        if not chunk:
                            raise CryptoError("Verschlüsselte Datei ist unvollständig.")
                        plaintext = decryptor.update(chunk)
                        dst.write(plaintext)
                        written += len(plaintext)
                        remaining -= len(chunk)
                    try:
                        final = decryptor.finalize()
                    except InvalidTag as exc:
                        raise CryptoError(
                            "Entschlüsselung fehlgeschlagen: Datei wurde manipuliert "
                            "oder der Schlüssel ist falsch."
                        ) from exc
                    dst.write(final)
                    written += len(final)
                    dst.flush()
                    os.fsync(dst.fileno())
                if written != plain_size:
                    raise CryptoError("Entschlüsselte Datei hat eine unerwartete Größe.")
            os.replace(stage, destination)
            stage = None
            if os.name != "nt":
                destination.chmod(0o600)
        finally:
            if fd is not None:
                os.close(fd)
            if stage is not None:
                stage.unlink(missing_ok=True)
        return plain_size
