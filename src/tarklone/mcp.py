from __future__ import annotations

import json
from typing import Any

import httpx

PROTOCOL_VERSION = "2025-06-18"


class McpError(RuntimeError):
    pass


class McpAuthError(McpError):
    pass


class McpClient:
    def __init__(
        self,
        url: str,
        access_token: str,
        client_name: str = "tarklone",
        client_version: str = "0.1.0",
        http: httpx.Client | None = None,
    ):
        self.url = url
        self.access_token = access_token
        self.client_name = client_name
        self.client_version = client_version
        self.http = http or httpx.Client(timeout=120.0)
        self.session_id = ""
        self.protocol_version = PROTOCOL_VERSION
        self._next_id = 0
        self._initialized = False

    def close(self) -> None:
        self.http.close()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": f"{self.client_name}/{self.client_version}",
        }
        if self._initialized:
            headers["MCP-Protocol-Version"] = self.protocol_version
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    @staticmethod
    def _parse_sse(text: str) -> list[Any]:
        messages: list[Any] = []
        data_lines: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.rstrip("\r")
            if not line:
                if data_lines:
                    payload = "\n".join(data_lines)
                    try:
                        messages.append(json.loads(payload))
                    except ValueError:
                        pass
                    data_lines = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            try:
                messages.append(json.loads("\n".join(data_lines)))
            except ValueError:
                pass
        return messages

    def _post(self, payload: dict[str, Any], expect_response: bool = True) -> Any:
        try:
            response = self.http.post(self.url, json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            raise McpError(f"MCP-Verbindung fehlgeschlagen: {exc}") from exc

        session_id = response.headers.get("Mcp-Session-Id")
        if session_id:
            self.session_id = session_id

        if response.status_code == 401:
            raise McpAuthError(
                "Die Tarkle-Sitzung ist abgelaufen. Bitte in tarklone erneut anmelden."
            )
        if response.status_code >= 400:
            detail = response.text[:500]
            raise McpError(f"MCP-Anfrage fehlgeschlagen (HTTP {response.status_code}): {detail}")
        if not expect_response or response.status_code == 202 or not response.content:
            return None

        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            messages = self._parse_sse(response.text)
            if not messages:
                raise McpError("Der MCP-Server lieferte eine leere SSE-Antwort.")
            request_id = payload.get("id")
            for message in messages:
                if isinstance(message, dict) and message.get("id") == request_id:
                    return self._unwrap(message)
            return self._unwrap(messages[-1])

        try:
            message = response.json()
        except ValueError as exc:
            raise McpError("Der MCP-Server lieferte keine gültige JSON-Antwort.") from exc
        return self._unwrap(message)

    @staticmethod
    def _unwrap(message: Any) -> Any:
        if not isinstance(message, dict):
            raise McpError("Unerwartete MCP-Antwort.")
        error = message.get("error")
        if isinstance(error, dict):
            raise McpError(
                str(error.get("message") or error.get("code") or "Unbekannter MCP-Fehler")
            )
        if "result" not in message:
            raise McpError("MCP-Antwort ohne result.")
        return message["result"]

    def initialize(self) -> dict[str, Any]:
        if self._initialized:
            return {}
        self._next_id += 1
        result = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": self.client_name,
                        "version": self.client_version,
                    },
                },
            }
        )
        if isinstance(result, dict):
            server_version = result.get("protocolVersion")
            if isinstance(server_version, str) and server_version:
                self.protocol_version = server_version
        self._initialized = True
        try:
            self._post(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                },
                expect_response=False,
            )
        except McpError:
            # Einige Server quittieren die Notification mit einem Fehler; die
            # Session ist trotzdem nutzbar.
            pass
        return result if isinstance(result, dict) else {}

    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {}
            if cursor:
                params["cursor"] = cursor
            self._next_id += 1
            result = self._post(
                {
                    "jsonrpc": "2.0",
                    "id": self._next_id,
                    "method": "tools/list",
                    "params": params,
                }
            )
            if not isinstance(result, dict):
                break
            batch = result.get("tools")
            if isinstance(batch, list):
                tools.extend(item for item in batch if isinstance(item, dict))
            cursor_value = result.get("nextCursor")
            cursor = cursor_value if isinstance(cursor_value, str) and cursor_value else None
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        result = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        return result if isinstance(result, dict) else {"content": result}


def tool_text(result: dict[str, Any]) -> str:
    """Fasst Text-Inhalte eines MCP-Tool-Ergebnisses zusammen."""
    parts: list[str] = []
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "\n".join(parts)


def tool_json(result: dict[str, Any]) -> Any:
    """Versucht, den Text-Inhalt eines Tool-Ergebnisses als JSON zu lesen."""
    text = tool_text(result).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None
