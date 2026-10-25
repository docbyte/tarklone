from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import stat
import tempfile
import threading
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .config import Settings, data_dir, save_settings

SUPABASE_URL = "https://dzvfdojgelkzppeaqncy.supabase.co"
AUTH_BASE = f"{SUPABASE_URL}/auth/v1"
REGISTER_URL = f"{AUTH_BASE}/oauth/clients/register"
AUTHORIZE_URL = f"{AUTH_BASE}/oauth/authorize"
TOKEN_URL = f"{AUTH_BASE}/oauth/token"
USERINFO_URL = f"{AUTH_BASE}/oauth/userinfo"
LOGOUT_URL = f"{AUTH_BASE}/logout?scope=local"

# Öffentlicher Supabase-Anon-Key der Tarkle-Web-App. Kein Geheimnis.
ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImR6dmZkb2pnZWxrenBwZWFxbmN5Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3MjA0NjQ2OTEsImV4cCI6MjAzNjA0MDY5MX0."
    "OuhAmaANgc8Fhx_2vaPb0IEShtgWbfLDkCihOuB3Ces"
)

SERVICE_NAME = "tarklone"
TOKEN_USERNAME = "oauth"
OAUTH_SCOPE = "email profile offline_access"
_DPAPI_PREFIX = b"TARKLONE-DPAPI1:"
logger = logging.getLogger("tarklone.auth")


class AuthError(RuntimeError):
    pass


class NotLoggedIn(AuthError):
    pass


class TokenStoreError(AuthError):
    pass


@dataclass
class Token:
    access_token: str
    refresh_token: str = ""
    expires_at: float = 0.0
    token_type: str = "Bearer"
    scope: str = ""
    email: str = ""

    @property
    def expires_in(self) -> int:
        return max(0, int(self.expires_at - time.time()))

    def is_expired(self, skew_seconds: int = 60) -> bool:
        return bool(self.expires_at) and self.expires_at <= time.time() + skew_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "token_type": self.token_type,
            "scope": self.scope,
            "email": self.email,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Token":
        return cls(
            access_token=str(data.get("access_token", "")),
            refresh_token=str(data.get("refresh_token", "")),
            expires_at=float(data.get("expires_at", 0) or 0),
            token_type=str(data.get("token_type", "Bearer")),
            scope=str(data.get("scope", "")),
            email=str(data.get("email", "")),
        )


