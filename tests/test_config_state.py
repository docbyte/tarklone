from __future__ import annotations

from pathlib import Path

from tarklone.config import Settings, load_settings, save_settings
from tarklone.state import EntryState, SyncState


def test_settings_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    settings = Settings(
        local_folder=str(tmp_path),
        workspace_id="ws-1",
        workspace_name="Test",
        sync_deletions=True,
        exclude=["*.tmp"],
    )
    save_settings(settings, path)
    loaded = load_settings(path)
    assert loaded.local_folder == str(tmp_path)
    assert loaded.workspace_id == "ws-1"
    assert loaded.workspace_name == "Test"
    assert loaded.sync_deletions is True
    assert loaded.exclude == ["*.tmp"]


def test_settings_ignores_unknown_fields(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"workspace_id": "ws-1", "unknown": 42}', encoding="utf-8")
    loaded = load_settings(path)
    assert loaded.workspace_id == "ws-1"


def test_settings_validation(tmp_path: Path) -> None:
    settings = Settings(local_folder=str(tmp_path), workspace_id="ws-1")
    assert settings.validation_errors() == []
    settings.workspace_id = ""
    assert any("Workspace" in error for error in settings.validation_errors())
    settings.local_folder = str(tmp_path / "missing")
    assert any("existiert nicht" in error for error in settings.validation_errors())


def test_state_roundtrip(tmp_path: Path) -> None:
    state = SyncState(tmp_path / "state.sqlite3")
    try:
        entry = EntryState(
            path="a/b.txt",
            kind="file",
            local_mtime_ns=123,
            local_size=5,
            remote_id="r1",
            remote_size=5,
        )
        state.upsert(entry)
        loaded = state.get("a/b.txt")
        assert loaded is not None
        assert loaded.remote_id == "r1"
        assert state.all()["a/b.txt"].local_size == 5

        state.set_meta("last_sync", "2026-01-01T00:00:00")
        assert state.get_meta("last_sync") == "2026-01-01T00:00:00"

        state.delete("a/b.txt")
        assert state.get("a/b.txt") is None
    finally:
        state.close()
