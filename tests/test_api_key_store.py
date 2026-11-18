from __future__ import annotations

import os

import keyring
import pytest

from tarklone.auth import ApiKeyStore, mask_api_key
from tarklone.config import normalize_api_key

KEY = "tk_test_1234567890abcdef"


def test_mask_api_key() -> None:
    assert mask_api_key(KEY) == "tk_…cdef"
    assert mask_api_key("") == ""


def test_normalize_api_key_accepts_bearer_prefix() -> None:
    assert normalize_api_key(f"  Bearer {KEY}  ") == KEY
    assert normalize_api_key(f'"{KEY}"') == KEY


def test_api_key_store_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    values: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        keyring,
        "get_keyring",
        lambda: type("TestKeyring", (), {"priority": 1})(),
    )

    def set_password(service: str, username: str, value: str) -> None:
        values[(service, username)] = value

    def get_password(service: str, username: str) -> str | None:
        return values.get((service, username))

    def delete_password(service: str, username: str) -> None:
        values.pop((service, username), None)

    monkeypatch.setattr(keyring, "set_password", set_password)
    monkeypatch.setattr(keyring, "get_password", get_password)
    monkeypatch.setattr(keyring, "delete_password", delete_password)

    store = ApiKeyStore(fallback_path=tmp_path / "api-key.json")
    assert store.load() is None
    store.save(KEY)
    assert store.load() == KEY
    assert store.delete() is True
    assert store.load() is None


def test_api_key_store_fallback_without_keyring(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    def fail(*args, **kwargs):
        raise RuntimeError("no keyring")

    monkeypatch.setattr(keyring, "set_password", fail)
    monkeypatch.setattr(keyring, "get_password", fail)
    monkeypatch.setattr(keyring, "delete_password", fail)

    fallback = tmp_path / "api-key.json"
    store = ApiKeyStore(fallback_path=fallback)
    store.save(KEY)
    assert fallback.is_file()
    if os.name == "nt":
        assert KEY.encode() not in fallback.read_bytes()
    else:
        assert fallback.stat().st_mode & 0o777 == 0o600
    assert store.load() == KEY
    assert store.delete() is False
    assert not fallback.exists()


def test_api_key_store_rejects_invalid_key(tmp_path) -> None:
    store = ApiKeyStore(
        service="tarklone-test-invalid",
        username="api-key-test",
        fallback_path=tmp_path / "api-key.json",
    )
    with pytest.raises(Exception, match="tk_"):
        store.save("not-a-key")
    assert store.load() is None


def test_api_key_store_delete_reports_persistent_keyring_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    values: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        keyring,
        "get_keyring",
        lambda: type("TestKeyring", (), {"priority": 1})(),
    )
    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda service, username, value: values.__setitem__((service, username), value),
    )
    monkeypatch.setattr(
        keyring,
        "get_password",
        lambda service, username: values.get((service, username)),
    )
    monkeypatch.setattr(keyring, "delete_password", lambda *_args: None)
    store = ApiKeyStore(fallback_path=tmp_path / "api-key.json")
    store.save(KEY)
    assert store.delete() is False
    assert store.load() == KEY
