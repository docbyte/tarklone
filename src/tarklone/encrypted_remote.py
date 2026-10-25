from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from pathlib import Path

from .crypto import (
    ENCRYPTED_DIR,
    HEADER_SIZE,
    KEYFILE_NAME,
    TAG_SIZE,
    VERSION,
    CryptoError,
    EncryptionManager,
    parse_file_header,
    redact_encrypted_path,
)
from .sync import RemoteEntry, SyncError
from .state import EntryState, SyncState
from .tarkle import TarkleRemote

logger = logging.getLogger("tarklone.crypto")
REMOTE_KEYFILE = f"{ENCRYPTED_DIR}/{KEYFILE_NAME}"


class EncryptedStateAdapter:
    """Stores encrypted-folder paths as deterministic name tokens in SQLite."""

    def __init__(self, state: SyncState, encryption: EncryptionManager):
        self.state = state
        self.encryption = encryption

    def _stored_path(self, path: str) -> str:
        if self.encryption.is_encrypted_path(path):
            if not self.encryption.unlocked:
                raise SyncError("Verschlüsselter Sync-Zustand ist gesperrt.")
            return self.encryption.encrypt_path(path)
        return path

    def all(self) -> dict[str, EntryState]:
        entries = self.state.all()
        result: dict[str, EntryState] = {}
        migrated = False
        for stored_path, entry in entries.items():
            if not self.encryption.is_encrypted_path(stored_path):
                result[stored_path] = replace(entry, path=stored_path)
                continue
            try:
                logical_path = self.encryption.decrypt_path(stored_path)
            except CryptoError:
                # Older versions stored plaintext names in SQLite. Replace
                # those keys with the same deterministic tokens used remotely.
                logical_path = stored_path
                token_path = self.encryption.encrypt_path(logical_path)
                existing = self.state.get(token_path)
                if existing is None:
                    self.state.upsert(replace(entry, path=token_path))
                self.state.delete(stored_path)
                migrated = True
            result[logical_path] = replace(entry, path=logical_path)
        if migrated:
            self.state.compact_sensitive_pages()
        return result

    def get(self, path: str) -> EntryState | None:
        stored_path = self._stored_path(path)
        entry = self.state.get(stored_path)
        if entry is None and stored_path != path:
            # One-time compatibility for plaintext path rows from older builds.
            legacy = self.state.get(path)
            if legacy is not None:
                self.state.upsert(replace(legacy, path=stored_path))
                self.state.delete(path)
                self.state.compact_sensitive_pages()
                entry = legacy
        return replace(entry, path=path) if entry is not None else None

    def upsert(self, entry: EntryState) -> None:
        self.state.upsert(replace(entry, path=self._stored_path(entry.path)))

    def delete(self, path: str) -> None:
        self.state.delete(self._stored_path(path))
        if self.encryption.is_encrypted_path(path):
            self.state.delete(path)

    def delete_prefix(self, prefix: str) -> None:
        normalized = prefix.replace("\\", "/").strip("/")
        if normalized == ENCRYPTED_DIR or not self.encryption.is_encrypted_path(normalized):
            self.state.delete_prefix(normalized)
            return
        for stored_path in self.state.all():
            try:
                logical_path = self.encryption.decrypt_path(stored_path)
            except CryptoError:
                logical_path = stored_path
            if logical_path == normalized or logical_path.startswith(normalized + "/"):
                self.state.delete(stored_path)

    def get_meta(self, key: str, default: str = "") -> str:
        return self.state.get_meta(key, default)

    def set_meta(self, key: str, value: str) -> None:
        self.state.set_meta(key, value)

    def close(self) -> None:
        self.state.close()


