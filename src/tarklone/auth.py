"""Sichere Ablage des Tarkle-API-Keys.

Der Key wird bevorzugt im OS-Keyring gespeichert. Für headless Systeme gibt es
einen Datei-Fallback, der unter Windows zusätzlich mit DPAPI an das aktuelle
Benutzerkonto gebunden wird.
"""

from __future__ import annotations

import base64
import logging
import os
import stat
import tempfile
from pathlib import Path

from .config import data_dir, is_api_key, normalize_api_key

SERVICE_NAME = "tarklone"
API_KEY_USERNAME = "api-key"
_DPAPI_PREFIX = b"TARKLONE-DPAPI1:"
logger = logging.getLogger("tarklone.auth")


class ApiKeyStoreError(RuntimeError):
    pass


class ApiKeyMissing(RuntimeError):
    pass


def mask_api_key(value: str) -> str:
    key = normalize_api_key(value)
    if not key:
        return ""
    if len(key) <= 8:
        return "tk_…"
    return f"{key[:3]}…{key[-4:]}"


class ApiKeyStore:
    def __init__(
        self,
        service: str = SERVICE_NAME,
        username: str = API_KEY_USERNAME,
        fallback_path: Path | None = None,
    ):
        self.service = service
        self.username = username
        self.fallback_path = fallback_path or (data_dir() / "api-key.json")

    @staticmethod
    def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
        if os.name != "nt":
            raise ApiKeyStoreError("DPAPI ist nur unter Windows verfügbar.")
        import ctypes
        from ctypes import wintypes

        class DataBlob(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(wintypes.BYTE)),
            ]

        def make_blob(value: bytes):
            buffer = (wintypes.BYTE * len(value)).from_buffer_copy(value)
            return DataBlob(
                len(value), ctypes.cast(buffer, ctypes.POINTER(wintypes.BYTE))
            ), buffer

        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        input_blob, _input_buffer = make_blob(data)
        output_blob = DataBlob()
        flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
        if decrypt:
            description = wintypes.LPWSTR()
            crypt32.CryptUnprotectData.argtypes = [
                ctypes.POINTER(DataBlob),
                ctypes.POINTER(wintypes.LPWSTR),
                ctypes.POINTER(DataBlob),
                wintypes.LPVOID,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(DataBlob),
            ]
            crypt32.CryptUnprotectData.restype = wintypes.BOOL
            ok = crypt32.CryptUnprotectData(
                ctypes.byref(input_blob),
                ctypes.byref(description),
                None,
                None,
                None,
                flags,
                ctypes.byref(output_blob),
            )
        else:
            crypt32.CryptProtectData.argtypes = [
                ctypes.POINTER(DataBlob),
                wintypes.LPCWSTR,
                ctypes.POINTER(DataBlob),
                wintypes.LPVOID,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(DataBlob),
            ]
            crypt32.CryptProtectData.restype = wintypes.BOOL
            ok = crypt32.CryptProtectData(
                ctypes.byref(input_blob),
                "tarklone API key",
                None,
                None,
                None,
                flags,
                ctypes.byref(output_blob),
            )
        if not ok:
            raise ApiKeyStoreError(
                f"Windows-DPAPI fehlgeschlagen (Fehler {ctypes.get_last_error()})."
            )
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
            kernel32.LocalFree.restype = wintypes.HLOCAL
            kernel32.LocalFree(ctypes.cast(output_blob.pbData, wintypes.HLOCAL))
            if decrypt and description:
                kernel32.LocalFree(ctypes.cast(description, wintypes.HLOCAL))

    def _encode_fallback(self, raw: str) -> bytes:
        payload = raw.encode("utf-8")
        if os.name == "nt":
            protected = self._dpapi(payload, decrypt=False)
            return _DPAPI_PREFIX + base64.b64encode(protected)
        return payload

    def _decode_fallback(self, content: bytes) -> str:
        if content.startswith(_DPAPI_PREFIX):
            try:
                protected = base64.b64decode(
                    content[len(_DPAPI_PREFIX) :], validate=True
                )
                return self._dpapi(protected, decrypt=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as exc:
                raise ApiKeyStoreError(
                    "DPAPI-API-Key-Fallback ist beschädigt."
                ) from exc
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ApiKeyStoreError("API-Key-Fallback ist beschädigt.") from exc

    def load(self) -> str | None:
        raw_keyring = None
        try:
            import keyring

            raw_keyring = keyring.get_password(self.service, self.username)
        except Exception:
            raw_keyring = None
        key = normalize_api_key(raw_keyring or "")
        if is_api_key(key):
            try:
                self.fallback_path.unlink(missing_ok=True)
            except OSError:
                pass
            return key

        try:
            metadata = self.fallback_path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                return None
            if os.name != "nt":
                self.fallback_path.chmod(0o600)
            raw_file = self._decode_fallback(self.fallback_path.read_bytes())
        except ApiKeyStoreError as exc:
            logger.warning("Gespeicherter API-Key ist nicht lesbar: %s", exc)
            return None
        except OSError:
            return None
        key = normalize_api_key(raw_file.strip())
        return key if is_api_key(key) else None

    def _save_fallback(self, raw: str) -> None:
        self.fallback_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            self.fallback_path.parent.chmod(0o700)
        payload = self._encode_fallback(raw)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.fallback_path.name}-",
            suffix=".tmp",
            dir=self.fallback_path.parent,
        )
        temp = Path(temp_name)
        try:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.fallback_path)
            if os.name != "nt":
                self.fallback_path.chmod(0o600)
        finally:
            temp.unlink(missing_ok=True)

    def save(self, api_key: str) -> None:
        key = normalize_api_key(api_key)
        if not is_api_key(key):
            raise ApiKeyStoreError(
                "Ungültiger Tarkle-API-Key. Der Key beginnt mit tk_."
            )
        stored = False
        try:
            import keyring

            keyring.set_password(self.service, self.username, key)
            stored = keyring.get_password(self.service, self.username) == key
        except Exception:
            stored = False
        if stored:
            try:
                self.fallback_path.unlink(missing_ok=True)
            except OSError:
                pass
            return

        # Kein brauchbarer Keyring: einen eventuell vorhandenen alten Wert
        # entfernen, damit nicht der veraltete Key Vorrang vor dem neuen hat.
        try:
            import keyring

            current = keyring.get_password(self.service, self.username)
        except Exception:
            current = None
            keyring = None
        if current is not None and keyring is not None:
            try:
                keyring.delete_password(self.service, self.username)
            except Exception:
                pass
            try:
                cleared = keyring.get_password(self.service, self.username) is None
            except Exception:
                cleared = False
            if not cleared:
                raise ApiKeyStoreError(
                    "Ein alter API-Key konnte nicht sicher aus dem Keyring "
                    "entfernt werden; der neue Key wurde nicht gespeichert."
                )
        self._save_fallback(key)

    def delete(self) -> bool:
        fallback_removed = True
        try:
            self.fallback_path.unlink(missing_ok=True)
        except OSError:
            fallback_removed = False
        keyring_empty = False
        try:
            import keyring

            try:
                keyring.delete_password(self.service, self.username)
            except Exception:
                pass
            keyring_empty = keyring.get_password(self.service, self.username) is None
        except Exception:
            keyring_empty = False
        return fallback_removed and keyring_empty


def load_api_key() -> str:
    key = ApiKeyStore().load()
    if not key:
        raise ApiKeyMissing(
            "Kein Tarkle-API-Key gespeichert. Bitte in den Einstellungen "
            "oder mit `tarklone login` einen Key eintragen."
        )
    return key


def legacy_token_path() -> Path:
    """Pfad des früheren OAuth-Token-Fallbacks (nur für Aufräumarbeiten)."""
    return data_dir() / "token.json"


def clear_legacy_oauth_token() -> None:
    """Entfernt OAuth-Reste früherer tarklone-Versionen."""
    try:
        legacy_token_path().unlink(missing_ok=True)
    except OSError:
        pass
    try:
        import keyring

        keyring.delete_password(SERVICE_NAME, "oauth")
    except Exception:
        pass
