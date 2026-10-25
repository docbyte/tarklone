from __future__ import annotations

import base64
import json
import os
import threading
import urllib.error
import urllib.request
from urllib.parse import urlparse

import keyring
import pytest

from tarklone.auth import OAuthClient, Token, TokenStore, _CallbackServer, jwt_claims
from tarklone.config import Settings


def make_jwt(claims: dict) -> str:
    def part(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part(claims)}.signature"


def test_jwt_claims() -> None:
    token = make_jwt({"email": "a@example.com", "sub": "123"})
    claims = jwt_claims(token)
    assert claims["email"] == "a@example.com"
    assert claims["sub"] == "123"


def test_callback_server_captures_code() -> None:
    server = _CallbackServer(("127.0.0.1", 0))
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{port}/callback?code=abc&state=xyz"
        with urllib.request.urlopen(url, timeout=5) as response:
            assert response.status == 200
        assert server.event.wait(2)
        assert server.result["code"] == ["abc"]
        assert server.result["state"] == ["xyz"]
    finally:
        server.shutdown()
        server.server_close()


def test_callback_server_ignores_other_paths() -> None:
    server = _CallbackServer(("127.0.0.1", 0))
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/favicon.ico", timeout=5)
        assert excinfo.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_token_store_roundtrip(
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

    store = TokenStore(fallback_path=tmp_path / "token.json")
    assert store.load() is None
    token = Token(
        access_token="access",
        refresh_token="refresh",
        expires_at=123.0,
        email="a@example.com",
    )
    store.save(token)
    loaded = store.load()
    assert loaded is not None
    assert loaded.access_token == "access"
    assert loaded.refresh_token == "refresh"
    assert loaded.email == "a@example.com"
    store.delete()
    assert store.load() is None


def test_token_store_fallback_without_keyring(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    def fail(*args, **kwargs):
        raise RuntimeError("no keyring")

    monkeypatch.setattr(keyring, "set_password", fail)
    monkeypatch.setattr(keyring, "get_password", fail)
    monkeypatch.setattr(keyring, "delete_password", fail)

    fallback = tmp_path / "token.json"
    store = TokenStore(fallback_path=fallback)
    token = Token(access_token="access", refresh_token="refresh", email="a@example.com")
    store.save(token)
    assert fallback.is_file()
    if os.name == "nt":
        assert b"access" not in fallback.read_bytes()
    else:
        assert fallback.stat().st_mode & 0o777 == 0o600
    loaded = store.load()
    assert loaded is not None
    assert loaded.access_token == "access"
    assert store.delete() is False
    assert not fallback.exists()


def test_token_store_delete_reports_persistent_keyring_value(
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
    store = TokenStore(fallback_path=tmp_path / "token.json")
    store.save(Token(access_token="access", refresh_token="refresh"))
    assert store.delete() is False
    assert store.load() is not None


def test_token_from_payload() -> None:
    jwt = make_jwt({"email": "a@example.com"})
    settings = Settings()
    from tarklone.auth import OAuthClient

    client = OAuthClient(settings)
    try:
        token = client._token_from_payload(
            {
                "access_token": jwt,
                "refresh_token": "refresh",
                "expires_in": "3600",
                "token_type": "Bearer",
            }
        )
    finally:
        client.close()
    assert token.email == "a@example.com"
    assert token.refresh_token == "refresh"
    assert token.expires_in > 3000


def test_logout_revokes_local_server_session_and_clears_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = Token(access_token="access", refresh_token="refresh")

    class Store:
        def __init__(self) -> None:
            self.deleted = False

        def load(self):
            return token

        def delete(self):
            self.deleted = True
            return True

    class Http:
        def __init__(self) -> None:
            self.request = None

        def post(self, url, *, headers):
            self.request = (url, headers)
            return type("Response", (), {"status_code": 204})()

        def close(self):
            pass

    store = Store()
    http = Http()
    monkeypatch.setattr("tarklone.auth.save_settings", lambda _settings: None)
    client = OAuthClient(Settings(), http=http, token_store=store)
    try:
        assert client.logout() == (True, True)
    finally:
        client.close()
    assert store.deleted
    assert http.request[0].endswith("/logout?scope=local")
    assert http.request[1]["Authorization"] == "Bearer access"


def test_callback_url_is_localhost_only() -> None:
    # Der Callback darf niemals auf eine externe Adresse zeigen.
    assert urlparse("http://127.0.0.1:53682/callback").hostname == "127.0.0.1"
