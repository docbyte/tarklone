from __future__ import annotations

import pytest

from tarklone.config import Settings
from tarklone.ui import SetupDialog


class FakeVar:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


def make_dialog(settings: Settings, *, launch: bool):
    dialog = SetupDialog.__new__(SetupDialog)
    dialog.settings = settings
    dialog._profiles = settings.profiles
    dialog._tabs = {}
    dialog._workspaces_loaded_ok = False
    dialog.start_minimized_var = FakeVar(settings.start_minimized)
    dialog.launch_var = FakeVar(launch)
    dialog.api_key_var = FakeVar("")
    dialog._update_days_value = lambda: 7
    dialog._closed = False
    dialog._release_grab = lambda: None
    dialog.destroy = lambda: None
    dialog.on_saved = lambda _settings: None
    return dialog


def test_save_without_api_key_still_applies_autostart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(launch_at_login=True)
    dialog = make_dialog(settings, launch=False)
    saved: list[Settings] = []
    applied: list[bool] = []
    monkeypatch.setattr("tarklone.ui.ApiKeyStore.load", lambda self: None)
    monkeypatch.setattr(
        "tarklone.ui.save_settings", lambda value: saved.append(value)
    )
    monkeypatch.setattr(
        "tarklone.ui.apply_launch_at_login", lambda value: applied.append(value)
    )

    SetupDialog._save(dialog)

    assert dialog._closed is True
    assert applied == [False]
    assert saved and saved[0].launch_at_login is False
