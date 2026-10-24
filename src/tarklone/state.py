from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any


@dataclass
class EntryState:
    path: str
    kind: str = "file"
    local_mtime_ns: int = 0
    local_size: int = 0
    local_hash: str = ""
    remote_id: str = ""
    remote_mtime: float = 0.0
    remote_size: int = 0
    remote_hash: str = ""
    synced_at: str = ""

    def to_row(self) -> dict[str, Any]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "EntryState":
        values = {field.name: row[field.name] for field in fields(cls)}
        return cls(**values)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    path TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'file',
    local_mtime_ns INTEGER NOT NULL DEFAULT 0,
    local_size INTEGER NOT NULL DEFAULT 0,
    local_hash TEXT NOT NULL DEFAULT '',
    remote_id TEXT NOT NULL DEFAULT '',
    remote_mtime REAL NOT NULL DEFAULT 0,
    remote_size INTEGER NOT NULL DEFAULT 0,
    remote_hash TEXT NOT NULL DEFAULT '',
    synced_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""


class SyncState:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def all(self) -> dict[str, EntryState]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM entries").fetchall()
        return {row["path"]: EntryState.from_row(row) for row in rows}

    def get(self, path: str) -> EntryState | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM entries WHERE path = ?", (path,)).fetchone()
        return EntryState.from_row(row) if row else None

    def upsert(self, entry: EntryState) -> None:
        values = entry.to_row()
        columns = ", ".join(values)
        placeholders = ", ".join(f":{column}" for column in values)
        updates = ", ".join(
            f"{column} = excluded.{column}" for column in values if column != "path"
        )
        sql = (
            f"INSERT INTO entries ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(path) DO UPDATE SET {updates}"
        )
        with self._lock:
            self._conn.execute(sql, values)
            self._conn.commit()

    def delete(self, path: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM entries WHERE path = ?", (path,))
            self._conn.commit()

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM entries")
            self._conn.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()
