"""Small, dependency-free client for the Airshower reference API.

Credentials belong in the Authorization header. This client refuses redirects
and cleartext connections to non-loopback hosts so a typo cannot silently send a
bearer token elsewhere. It never reads a local file on behalf of a remote agent.
"""

import ipaddress
import json
import socket
from http.client import HTTPException
from typing import Any, Dict, Iterator, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class AirshowerError(Exception):
    """Base class for client failures safe to display without credentials."""


class APIError(AirshowerError):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__("HTTP {} {}: {}".format(status, code, message))


class TransportError(AirshowerError):
    """The server could not be reached, or its response was malformed."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Client:
    """An actor-scoped HTTP client; create one per owner or agent token."""

    def __init__(self, base_url: str, token: str, timeout: float = 15.0):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Server URL must be HTTP(S), without credentials, query, or fragment")
        if parsed.scheme == "http" and not _loopback(parsed.hostname):
            raise ValueError("Remote servers require HTTPS; HTTP is allowed only on loopback")
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise ValueError("A nonempty bearer token without whitespace is required")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout
        self._opener = build_opener(_NoRedirect())

    def __repr__(self) -> str:
        return "Client(base_url={!r}, token=<redacted>)".format(self.base_url)

    def _open(self, method: str, path: str, payload=None, headers=None, timeout=None):
        parsed = urlsplit(path)
        if not path.startswith("/") or path.startswith("//") or parsed.scheme or parsed.netloc or parsed.fragment:
            raise ValueError("API path must be a relative path beginning with one slash")
        request_headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json"}
        if headers:
            request_headers.update(headers)
        data = None
        if payload is not None:
            data = json.dumps(payload, allow_nan=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = Request(self.base_url + path, data=data, headers=request_headers, method=method.upper())
        try:
            return self._opener.open(request, timeout=self.timeout if timeout is None else timeout)
        except HTTPError as exc:
            try:
                error = json.loads(exc.read(64 * 1024)).get("error", {})
                code = error.get("code", "http_error")
                message = error.get("message", "Server rejected the request")
                if not isinstance(code, str) or not isinstance(message, str):
                    raise ValueError("invalid error")
            except (ValueError, AttributeError, UnicodeError):
                code, message = "http_error", "Server rejected the request"
            finally:
                exc.close()
            raise APIError(exc.code, code, message) from None
        except (URLError, OSError, socket.timeout, HTTPException):
            raise TransportError("Connection failed; check the server URL, TLS, and network") from None

    def request(self, method: str, path: str, payload=None) -> Any:
        try:
            with self._open(method, path, payload) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, socket.timeout, HTTPException):
            raise TransportError("Connection interrupted while reading the response") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise TransportError("Server response exceeds the client size limit")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise TransportError("Server returned invalid JSON") from None

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, payload: Dict[str, Any]) -> Any:
        return self.request("POST", path, payload)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)

    def download(self, request_id: str, source_id: str) -> bytes:
        path = "/v1/requests/{}/files/{}".format(quote(request_id, safe=""), quote(source_id, safe=""))
        try:
            with self._open("GET", path, headers={"Accept": "application/octet-stream"}) as response:
                content = response.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, socket.timeout, HTTPException):
            raise TransportError("Connection interrupted during file transfer") from None
        if len(content) > MAX_RESPONSE_BYTES:
            raise TransportError("File exceeds the client size limit")
        return content

    def events(self, after: int = 0, timeout: float = 45.0) -> Iterator[Dict[str, Any]]:
        """Stream filtered events, replaying everything visible after the cursor.

        The caller owns reconnect policy and should persist the last processed
        event ID. Closing the iterator closes its network connection.
        """
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("Event cursor must be a nonnegative integer")
        headers = {"Accept": "text/event-stream", "Last-Event-ID": str(after)}
        response = self._open("GET", "/v1/events?after={}".format(after), headers=headers, timeout=timeout)
        if response.headers.get_content_type() != "text/event-stream":
            response.close()
            raise TransportError("Server did not return an event stream")
        data_lines = []
        event_id = None
        event_type = None
        frame_bytes = 0
        try:
            with response:
                while True:
                    raw = response.readline(64 * 1024 + 1)
                    if not raw:
                        return
                    frame_bytes += len(raw)
                    if frame_bytes > 64 * 1024:
                        raise TransportError("Server sent an oversized event")
                    line = raw.decode("utf-8").rstrip("\r\n")
                    if not line:
                        if data_lines:
                            try:
                                event = json.loads("\n".join(data_lines))
                                if not isinstance(event, dict):
                                    raise ValueError("invalid event")
                                if "id" not in event and event_id is not None:
                                    event["id"] = int(event_id)
                                if "type" not in event and event_type:
                                    event["type"] = event_type
                                if not isinstance(event.get("id"), int) or isinstance(event["id"], bool):
                                    raise ValueError("invalid event id")
                            except (ValueError, TypeError):
                                raise TransportError("Server returned an invalid event") from None
                            yield event
                        data_lines, event_id, event_type, frame_bytes = [], None, None, 0
                    elif not line.startswith(":"):
                        field, _, value = line.partition(":")
                        if value.startswith(" "):
                            value = value[1:]
                        if field == "data":
                            data_lines.append(value)
                        elif field == "id":
                            event_id = value
                        elif field == "event":
                            event_type = value
        except (OSError, socket.timeout, UnicodeError, HTTPException):
            raise TransportError("Event stream interrupted; reconnect with the last processed cursor") from None
