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
        result: list[RemoteEntry] = []
        for entry in entries:
            if entry.kind != "file" or not self.encryption.is_encrypted_path(entry.path):
                result.append(entry)
                continue
            if not self.encryption.unlocked:
                logger.warning(
                    "Überspringe verschlüsselte Datei (Verschlüsselung gesperrt): %s",
                    entry.path,
                )
                continue
            entry.logical_size = self._logical_size(entry)
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
        temp = self._temp_path(local_path.name)
        try:
            self.encryption.encrypt_file(local_path, temp)
            cipher_size = temp.stat().st_size
            uploaded = self.remote.upload(
                temp,
                remote_path,
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