class EncryptionAdapter:
    """Transparente Ver-/Entschlüsselung für Pfade unter ``encrypted/``.

    Der lokale Ordner bleibt im Klartext nutzbar. Beim Upload wird der
    Ciphertext übertragen, beim Download wieder entschlüsselt. Die Schlüsseldatei
    ``encrypted/.tarklone-key.json`` wird unverschlüsselt mitgesynct.
    """

    def __init__(self, remote: TarkleRemote, encryption: EncryptionManager):
        self.remote = remote
        self.encryption = encryption
        self.supports_empty_folders = getattr(
            remote, "supports_empty_folders", True
        )
        self._logical_sizes: dict[str, tuple[int, int, bytes]] = {}
        self._legacy_prefixes: set[str] = set()

    def close(self) -> None:
        self.remote.close()

    def _temp_path(self, name: str) -> Path:
        temp_dir = self.encryption.local_root / ".tarklone" / "tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        return temp_dir / f"{name}.{uuid.uuid4().hex}.tmp"

    def _logical_size(self, entry: RemoteEntry) -> tuple[int, int, bytes]:
        cache_key = f"{entry.entry_id}:{entry.size}"
        cached = self._logical_sizes.get(cache_key)
        if cached is not None:
            return cached
        header = self.remote.download_range(entry, 0, HEADER_SIZE)
        if len(header) != HEADER_SIZE:
            raise SyncError(
                "Verschlüsselte Datei hat keinen gültigen Header: "
                f"{redact_encrypted_path(entry.path)}"
            )
        try:
            version, plain_size, file_id, _nonce, header_size, _generation = (
                parse_file_header(header)
            )
        except CryptoError as exc:
            raise SyncError(
                "Datei im encrypted-Ordner ist nicht tarklone-verschlüsselt: "
                f"{redact_encrypted_path(entry.path)}"
            ) from exc
        expected = header_size + plain_size + TAG_SIZE
        if entry.size and entry.size != expected:
            raise SyncError(
                "Verschlüsselte Datei hat eine unerwartete Größe: "
                f"{redact_encrypted_path(entry.path)}"
            )
        self._logical_sizes[cache_key] = (plain_size, version, file_id)
        return plain_size, version, file_id

    def _is_legacy(self, path: str) -> bool:
        return any(
            path == prefix or path.startswith(prefix + "/")
            for prefix in self._legacy_prefixes
        )

    def _migrate_legacy_names(self, entries: list[RemoteEntry]) -> list[RemoteEntry]:
        """Verschiebt vorhandene Klartextnamen unter encrypted/ auf Token-Namen.

        Der bereits verschlüsselte Inhalt wird dabei nur kopiert, nicht neu
        verschlüsselt. Das passiert einmalig beim ersten Sync nach dem Update.
        """
        legacy_files: list[RemoteEntry] = []
        legacy_dirs: list[RemoteEntry] = []
        for entry in entries:
            if not self.encryption.is_encrypted_path(entry.path):
                continue
            try:
                self.encryption.decrypt_path(entry.path)
            except CryptoError:
                if entry.kind == "file":
                    legacy_files.append(entry)
                else:
                    legacy_dirs.append(entry)
        if not legacy_files and not legacy_dirs:
            return entries

        logger.warning(
            "Migriere %d Datei(en) und %d Ordner auf verschlüsselte Namen.",
            len(legacy_files),
            len(legacy_dirs),
        )
        existing = {entry.path for entry in entries}
        for entry in legacy_files:
            try:
                token_path = self.encryption.encrypt_path(entry.path)
            except CryptoError as exc:
                logger.warning(
                    "Namensmigration übersprungen (%s): %s",
                    type(exc).__name__,
                    redact_encrypted_path(entry.path),
                )
                continue
            if token_path in existing:
                continue
            temp = self._temp_path("legacy")
            try:
                self.remote.download(entry, temp)
                self.remote.upload(temp, token_path)
                self.remote.delete(entry)
                existing.add(token_path)
                logger.info("Name migriert: %s", redact_encrypted_path(entry.path))
            except Exception as exc:
                logger.error(
                    "Namensmigration fehlgeschlagen (%s): %s",
                    type(exc).__name__,
                    redact_encrypted_path(entry.path),
                )
            finally:
                temp.unlink(missing_ok=True)

        if legacy_dirs:
            try:
                fresh = self.remote.list_tree()
            except Exception:
                fresh = entries
            paths = {entry.path for entry in fresh}
            for entry in sorted(
                legacy_dirs, key=lambda item: item.path.count("/"), reverse=True
            ):
                if entry.path not in paths:
                    continue
                if any(path.startswith(entry.path + "/") for path in paths):
                    continue
                try:
                    self.remote.delete(entry)
                    logger.info(
                        "Leeren Legacy-Ordner entfernt: %s",
                        redact_encrypted_path(entry.path),
                    )
                except Exception as exc:
                    logger.error(
                        "Legacy-Ordner konnte nicht entfernt werden (%s): %s",
                        type(exc).__name__,
                        redact_encrypted_path(entry.path),
                    )
        return self.remote.list_tree()

    def list_tree(self) -> list[RemoteEntry]:
        entries = self.remote.list_tree()
        target_path = str(getattr(self.remote, "target_path", "") or "")
        target_parts = target_path.replace("\\", "/").strip("/").split("/")
        if (
            self.encryption.enabled
            and any(
                part.casefold() == ENCRYPTED_DIR.casefold()
                for part in target_parts
                if part
            )
        ):
            raise SyncError(
                "Der Remote-Sync-Ordner darf nicht innerhalb des reservierten "
                "Verschlüsselungsordners liegen. Sonst könnten normale Dateien "
                "unverschlüsselt in encrypted/ landen."
            )
        has_mis_cased_root = any(
            entry.path.split("/", 1)[0].casefold() == ENCRYPTED_DIR.casefold()
            and entry.path.split("/", 1)[0] != ENCRYPTED_DIR
            for entry in entries
        )
        has_keyfile = any(
            entry.path.casefold() == REMOTE_KEYFILE.casefold()
            and entry.kind == "file"
            for entry in entries
        )
        if has_mis_cased_root and (self.encryption.enabled or has_keyfile):
            raise SyncError(
                "Der Remote-Verschlüsselungsordner muss exakt 'encrypted' heißen. "
                "Der Sync wurde abgebrochen, damit keine Ciphertexte als Klartext "
                "behandelt werden."
            )
        if not self.encryption.enabled:
            # Auf einem neuen Rechner kann die Schlüsseldatei remote vorhanden
            # sein. Dann nur diese und normale Dateien übertragen, aber keine
            # Ciphertext-Dateien als Klartext herunterladen.
            if not has_keyfile:
                return entries
            return [
                entry
                for entry in entries
                if entry.kind != "file"
                or not self.encryption.is_encrypted_path(entry.path)
                or self.encryption.is_keyfile(entry.path)
            ]
        if self.encryption.unlocked:
            entries = self._migrate_legacy_names(entries)

        self._legacy_prefixes.clear()
        result: list[RemoteEntry] = []
        for entry in entries:
            if not self.encryption.is_encrypted_path(entry.path):
                result.append(entry)
                continue
            remote_path = entry.path
            try:
                plain_path = self.encryption.decrypt_path(remote_path)
            except CryptoError:
                # Kompatibilität mit Dateien, die vor der Namensverschlüsselung
                # angelegt wurden (oder bei denen die Migration nicht ging).
                plain_path = remote_path
                self._legacy_prefixes.add(remote_path)
                logger.warning(
                    "Klartextname im encrypted-Ordner (Legacy): %s",
                    redact_encrypted_path(remote_path),
                )
            entry.remote_path = remote_path
            if entry.kind == "file" and self.encryption.unlocked:
                # Der logische Größenheader wird über die Remote-ID gelesen;
                # entry.remote_path ist der echte Remote-Pfad.
                entry.logical_size, content_version, file_id = self._logical_size(entry)
                entry.needs_reencrypt = content_version != VERSION
                if content_version == VERSION and len(file_id) == 16:
                    entry.crypto_generation = int.from_bytes(file_id[:8], "big")
                    entry.content_hash = (
                        f"tarklone-v2:{entry.crypto_generation}:{file_id[8:].hex()}"
                    )
            elif entry.kind == "file":
                logger.warning(
                    "Überspringe verschlüsselte Datei (Verschlüsselung gesperrt): %s",
                    redact_encrypted_path(plain_path),
                )
                continue
            entry.path = plain_path
            result.append(entry)
        return result

    def download(self, entry: RemoteEntry, destination: Path) -> None:
        if self.encryption.is_encrypted_path(entry.path) and not self.encryption.enabled:
            raise SyncError(
                "Verschlüsselte Datei gefunden, aber kein lokales Passwort gesetzt. "
                "Bitte zuerst das Passwort in den Einstellungen eingeben."
            )
        if not self.encryption.enabled or not self.encryption.is_encrypted_path(entry.path):
            self.remote.download(entry, destination)
            return
        if not self.encryption.unlocked:
            raise SyncError(
                "Die Verschlüsselung ist gesperrt. Bitte das Passwort eingeben."
            )
        temp = self._temp_path(destination.name)
        try:
            self.remote.download(entry, temp)
            self.encryption.decrypt_file(
                temp,
                destination,
                logical_path=entry.path,
            )
        finally:
            temp.unlink(missing_ok=True)

    def upload(
        self,
        local_path: Path,
        remote_path: str,
        *,
        mtime: float | None = None,
        remote_entry: RemoteEntry | None = None,
        crypto_generation: int = 1,
    ) -> RemoteEntry | None:
        if self.encryption.is_encrypted_path(remote_path) and not self.encryption.enabled:
            raise SyncError(
                "Der Ordner encrypted ist für die Verschlüsselung reserviert. "
                "Bitte zuerst ein Passwort in den Einstellungen setzen."
            )
        if not self.encryption.enabled or not self.encryption.is_encrypted_path(remote_path):
            return self.remote.upload(
                local_path,
                remote_path,
                mtime=mtime,
                remote_entry=remote_entry,
            )
        if not self.encryption.unlocked:
            raise SyncError(
                "Die Verschlüsselung ist gesperrt. Bitte das Passwort eingeben."
            )
        plain_size = local_path.stat().st_size
        if self._is_legacy(remote_path):
            token_path = remote_path
        else:
            token_path = self.encryption.encrypt_path(remote_path)
        temp = self._temp_path(local_path.name)
        try:
            self.encryption.encrypt_file(
                local_path,
                temp,
                logical_path=remote_path,
                generation=crypto_generation,
            )
            with temp.open("rb") as encrypted_file:
                header = encrypted_file.read(HEADER_SIZE)
            version, _size, file_id, _nonce, _header_size, generation = (
                parse_file_header(header)
            )
            if version != VERSION or len(file_id) != 16:
                raise SyncError("Neu verschlüsselter Inhalt hat ein ungültiges Format.")
            cipher_size = temp.stat().st_size
            uploaded = self.remote.upload(
                temp,
                token_path,
                mtime=mtime,
                remote_entry=remote_entry,
            )
        finally:
            temp.unlink(missing_ok=True)
        if uploaded is None:
            uploaded = RemoteEntry(path=remote_path, size=cipher_size)
        return RemoteEntry(
            path=remote_path,
            kind="file",
            size=uploaded.size or cipher_size,
            mtime=0.0,
            entry_id=uploaded.entry_id,
            parent_id=uploaded.parent_id,
            logical_size=plain_size,
            content_hash=f"tarklone-v2:{generation}:{file_id[8:].hex()}",
            crypto_generation=generation,
        )

    def delete(self, entry: RemoteEntry) -> None:
        self.remote.delete(entry)

    def create_folder(self, remote_path: str) -> RemoteEntry | None:
        return self.remote.create_folder(remote_path)
