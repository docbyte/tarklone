from __future__ import annotations

from pathlib import Path

from tarklone.config import (
    Settings,
    SyncFolder,
    SyncProfile,
    crypto_keyring_username,
    load_settings,
    profile_state_path,
    save_settings,
    validate_api_base,
)
from tarklone.state import EntryState, SyncState


def test_settings_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    settings = Settings(
        local_folder=str(tmp_path),
        workspace_id="ws1abcde",
        workspace_name="Test",
        sync_deletions=True,
        exclude=["*.tmp"],
    )
    save_settings(settings, path)
    loaded = load_settings(path)
    assert loaded.local_folder == str(tmp_path)
    assert loaded.workspace_id == "ws1abcde"
    assert loaded.workspace_name == "Test"
    assert loaded.sync_deletions is True
    assert loaded.exclude == ["*.tmp"]
    assert len(loaded.profiles) == 1
    assert loaded.profiles[0].workspace_id == "ws1abcde"


def test_multi_folder_profile_roundtrip_and_validation(tmp_path: Path) -> None:
    first = tmp_path / "eins"
    second = tmp_path / "zwei"
    first.mkdir()
    second.mkdir()
    path = tmp_path / "config.json"
    settings = Settings(
        profiles=[
            SyncProfile(
                id="ws1abcde",
                workspace_id="ws1abcde",
                workspace_name="Multi",
                sync_mode="multi",
                folders=[
                    SyncFolder(
                        id="folder-1",
                        local_folder=str(first),
                        remote_folder_id="Team/Eins",
                    ),
                    SyncFolder(
                        id="folder-2",
                        local_folder=str(second),
                        remote_folder_id="Team/Zwei",
                    ),
                ],
            )
        ]
    )
    save_settings(settings, path)
    loaded = load_settings(path)
    profile = loaded.profiles[0]
    assert profile.sync_mode == "multi"
    assert [folder.remote_folder_id for folder in profile.folders] == [
        "Team/Eins",
        "Team/Zwei",
    ]
    assert [folder.local_folder for folder in profile.active_folders()] == [
        str(first),
        str(second),
    ]
    assert loaded.enabled_profiles == [profile]
    assert loaded.validation_errors() == []

    profile.folders[1].remote_folder_id = "Team/Eins"
    assert any("Remote-Ordner" in error for error in loaded.validation_errors())

    profile.folders[1].remote_folder_id = "Team/Zwei"
    profile.folders[1].local_folder = str(first)
    assert any("Lokale Ordner" in error for error in loaded.validation_errors())

    profile.folders[1].local_folder = str(second)
    profile.folders[1].remote_folder_id = ""
    profile.folders[0].remote_folder_id = "Team/Eins"
    assert any("Remote-Ordner" in error for error in loaded.validation_errors())


def test_multiple_profiles_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    settings = Settings(
        profiles=[
            SyncProfile(
                id="ws1abcde",
                workspace_id="ws1abcde",
                workspace_name="Eins",
                local_folder=str(tmp_path),
                remote_folder_id="kunden",
                sync_interval_seconds=30,
                sync_deletions=True,
            ),
            SyncProfile(
                id="ws2abcde",
                workspace_id="ws2abcde",
                workspace_name="Zwei",
                local_folder=str(tmp_path),
                sync_interval_seconds=120,
            ),
        ]
    )
    save_settings(settings, path)
    loaded = load_settings(path)
    assert [profile.workspace_id for profile in loaded.profiles] == ["ws1abcde", "ws2abcde"]
    assert loaded.profiles[0].remote_folder_id == "kunden"
    assert loaded.profiles[0].sync_interval_seconds == 30
    assert loaded.profiles[0].sync_deletions is True
    assert loaded.profiles[1].sync_interval_seconds == 120
    assert loaded.enabled_profiles == loaded.profiles


def test_disabled_workspace_profiles_are_ignored(tmp_path: Path) -> None:
    settings = Settings(
        profiles=[
            SyncProfile(
                id="ws1abcde",
                workspace_id="ws1abcde",
                local_folder=str(tmp_path),
                enabled=True,
            ),
            SyncProfile(
                id="ws2abcde",
                workspace_id="ws2abcde",
                local_folder="",
                enabled=False,
            ),
        ]
    )
    assert settings.validation_errors() == []
    assert settings.is_configured
    assert [profile.id for profile in settings.enabled_profiles] == ["ws1abcde"]
    settings.profiles[0].enabled = False
    assert not settings.is_configured
    assert any("Profil" in error for error in settings.validation_errors())


def test_profile_paths_are_stable_and_safe() -> None:
    assert profile_state_path("ws1abcde").name == "ws1abcde.sqlite3"
    assert "/" not in profile_state_path("../../evil").name
    assert crypto_keyring_username("ws1abcde") == "crypto-master-ws1abcde"
    first = crypto_keyring_username("ws1abcde", "C:/sync/a")
    second = crypto_keyring_username("ws1abcde", "C:/sync/b")
    assert first.startswith("crypto-master-ws1abcde-")
    assert first != second
    assert first == crypto_keyring_username("ws1abcde", "C:/sync/a")


def test_service_endpoints_reject_untrusted_origins() -> None:
    assert validate_api_base("https://tarkle.tech/api/v1") == (
        "https://tarkle.tech/api/v1"
    )
    for endpoint in (
        "http://tarkle.tech/api/v1",
        "https://attacker.example/api/v1",
        "https://user:pass@tarkle.tech/api/v1",
        "https://tarkle.tech:444/api/v1",
        "https://tarkle.tech/api/v2",
    ):
        try:
            validate_api_base(endpoint)
        except ValueError:
            pass
        else:
            raise AssertionError(f"endpoint should be rejected: {endpoint}")


def test_settings_ignores_unknown_fields(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"workspace_id": "ws1abcde", "unknown": 42}', encoding="utf-8")
    loaded = load_settings(path)
    assert loaded.workspace_id == "ws1abcde"


def test_legacy_single_profile_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        '{"local_folder": "%s", "workspace_id": "ws1abcde", '
        '"sync_interval_seconds": 30, "sync_deletions": true}'
        % str(tmp_path).replace("\\", "\\\\"),
        encoding="utf-8",
    )
    loaded = load_settings(path)
    assert len(loaded.profiles) == 1
    profile = loaded.profiles[0]
    assert profile.workspace_id == "ws1abcde"
    assert profile.local_folder == str(tmp_path)
    assert profile.sync_interval_seconds == 30
    assert profile.sync_deletions is True


def test_settings_validation(tmp_path: Path) -> None:
    settings = Settings(local_folder=str(tmp_path), workspace_id="ws1abcde")
    assert settings.validation_errors() == []
    settings.ensure_profiles()
    settings.profiles[0].workspace_id = ""
    assert any("Workspace" in error for error in settings.validation_errors())
    settings.profiles[0].workspace_id = "ws1abcde"
    settings.profiles[0].local_folder = str(tmp_path / "missing")
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