def jwt_claims(token: str) -> dict[str, Any]:
    """Liest die Claims eines JWT ohne Signaturprüfung (nur für Anzeige)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return claims if isinstance(claims, dict) else {}
    except (IndexError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _email_from_tokens(*tokens: str) -> str:
    for token in tokens:
        if not token:
            continue
        claims = jwt_claims(token)
        email = claims.get("email")
        if isinstance(email, str) and email:
            return email
    return ""


class TokenStore:
    """Speichert Tokens im OS-Keyring, mit einem geschützten Dateifallback.

    Der Datei-Fallback ist für headless Linux-/WSL-Systeme ohne
    SecretService/Keyring gedacht. Auf Windows wird der Fallback zusätzlich
    mit DPAPI an das aktuelle Benutzerkonto gebunden.
    """

    def __init__(
        self,
        service: str = SERVICE_NAME,
        username: str = TOKEN_USERNAME,
        fallback_path: Path | None = None,
    ):
        self.service = service
        self.username = username
        self.fallback_path = fallback_path or (data_dir() / "token.json")

    def _parse(self, raw: str | None) -> Token | None:
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict) or not data.get("access_token"):
            return None
        return Token.from_dict(data)

    @staticmethod
    def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
        if os.name != "nt":
            raise TokenStoreError("DPAPI ist nur unter Windows verfügbar.")
        import ctypes
        from ctypes import wintypes

        class DataBlob(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(wintypes.BYTE)),
            ]

        def make_blob(value: bytes):
            buffer = (wintypes.BYTE * len(value)).from_buffer_copy(value)
            return DataBlob(
                len(value), ctypes.cast(buffer, ctypes.POINTER(wintypes.BYTE))
            ), buffer

        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        input_blob, _input_buffer = make_blob(data)
        output_blob = DataBlob()
        flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
        if decrypt:
            description = wintypes.LPWSTR()
            crypt32.CryptUnprotectData.argtypes = [
                ctypes.POINTER(DataBlob),
                ctypes.POINTER(wintypes.LPWSTR),
                ctypes.POINTER(DataBlob),
                wintypes.LPVOID,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(DataBlob),
            ]
            crypt32.CryptUnprotectData.restype = wintypes.BOOL
            ok = crypt32.CryptUnprotectData(
                ctypes.byref(input_blob),
                ctypes.byref(description),
                None,
                None,
                None,
                flags,
                ctypes.byref(output_blob),
            )
        else:
            crypt32.CryptProtectData.argtypes = [
                ctypes.POINTER(DataBlob),
                wintypes.LPCWSTR,
                ctypes.POINTER(DataBlob),
                wintypes.LPVOID,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(DataBlob),
            ]
            crypt32.CryptProtectData.restype = wintypes.BOOL
            ok = crypt32.CryptProtectData(
                ctypes.byref(input_blob),
                "tarklone OAuth token",
                None,
                None,
                None,
                flags,
                ctypes.byref(output_blob),
            )
        if not ok:
            raise TokenStoreError(
                f"Windows-DPAPI fehlgeschlagen (Fehler {ctypes.get_last_error()})."
            )
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
            kernel32.LocalFree.restype = wintypes.HLOCAL
            kernel32.LocalFree(ctypes.cast(output_blob.pbData, wintypes.HLOCAL))
            if decrypt and description:
                kernel32.LocalFree(ctypes.cast(description, wintypes.HLOCAL))

    def _encode_fallback(self, raw: str) -> bytes:
        payload = raw.encode("utf-8")
        if os.name == "nt":
            protected = self._dpapi(payload, decrypt=False)
            return _DPAPI_PREFIX + base64.b64encode(protected)
        return payload

    def _decode_fallback(self, content: bytes) -> str:
        if content.startswith(_DPAPI_PREFIX):
            try:
                protected = base64.b64decode(content[len(_DPAPI_PREFIX) :], validate=True)
                return self._dpapi(protected, decrypt=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as exc:
                raise TokenStoreError("DPAPI-Token-Fallback ist beschädigt.") from exc
        try:
            raw = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TokenStoreError("Token-Fallback ist beschädigt.") from exc
        if os.name == "nt":
            # Migrate older plaintext Windows fallbacks immediately.
            self._save_fallback(raw)
        return raw

    def load(self) -> Token | None:
        raw_keyring = None
        try:
            import keyring

            raw_keyring = keyring.get_password(self.service, self.username)
        except Exception:
            raw_keyring = None
        token = self._parse(raw_keyring)
        if token is not None:
            try:
                self.fallback_path.unlink(missing_ok=True)
            except OSError:
                pass
            return token
        try:
            metadata = self.fallback_path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                return None
            if os.name != "nt":
                self.fallback_path.chmod(0o600)
            raw_file = self._decode_fallback(self.fallback_path.read_bytes())
        except TokenStoreError as exc:
            logger.warning("Gespeicherte Zugangsdaten sind nicht lesbar: %s", exc)
            return None
        except OSError:
            return None
        return self._parse(raw_file)

    def _save_fallback(self, raw: str) -> None:
        self.fallback_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            self.fallback_path.parent.chmod(0o700)
        payload = self._encode_fallback(raw)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.fallback_path.name}-",
            suffix=".tmp",
            dir=self.fallback_path.parent,
        )
        temp = Path(temp_name)
        try:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.fallback_path)
            if os.name != "nt":
                self.fallback_path.chmod(0o600)
        finally:
            temp.unlink(missing_ok=True)

    def save(self, token: Token) -> None:
        raw = json.dumps(token.to_dict(), ensure_ascii=False)
        stored = False
        try:
            import keyring

            keyring.set_password(self.service, self.username, raw)
            stored = keyring.get_password(self.service, self.username) == raw
        except Exception:
            stored = False
        if stored:
            try:
                self.fallback_path.unlink(missing_ok=True)
            except OSError:
                pass
        else:
            # Do not leave a known stale keyring token taking precedence over
            # the new fallback value.
            try:
                import keyring

                current = keyring.get_password(self.service, self.username)
            except Exception:
                current = None
                keyring = None
            if current is not None and keyring is not None:
                try:
                    keyring.delete_password(self.service, self.username)
                except Exception:
                    pass
                try:
                    cleared = keyring.get_password(self.service, self.username) is None
                except Exception:
                    cleared = False
                if not cleared:
                    raise TokenStoreError(
                        "Ein alter Token konnte nicht sicher aus dem Keyring "
                        "entfernt werden; der neue Token wurde nicht gespeichert."
                    )
            self._save_fallback(raw)

    def delete(self) -> bool:
        fallback_removed = True
        try:
            self.fallback_path.unlink(missing_ok=True)
        except OSError:
            fallback_removed = False
        keyring_empty = False
        try:
            import keyring

            try:
                keyring.delete_password(self.service, self.username)
            except Exception:
                pass
            keyring_empty = keyring.get_password(self.service, self.username) is None
        except Exception:
            keyring_empty = False
        return fallback_removed and keyring_empty


class _CallbackServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int]):
        super().__init__(server_address, _CallbackHandler)
        self.result: dict[str, list[str]] = {}
        self.event = threading.Event()


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        params = parse_qs(parsed.query)
        self.server.result = params  # type: ignore[attr-defined]
        self.server.event.set()  # type: ignore[attr-defined]

        body = """<!doctype html>
