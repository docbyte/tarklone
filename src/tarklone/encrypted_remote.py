from __future__ import annotations

import logging
import uuid
from pathlib import Path

from .crypto import (
    ENCRYPTED_DIR,
    HEADER,
    HEADER_SIZE,
    KEYFILE_NAME,
    MAGIC,
    TAG_SIZE,
    VERSION,
    CryptoError,
    EncryptionManager,
)
from .sync import RemoteEntry, SyncError
from .tarkle import TarkleRemote

logger = logging.getLogger("tarklone.crypto")
REMOTE_KEYFILE = f"{ENCRYPTED_DIR}/{KEYFILE_NAME}"


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
        self._logical_sizes: dict[str, int] = {}
        self._legacy_prefixes: set[str] = set()

    def close(self) -> None:
        self.remote.close()

    def _temp_path(self, name: str) -> Path:
        temp_dir = self.encryption.local_root / ".tarklone" / "tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        return temp_dir / f"{name}.{uuid.uuid4().hex}.tmp"

    def _logical_size(self, entry: RemoteEntry) -> int:
        cache_key = f"{entry.entry_id}:{entry.size}"
        cached = self._logical_sizes.get(cache_key)
        if cached is not None:
            return cached
        header = self.remote.download_range(entry, 0, HEADER_SIZE)
        if len(header) != HEADER_SIZE:
            raise SyncError(
                f"Verschlüsselte Datei hat keinen gültigen Header: {entry.path}"
            )
        magic, version, plain_size, _nonce = HEADER.unpack(header)
        if magic != MAGIC or version != VERSION:
            raise SyncError(
                f"Datei im encrypted-Ordner ist nicht tarklone-verschlüsselt: {entry.path}"
            )
        expected = HEADER_SIZE + plain_size + TAG_SIZE
        if entry.size and entry.size != expected:
            raise SyncError(
                f"Verschlüsselte Datei hat eine unerwartete Größe: {entry.path}"
            )
        self._logical_sizes[cache_key] = plain_size
        return plain_size

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
                logger.warning("Namensmigration übersprungen (%s): %s", exc, entry.path)
                continue
            if token_path in existing:
                continue
            temp = self._temp_path("legacy")
            try:
                self.remote.download(entry, temp)
                self.remote.upload(temp, token_path)
                self.remote.delete(entry)
                existing.add(token_path)
                logger.info("Name migriert: %s", entry.path)
            except Exception:
                logger.exception("Namensmigration fehlgeschlagen: %s", entry.path)
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
                    logger.info("Leeren Legacy-Ordner entfernt: %s", entry.path)
                except Exception:
                    logger.exception("Legacy-Ordner konnte nicht entfernt werden: %s", entry.path)
        return self.remote.list_tree()

    def list_tree(self) -> list[RemoteEntry]:
        entries = self.remote.list_tree()
        if not self.encryption.enabled:
            # Auf einem neuen Rechner kann die Schlüsseldatei remote vorhanden
            # sein. Dann nur diese und normale Dateien übertragen, aber keine
            # Ciphertext-Dateien als Klartext herunterladen.
            has_keyfile = any(
                entry.path == REMOTE_KEYFILE and entry.kind == "file"
                for entry in entries
            )
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
                    "Klartextname im encrypted-Ordner (Legacy): %s", remote_path
                )
            entry.remote_path = remote_path
            if entry.kind == "file" and self.encryption.unlocked:
                # Der logische Größenheader wird über die Remote-ID gelesen;
                # entry.remote_path ist der echte Remote-Pfad.
                entry.logical_size = self._logical_size(entry)
            elif entry.kind == "file":
                logger.warning(
                    "Überspringe verschlüsselte Datei (Verschlüsselung gesperrt): %s",
                    plain_path,
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
            self.encryption.decrypt_file(temp, destination)
        finally:
            temp.unlink(missing_ok=True)

    def upload(
        self,
        local_path: Path,
        remote_path: str,
        *,
        mtime: float | None = None,
        remote_entry: RemoteEntry | None = None,
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
            self.encryption.encrypt_file(local_path, temp)
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
        )

    def delete(self, entry: RemoteEntry) -> None:
        self.remote.delete(entry)

    def create_folder(self, remote_path: str) -> RemoteEntry | None:
        return self.remote.create_folder(remote_path)
