from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import shutil
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Protocol

from .state import EntryState, SyncState


class SyncError(RuntimeError):
    pass


class SyncBusy(SyncError):
    pass


class RemoteUnsupported(SyncError):
    pass


@dataclass
class RemoteEntry:
    path: str
    kind: str = "file"
    size: int = 0
    mtime: float = 0.0
    entry_id: str = ""
    content_hash: str = ""
    parent_id: str = ""
    # Tatsächlicher Remote-Pfad, falls er vom logischen Pfad abweicht
    # (verschlüsselte Namen unter encrypted/). Leer = identisch mit path.
    remote_path: str = ""
    # Für transparent verschlüsselte Dateien: Größe des Klartexts. ``size``
    # bleibt die Größe des tatsächlich übertragenen Blobs (Ciphertext).
    # None bedeutet: keine abweichende logische Größe vorhanden.
    logical_size: int | None = None


@dataclass
class LocalEntry:
    path: str
    absolute: Path
    kind: str = "file"
    size: int = 0
    mtime_ns: int = 0
    content_hash: str = ""

    @property
    def mtime(self) -> float:
        return self.mtime_ns / 1_000_000_000


class RemoteAdapter(Protocol):
    def list_tree(self) -> list[RemoteEntry]: ...

    def download(self, entry: RemoteEntry, destination: Path) -> None: ...

    def upload(
        self,
        local_path: Path,
        remote_path: str,
        *,
        mtime: float | None = None,
        remote_entry: RemoteEntry | None = None,
    ) -> RemoteEntry | None: ...

    def delete(self, entry: RemoteEntry) -> None: ...

    def create_folder(self, remote_path: str) -> RemoteEntry | None: ...


@dataclass
class Action:
    kind: str
    path: str
    reason: str = ""
    local: LocalEntry | None = None
    remote: RemoteEntry | None = None


@dataclass
class SyncProgress:
    current: int
    total: int
    percent: int
    eta_seconds: float
    message: str = ""


@dataclass
class SyncResult:
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    dry_run: bool = False
    uploaded: int = 0
    downloaded: int = 0
    deleted_local: int = 0
    deleted_remote: int = 0
    created_folders: int = 0
    conflicts: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, (self.finished_at or time.time()) - self.started_at)

    @property
    def changed(self) -> int:
        return (
            self.uploaded
            + self.downloaded
            + self.deleted_local
            + self.deleted_remote
            + self.created_folders
            + self.conflicts
        )