<html lang="de"><head><meta charset="utf-8"><title>tarklone</title>
<style>
body{font-family:Segoe UI,sans-serif;background:#f6f5fb;color:#17151f;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.card{background:#fff;border:1px solid #e6e2f0;border-radius:16px;
padding:32px 40px;box-shadow:0 10px 40px rgba(60,40,110,.12);text-align:center}
h1{font-size:20px;margin:0 0 8px}p{color:#625d73;margin:0}
</style></head><body><div class="card">
<h1>Anmeldung erfolgreich</h1>
<p>Du kannst dieses Fenster schließen und zu tarklone zurückkehren.</p>
</div></body></html>"""
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        return


class OAuthClient:
    def __init__(
        self,
        settings: Settings,
        http: httpx.Client | None = None,
        token_store: TokenStore | None = None,
    ):
        self.settings = settings
        self.http = http or httpx.Client(timeout=30.0, follow_redirects=False)
        self.token_store = token_store or TokenStore()

    def close(self) -> None:
        self.http.close()

    def _register_client(self, redirect_uri: str) -> str:
        payload = {
            "client_name": "tarklone",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": OAUTH_SCOPE,
        }
        try:
            response = self.http.post(
                REGISTER_URL,
                json=payload,
                headers={
                    "apikey": ANON_KEY,
                    "Authorization": f"Bearer {ANON_KEY}",
                    "Accept": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise AuthError(f"OAuth-Client konnte nicht registriert werden: {exc}") from exc
        if response.status_code not in (200, 201):
            raise AuthError(
                "OAuth-Client konnte nicht registriert werden "
                f"(HTTP {response.status_code})."
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise AuthError("Ungültige Antwort bei der OAuth-Registrierung.") from exc
        client_id = data.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            raise AuthError("OAuth-Registrierung lieferte keine Client-ID.")
        return client_id

    def login(
        self,
        timeout_seconds: int = 300,
        open_browser: bool = True,
        on_url: Callable[[str], None] | None = None,
    ) -> Token:
        try:
            server = _CallbackServer(("127.0.0.1", self.settings.oauth_redirect_port))
        except OSError:
            server = _CallbackServer(("127.0.0.1", 0))
        port = int(server.server_address[1])
        redirect_uri = f"http://127.0.0.1:{port}/callback"

        client_id = self.settings.oauth_client_id
        if not client_id or self.settings.oauth_redirect_uri != redirect_uri:
            client_id = self._register_client(redirect_uri)
            self.settings.oauth_client_id = client_id
            self.settings.oauth_redirect_uri = redirect_uri
            self.settings.oauth_redirect_port = port
            save_settings(self.settings)

        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        state = secrets.token_urlsafe(32)
        query = urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": OAUTH_SCOPE,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
            }
        )
        authorize_url = f"{AUTHORIZE_URL}?{query}"

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            if on_url:
                on_url(authorize_url)
            if open_browser:
                webbrowser.open(authorize_url, new=2)
            if not server.event.wait(timeout_seconds):
                raise AuthError(
                    "Zeitüberschreitung beim Browser-Login. "
                    "Bitte den Login erneut starten."
                )
            result = server.result
        finally:
            server.shutdown()
            server.server_close()

        if "error" in result:
            description = result.get("error_description", result.get("error", ["Unbekannt"])[0])
            raise AuthError(f"Tarkle-Login abgebrochen: {description}")
        if result.get("state", [""])[0] != state:
            raise AuthError("Ungültiger OAuth-Status. Bitte den Login erneut starten.")
        code = result.get("code", [""])[0]
        if not code:
            raise AuthError("Der Browser-Login lieferte keinen Autorisierungscode.")

        token = self._exchange_code(code, verifier, redirect_uri)
        self.token_store.save(token)
        if token.email:
            self.settings.account_email = token.email
            save_settings(self.settings)
        return token

    def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        headers = {
            "apikey": ANON_KEY,
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            response = self.http.post(TOKEN_URL, data=data, headers=headers)
        except httpx.HTTPError as exc:
            raise AuthError(f"Token-Anfrage fehlgeschlagen: {exc}") from exc

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        # Nur bei einem Formatproblem erneut mit JSON versuchen. Ein 5xx oder
        # invalid_grant darf nicht wiederholt werden: Der Autorisierungscode
        # ist nach dem ersten Versuch verbraucht.
        if (
            response.status_code in (400, 415)
            and payload.get("error") in ("invalid_request", "unsupported_grant_type", None)
            and not payload.get("access_token")
        ):
            try:
                retry = self.http.post(
                    TOKEN_URL,
                    json=data,
                    headers={**headers, "Content-Type": "application/json"},
                )
            except httpx.HTTPError as exc:
                raise AuthError(f"Token-Anfrage fehlgeschlagen: {exc}") from exc
            try:
                retry_payload = retry.json()
            except ValueError:
                retry_payload = {}
            if retry.status_code < 400 and retry_payload.get("access_token"):
                return retry_payload
            payload = retry_payload
            response = retry

        if response.status_code >= 500:
            message = payload.get("msg") or payload.get("error_description") or "Serverfehler"
            raise AuthError(f"Tarkle-Anmeldung fehlgeschlagen: {message}")
        if response.status_code >= 400 or payload.get("error"):
            if payload.get("error") == "invalid_grant":
                raise AuthError(
                    "Der Tarkle-Anmeldecode ist ungültig oder abgelaufen. "
                    "Bitte den Login erneut starten und den Browser-Tab zügig abschließen."
                )
            message = payload.get("error_description") or payload.get("error") or "Unbekannt"
            raise AuthError(f"Tarkle-Anmeldung fehlgeschlagen: {message}")
        if not payload.get("access_token"):
            raise AuthError("Tarkle lieferte kein Zugriffstoken.")
        return payload

    def _exchange_code(self, code: str, verifier: str, redirect_uri: str) -> Token:
        payload = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": self.settings.oauth_client_id,
                "code_verifier": verifier,
            }
        )
        return self._token_from_payload(payload)

    def _token_from_payload(self, payload: dict[str, Any]) -> Token:
        expires_in = payload.get("expires_in", 3600)
        try:
            expires_in = int(expires_in)
        except (TypeError, ValueError):
            expires_in = 3600
        access_token = str(payload.get("access_token", ""))
        refresh_token = str(payload.get("refresh_token", ""))
        id_token = str(payload.get("id_token", ""))
        return Token(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=time.time() + max(0, expires_in - 30),
            token_type=str(payload.get("token_type", "Bearer")),
            scope=str(payload.get("scope", "")),
            email=_email_from_tokens(id_token, access_token),
        )

    def refresh(self, token: Token) -> Token:
        if not token.refresh_token:
            raise NotLoggedIn("Die Sitzung ist abgelaufen. Bitte erneut anmelden.")
        payload = self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": token.refresh_token,
                "client_id": self.settings.oauth_client_id,
            }
        )
        refreshed = self._token_from_payload(payload)
        if not refreshed.refresh_token:
            refreshed.refresh_token = token.refresh_token
        if not refreshed.email:
            refreshed.email = token.email
        self.token_store.save(refreshed)
        return refreshed

    def ensure_valid(self) -> Token:
        token = self.token_store.load()
        if token is None:
            raise NotLoggedIn("Noch nicht bei Tarkle angemeldet.")
        if token.is_expired():
            token = self.refresh(token)
        return token

    def logout(self) -> tuple[bool, bool]:
        """Revokes the current server session when possible, then clears local storage.

        Returns ``(local_deleted, remote_revoked)`` so callers never imply a
        complete logout when either operation could not be verified.
        """
        token = self.token_store.load()
        remote_revoked = token is None
        if token is not None:
            try:
                response = self.http.post(
                    LOGOUT_URL,
                    headers={
                        "apikey": ANON_KEY,
                        "Authorization": f"Bearer {token.access_token}",
                    },
                )
                remote_revoked = response.status_code < 400 or response.status_code == 401
            except httpx.HTTPError:
                remote_revoked = False
        local_deleted = self.token_store.delete()
        self.settings.account_email = ""
        save_settings(self.settings)
        return local_deleted, remote_revoked
