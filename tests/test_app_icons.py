from __future__ import annotations

import threading

import pytest

from tarklone.app import TarkloneApp, aggregate_icon_variant, logo_path, make_icon
from tarklone.config import Settings, SyncProfile


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            dict(has_api_key=False, paused=False, configured=True, tones=[]),
            "red",
        ),
        (
            dict(has_api_key=True, paused=True, configured=True, tones=["green"]),
            "red",
        ),
        (
            dict(has_api_key=True, paused=False, configured=False, tones=[]),
            "red",
        ),
        (
            dict(has_api_key=True, paused=False, configured=True, tones=["red"]),
            "red",
        ),
        (
            dict(has_api_key=True, paused=False, configured=True, tones=["blue"]),
            "blue",
        ),
        (
            dict(
                has_api_key=True,
                paused=False,
                configured=True,
                tones=["green", "neutral"],
            ),
            "green",
        ),
    ],
)
def test_aggregate_icon_variant(kwargs, expected: str) -> None:
    assert aggregate_icon_variant(**kwargs) == expected


@pytest.mark.parametrize("variant", ["green", "blue", "red"])
def test_status_logos_are_bundled_and_loadable(variant: str) -> None:
    path = logo_path(variant)
    assert path.is_file()
    image = make_icon(variant)
    assert image.mode == "RGBA"
    assert image.size == (64, 64)


def test_tray_menu_shows_one_status_line_per_workspace(tmp_path) -> None:
    app = TarkloneApp.__new__(TarkloneApp)
    app.settings = Settings(
        profiles=[
            SyncProfile(
                id="ws1abcde",
                workspace_id="ws1abcde",
                workspace_name="M",
                local_folder=str(tmp_path),
            ),
            SyncProfile(
                id="ws2abcde",
                workspace_id="ws2abcde",
                workspace_name="powmod",
                local_folder=str(tmp_path),
            ),
        ]
    )
    app.paused = False
    app._current_profile_id = ""
    app._queued_ids = set()
    app._queue_lock = threading.Lock()
    app._profile_text = {
        "ws1abcde": "Alles aktuell",
        "ws2abcde": "Synchronisiere – noch 50 %, ca. 2 min",
    }
    app._profile_tone = {"ws1abcde": "green", "ws2abcde": "blue"}
    app._has_api_key = lambda: True  # type: ignore[method-assign]

    menu = app._menu()
    status_lines = [str(item.text) for item in list(menu.items)[:2]]

    assert status_lines == [
        "M: Alles aktuell",
        "powmod: Synchronisiere – noch 50 %, ca. 2 min",
    ]
    assert app._icon_variant_for(True) == "blue"
