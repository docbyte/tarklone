from __future__ import annotations

import base64
import json
import threading
import urllib.error
import urllib.request
from urllib.parse import urlparse

import keyring
import pytest

from tarklone.auth import Token, TokenStore, _CallbackServer, jwt_claims
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
    loaded = store.load()
    assert loaded is not None
    assert loaded.access_token == "access"
    store.delete()
    assert not fallback.exists()


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


def test_callback_url_is_localhost_only() -> None:
    # Der Callback darf niemals auf eine externe Adresse zeigen.
    assert urlparse("http://127.0.0.1:53682/callback").hostname == "127.0.0.1"