def normalize_rel(path: str) -> str:
    value = path.replace("\\", "/").strip("/")
    parts = [part for part in value.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise SyncError(f"Ungültiger relativer Pfad: {path!r}")
    return "/".join(parts)


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class LocalScanner:
    def __init__(self, root: Path, exclude: Iterable[str] = ()):
        self.root = Path(root)
        self.exclude = [pattern.replace("\\", "/") for pattern in exclude]

    def is_excluded(self, rel_path: str) -> bool:
        rel = rel_path.replace("\\", "/")
        if rel == ".tarklone" or rel.startswith(".tarklone/"):
            return True
        name = PurePosixPath(rel).name
        for pattern in self.exclude:
            pattern = pattern.strip()
            if not pattern:
                continue
            if pattern.endswith("/**"):
                prefix = pattern[:-3].rstrip("/")
                if rel == prefix or rel.startswith(prefix + "/"):
                    return True
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
                return True
        return False

    def scan(self) -> tuple[dict[str, LocalEntry], list[str]]:
        entries: dict[str, LocalEntry] = {}
        errors: list[str] = []
        if not self.root.is_dir():
            raise SyncError(f"Lokaler Ordner existiert nicht: {self.root}")

        for current, dirnames, filenames in os.walk(self.root, followlinks=False):
            current_path = Path(current)
            rel_dir = current_path.relative_to(self.root).as_posix()
            if rel_dir == ".":
                rel_dir = ""

            kept_dirs: list[str] = []
            for dirname in dirnames:
                rel = f"{rel_dir}/{dirname}" if rel_dir else dirname
                if self.is_excluded(rel):
                    continue
                absolute = current_path / dirname
                if absolute.is_symlink():
                    continue
                kept_dirs.append(dirname)
                try:
                    stat = absolute.stat()
                except OSError as exc:
                    errors.append(f"{rel}: {exc}")
                    continue
                entries[rel] = LocalEntry(
                    path=rel,
                    absolute=absolute,
                    kind="dir",
                    size=0,
                    mtime_ns=stat.st_mtime_ns,
                )
            dirnames[:] = kept_dirs

            for filename in filenames:
                rel = f"{rel_dir}/{filename}" if rel_dir else filename
                if self.is_excluded(rel):
                    continue
                absolute = current_path / filename
                if absolute.is_symlink():
                    continue
                try:
                    stat = absolute.stat()
                except OSError as exc:
                    errors.append(f"{rel}: {exc}")
                    continue
                if not absolute.is_file():
                    continue
                entries[rel] = LocalEntry(
                    path=rel,
                    absolute=absolute,
                    kind="file",
                    size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                )
        return entries, errors


class SyncEngine:
    def __init__(
        self,
        local_root: Path,
        adapter: RemoteAdapter,
        state: SyncState,
        *,
        sync_deletions: bool = False,
        conflict_policy: str = "copy",
        exclude: Iterable[str] = (),
        logger: logging.Logger | None = None,
        progress: Callable[[str], None] | None = None,
        on_progress: Callable[[SyncProgress], None] | None = None,
        cancel_event: threading.Event | None = None,
    ):
        self.local_root = Path(local_root).resolve()
        self.adapter = adapter
        self.state = state
        self.supports_empty_folders = bool(
            getattr(adapter, "supports_empty_folders", True)
        )
        self.sync_deletions = sync_deletions
        self.conflict_policy = conflict_policy
        self.scanner = LocalScanner(self.local_root, exclude)
        self.logger = logger or logging.getLogger("tarklone.sync")
        self.progress = progress
        self.on_progress = on_progress
        self.cancel_event = cancel_event or threading.Event()
        self._run_lock = threading.Lock()
        self._trash_root = self.local_root / ".tarklone" / "trash"

    def _emit(self, message: str) -> None:
        self.logger.info(message)
        if self.progress:
            try:
                self.progress(message)
            except Exception:
                pass

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise SyncError("Sync abgebrochen.")

    def _emit_progress(
        self,
        current: int,
        total: int,
        started_at: float,
        message: str = "",
    ) -> None:
        if self.on_progress is None:
            return
        if total <= 0:
            percent = 100
            eta = 0.0
        else:
            percent = max(0, min(100, int(current * 100 / total)))
            elapsed = max(0.0, time.time() - started_at)
            eta = (elapsed / current) * (total - current) if current > 0 else 0.0
        try:
            self.on_progress(
                SyncProgress(
                    current=current,
                    total=total,
                    percent=percent,
                    eta_seconds=eta,
                    message=message,
                )
            )
        except Exception:
            pass

    def _safe_destination(self, rel_path: str) -> Path:
        rel = normalize_rel(rel_path)
        destination = (self.local_root / PurePosixPath(rel)).resolve()
        try:
            destination.relative_to(self.local_root)
        except ValueError as exc:
            raise SyncError(f"Pfad liegt außerhalb des Sync-Ordners: {rel_path!r}") from exc
        return destination

    def _trash_path(self, rel_path: str) -> Path:
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        return self._trash_root / stamp / PurePosixPath(normalize_rel(rel_path))

    def _move_to_trash(self, local: LocalEntry) -> None:
        target = self._trash_path(local.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(local.absolute), str(target))
        except OSError:
            shutil.copy2(local.absolute, target)
            local.absolute.unlink(missing_ok=True)

    @staticmethod
    def _content_equal(local: LocalEntry, remote: RemoteEntry) -> bool:
        remote_size = (
            remote.logical_size
            if remote.logical_size is not None
            else remote.size
        )
        if local.size != remote_size:
            return False
        if remote.content_hash and local.content_hash:
            return remote.content_hash.lower() == local.content_hash.lower()
        return True

    def _plan(
        self,
        local_map: dict[str, LocalEntry],
        remote_map: dict[str, RemoteEntry],
        state_map: dict[str, EntryState],
        full: bool,
    ) -> list[Action]:
        actions: list[Action] = []
        paths = sorted(set(local_map) | set(remote_map) | set(state_map))

        for path in paths:
            self._check_cancel()
            local = local_map.get(path)
            remote = remote_map.get(path)
            previous = None if full else state_map.get(path)

            if local and remote and local.kind != remote.kind:
                actions.append(
                    Action(
                        "conflict",
                        path,
                        "Datei/Ordner-Typ unterscheidet sich",
                        local,
                        remote,
                    )
                )
                continue

            if local and local.kind == "dir":
                if not remote:
                    if self.supports_empty_folders:
                        actions.append(Action("mkdir_remote", path, "Ordner nur lokal", local))
                    else:
                        actions.append(
                            Action(
                                "skip",
                                path,
                                "leerer Ordner wird beim ersten Upload angelegt",
                                local,
                            )
                        )
                continue
            if remote and remote.kind == "dir":
                if not local:
                    actions.append(Action("mkdir_local", path, "Ordner nur remote", remote=remote))
                continue

            if not local and not remote:
                if previous:
                    actions.append(Action("forget", path, "beidseitig gelöscht"))
                continue

            if local and remote:
                if previous is None:
                    if self._content_equal(local, remote):
                        actions.append(Action("record", path, "beidseitig vorhanden", local, remote))
                    else:
                        actions.append(
                            Action("conflict", path, "beidseitig vorhanden, Inhalt unbekannt", local, remote)
                        )
                    continue

                local_changed = (
                    local.mtime_ns != previous.local_mtime_ns
                    or local.size != previous.local_size
                )
                remote_changed = (
                    remote.entry_id != previous.remote_id
                    or remote.size != previous.remote_size
                    or (
                        remote.mtime
                        and previous.remote_mtime
                        and abs(remote.mtime - previous.remote_mtime) > 1.0
                    )
                )
                if not local_changed and not remote_changed:
                    actions.append(Action("skip", path, "unverändert", local, remote))
                elif local_changed and not remote_changed:
                    actions.append(Action("upload", path, "lokal geändert", local, remote))
                elif remote_changed and not local_changed:
                    actions.append(Action("download", path, "remote geändert", local, remote))
                else:
                    if self._content_equal(local, remote):
                        actions.append(Action("record", path, "beide geändert, gleicher Inhalt", local, remote))
                    else:
                        actions.append(Action("conflict", path, "beide geändert", local, remote))
                continue

            if local and not remote:
                if previous is None:
                    actions.append(Action("upload", path, "nur lokal", local))
                    continue
                local_changed = (
                    local.mtime_ns != previous.local_mtime_ns
                    or local.size != previous.local_size
                )
                if local_changed:
                    actions.append(Action("upload", path, "lokal geändert, remote gelöscht", local))
                elif self.sync_deletions:
                    actions.append(Action("delete_local", path, "remote gelöscht", local))
                else:
                    actions.append(Action("upload", path, "remote gelöscht, wiederherstellen", local))
                continue

            if remote and not local:
                if previous is None:
                    actions.append(Action("download", path, "nur remote", remote=remote))
                    continue
                remote_changed = (
                    remote.entry_id != previous.remote_id
                    or remote.size != previous.remote_size
                    or (
                        remote.mtime
                        and previous.remote_mtime
                        and abs(remote.mtime - previous.remote_mtime) > 1.0
                    )
                )
                if remote_changed:
                    actions.append(Action("download", path, "remote geändert, lokal gelöscht", remote=remote))
                elif self.sync_deletions:
                    actions.append(Action("delete_remote", path, "lokal gelöscht", remote=remote))
                else:
                    actions.append(Action("download", path, "lokal gelöscht, wiederherstellen", remote=remote))

        return actions

    def _conflict_path(self, rel_path: str) -> str:
        path = PurePosixPath(normalize_rel(rel_path))
        stamp = datetime.now().strftime("%Y-%m-%d %H%M%S")
        host = socket.gethostname().split(".")[0] or "pc"
        suffix = path.suffix
        stem = path.name[: -len(suffix)] if suffix else path.name
        parent = str(path.parent) if str(path.parent) != "." else ""
        index = 0
        while True:
            extra = f"-{index}" if index else ""
            name = f"{stem} (Konflikt {host} {stamp}{extra}){suffix}"
            candidate = f"{parent}/{name}" if parent else name
            if not (self.local_root / PurePosixPath(candidate)).exists():
                return candidate
            index += 1

    def _record(self, path: str, kind: str, local: LocalEntry | None, remote: RemoteEntry | None) -> None:
        state = EntryState(
            path=path,
            kind=kind,
            synced_at=datetime.now().isoformat(timespec="seconds"),
        )
        if local:
            state.local_mtime_ns = local.mtime_ns
            state.local_size = local.size
            state.local_hash = local.content_hash
        if remote:
            state.remote_id = remote.entry_id
            state.remote_mtime = remote.mtime
            state.remote_size = remote.size
            state.remote_hash = remote.content_hash
        self.state.upsert(state)

    def _upload(self, action: Action) -> None:
        local = action.local
        assert local is not None
        remote = self.adapter.upload(
            local.absolute,
            action.path,
            mtime=local.mtime,
            remote_entry=action.remote,
        )
        if remote is None:
            remote = RemoteEntry(path=action.path, size=local.size, mtime=local.mtime)
        remote.path = normalize_rel(remote.path or action.path)
        self._record(action.path, "file", local, remote)

    def _download(self, remote: RemoteEntry) -> None:
        destination = self._safe_destination(remote.path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(destination.name + ".tarklone-part")
        try:
            self.adapter.download(remote, temp)
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)
        stat = destination.stat()
        local = LocalEntry(
            path=remote.path,
            absolute=destination,
            kind="file",
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )
        self._record(remote.path, "file", local, remote)

    def _resolve_conflict(self, action: Action) -> None:
        local = action.local
        remote = action.remote
        assert local is not None and remote is not None
        conflict_rel = self._conflict_path(action.path)
        conflict_abs = self._safe_destination(conflict_rel)
        conflict_abs.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local.absolute, conflict_abs)

        # Remote-Version an den ursprünglichen Pfad laden, lokale Fassung
        # bleibt als Konfliktkopie erhalten und wird zusätzlich hochgeladen.
        self._download(remote)
        conflict_local = LocalEntry(
            path=conflict_rel,
            absolute=conflict_abs,
            kind="file",
            size=conflict_abs.stat().st_size,
            mtime_ns=conflict_abs.stat().st_mtime_ns,
        )
        uploaded = self.adapter.upload(
            conflict_abs,
            conflict_rel,
            mtime=conflict_local.mtime,
        )
        if uploaded is None:
            uploaded = RemoteEntry(path=conflict_rel, size=conflict_local.size)
        self._record(conflict_rel, "file", conflict_local, uploaded)
        self._emit(f"Konflikt: {action.path} → {conflict_rel}")

    def _execute(self, action: Action, result: SyncResult, dry_run: bool) -> None:
        self._check_cancel()
        if action.kind == "skip":
            if action.remote and action.local and not dry_run:
                current = self.state.get(action.path)
                if current is not None and (
                    current.remote_mtime != action.remote.mtime
                    or current.remote_id != action.remote.entry_id
                ):
                    current.remote_mtime = action.remote.mtime
                    current.remote_id = action.remote.entry_id
                    current.remote_size = action.remote.size
                    self.state.upsert(current)
            result.skipped += 1
            return
        if action.kind == "forget":
            if not dry_run:
                self.state.delete(action.path)
            result.skipped += 1
            return
        if dry_run:
            self._emit(f"[dry-run] {action.kind}: {action.path} ({action.reason})")
            return

        if action.kind == "record":
            assert action.local and action.remote
            if not action.local.content_hash and action.remote.content_hash:
                action.local.content_hash = hash_file(action.local.absolute)
            self._record(action.path, "file", action.local, action.remote)
            result.skipped += 1
            return

        if action.kind == "upload":
            self._emit(f"Upload: {action.path}")
            self._upload(action)
            result.uploaded += 1
            return

        if action.kind == "download":
            assert action.remote
            self._emit(f"Download: {action.path}")
            self._download(action.remote)
            result.downloaded += 1
            return

        if action.kind == "delete_local":
            assert action.local
            self._emit(f"Lokal löschen (Papierkorb): {action.path}")
            self._move_to_trash(action.local)
            self.state.delete(action.path)
            result.deleted_local += 1
            return

        if action.kind == "delete_remote":
            assert action.remote
            self._emit(f"Remote löschen: {action.path}")
            self.adapter.delete(action.remote)
            self.state.delete(action.path)
            result.deleted_remote += 1
            return

        if action.kind == "mkdir_local":
            assert action.remote
            self._emit(f"Ordner anlegen: {action.path}")
            destination = self._safe_destination(action.path)
            destination.mkdir(parents=True, exist_ok=True)
            stat = destination.stat()
            local = LocalEntry(
                path=action.path,
                absolute=destination,
                kind="dir",
                mtime_ns=stat.st_mtime_ns,
            )
            self._record(action.path, "dir", local, action.remote)
            result.created_folders += 1
            return

        if action.kind == "mkdir_remote":
            self._emit(f"Remote-Ordner anlegen: {action.path}")
            remote = self.adapter.create_folder(action.path)
            if remote is None:
                remote = RemoteEntry(path=action.path, kind="dir")
            self._record(action.path, "dir", action.local, remote)
            result.created_folders += 1
            return

        if action.kind == "conflict":
            assert action.local and action.remote
            self._emit(f"Konflikt: {action.path} ({action.reason})")
            self._resolve_conflict(action)
            result.conflicts += 1
            return

        raise SyncError(f"Unbekannte Sync-Aktion: {action.kind}")

    def sync(self, *, dry_run: bool = False, full: bool = False) -> SyncResult:
        if not self._run_lock.acquire(blocking=False):
            raise SyncBusy("Es läuft bereits ein Sync.")
        result = SyncResult(dry_run=dry_run)
        try:
            self._emit("Scanne lokalen Ordner …")
            local_map, scan_errors = self.scanner.scan()
            result.errors.extend(scan_errors)

            self._emit("Lade Remote-Dateiliste …")
            remote_entries = self.adapter.list_tree()
            remote_map: dict[str, RemoteEntry] = {}
            for entry in remote_entries:
                try:
                    path = normalize_rel(entry.path)
                except SyncError as exc:
                    result.errors.append(str(exc))
                    continue
                if not path:
                    continue
                entry.path = path
                remote_map[path] = entry

            state_map = self.state.all()
            actions = self._plan(local_map, remote_map, state_map, full)
            result.actions = actions
            self._emit(f"{len(actions)} Aktion(en) geplant.")
            total = sum(
                1 for action in actions if action.kind not in {"skip", "record"}
            )
            completed = 0
            started = time.time()
            self._emit_progress(0, total, started, "Start")

            for action in actions:
                self._check_cancel()
                try:
                    self._execute(action, result, dry_run)
                except RemoteUnsupported as exc:
                    result.errors.append(f"{action.path}: {exc}")
                except SyncError:
                    raise
                except Exception as exc:  # noqa: BLE001 - pro Datei weiterarbeiten
                    self.logger.exception("Sync-Aktion fehlgeschlagen: %s", action.path)
                    result.errors.append(f"{action.path}: {exc}")
                if action.kind not in {"skip", "record"}:
                    completed += 1
                    self._emit_progress(completed, total, started, action.path)

            self._emit_progress(total, total, started, "Fertig")
            result.finished_at = time.time()
            if not dry_run:
                self.state.set_meta("last_sync", datetime.now().isoformat(timespec="seconds"))
                self.state.set_meta("last_result", f"{result.changed} Änderung(en)")
            return result
        finally:
            self._run_lock.release()
