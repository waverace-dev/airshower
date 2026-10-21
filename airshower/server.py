"""Small standard-library HTTP/SSE transport for the Airshower reference.

Bind to loopback by default. Internet deployment requires an authenticated,
TLS-protected deployment design; this is not a hardened public service.
"""

import json
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from urllib.parse import parse_qs, quote, urlsplit

from . import __version__
from .errors import APIError, invalid, missing

MAX_BODY_BYTES = 7 * 1024 * 1024 + 65536
HEARTBEAT_SECONDS = 15


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _reject_constant(value):
    raise ValueError("Non-finite JSON value")


def _unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


class AirshowerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, store, public_url=None):
        self.store = store
        self.consent = None
        if public_url is not None:
            from .consent import ConsentBroker
            self.consent = ConsentBroker(store, public_url)
        self.stopping = threading.Event()
        super().__init__(address, handler)

    def server_bind(self):
        # HTTPServer performs a reverse DNS lookup here only to populate a
        # display name. The relay uses its configured public origin instead;
        # startup must not depend on a working reverse resolver.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def shutdown(self):
        self.stopping.set()
        with self.store.condition:
            self.store.condition.notify_all()
        super().shutdown()

    def server_close(self):
        self.stopping.set()
        with self.store.condition:
            self.store.condition.notify_all()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Airshower/0.1"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, format, *args):
        # URLs can contain secrets submitted in error. Never write request lines.
        pass

    def _headers(self, status, content_type, length=None, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        if length is not None:
            self.send_header("Content-Length", str(length))
        if self.close_connection and not (extra and "Connection" in extra):
            self.send_header("Connection", "close")
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.end_headers()

    def _respond(self, status, value):
        body = _json(value)
        self._headers(status, "application/json; charset=utf-8", len(body))
        if self.command != "HEAD":
            self.wfile.write(body)

    def _token(self):
        values = self.headers.get_all("Authorization", [])
        if len(values) != 1 or not values[0].startswith("Bearer ") or not values[0][7:]:
            raise APIError(401, "unauthorized", "A valid bearer token is required.")
        token = values[0][7:]
        if token.strip() != token:
            raise APIError(401, "unauthorized", "A valid bearer token is required.")
        return token

    def _location(self):
        try:
            location = urlsplit(self.path)
            query = parse_qs(location.query, keep_blank_values=True, strict_parsing=True, max_num_fields=8) if location.query else {}
        except ValueError:
            raise invalid("Invalid request URL.")
        if location.scheme or location.netloc or location.fragment:
            raise invalid("Use an origin-relative API URL.")
        # No API accepts credentials in query strings. Allow only event cursors.
        allowed = {"after"} if location.path in ("/v1/events", "/v1/events/history") else set()
        if set(query) - allowed or any(len(values) != 1 for values in query.values()):
            raise invalid("Unsupported URL query parameters.")
        if "%" in location.path or "//" in location.path or location.path.endswith("/"):
            raise invalid("Use the canonical API path.")
        return location.path, query

    def _body(self):
        if self.headers.get("Transfer-Encoding") is not None:
            raise invalid("Chunked request bodies are not supported.")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) > 1:
            raise invalid("Only one Content-Length is permitted.")
        raw_length = lengths[0] if lengths else "0"
        if not re.fullmatch(r"[0-9]{1,10}", raw_length):
            raise invalid("A valid Content-Length is required.")
        size = int(raw_length)
        if size > MAX_BODY_BYTES:
            raise APIError(413, "body_too_large", "Request body exceeds its size limit.")
        if self.command != "POST":
            if size:
                raise invalid("This endpoint does not accept a request body.")
            return None
        if not size:
            return {}
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise APIError(415, "unsupported_media_type", "Use application/json.")
        try:
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError("Incomplete request body")
            value = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant, object_pairs_hook=_unique_object)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise invalid("Request body must be a valid JSON object.")
        if not isinstance(value, dict):
            raise invalid("Request body must be a JSON object.")
        return value

    def _after(self, query):
        value = query.get("after", [self.headers.get("Last-Event-ID", "0")])[0]
        if not re.fullmatch(r"[0-9]{1,19}", value):
            raise invalid("after must be a nonnegative event identifier.")
        after = int(value)
        if after > 9223372036854775807:
            raise invalid("after must be a nonnegative event identifier.")
        return after

    def _events(self, token, actor, after):
        # Validate actor and cursor before starting a success response.
        pending = self.server.store.events(actor, after)
        self._headers(200, "text/event-stream; charset=utf-8", extra={
            "Connection": "close", "X-Accel-Buffering": "no"
        })
        self.close_connection = True
        self.wfile.write(b": connected\n\n")
        self.wfile.flush()
        heartbeat_at = time.monotonic() + HEARTBEAT_SECONDS
        while not self.server.stopping.is_set() and not self.server.store.closed:
            # Recheck credentials on every emission/wakeup, rather than treating
            # an open stream as a permanent authorization bypass.
            actor = self.server.store.authenticate(token)
            if pending:
                for event in pending:
                    payload = b"id: " + str(event["id"]).encode("ascii") + b"\nevent: " + event["type"].encode("ascii")
                    self.wfile.write(payload + b"\ndata: " + _json(event) + b"\n\n")
                    after = event["id"]
                self.wfile.flush()
                pending = self.server.store.events(actor, after)
                if pending:
                    continue
            now = time.monotonic()
            if now >= heartbeat_at:
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
                heartbeat_at = now + HEARTBEAT_SECONDS
            # Recheck while holding the shared condition so an event cannot be
            # committed between the empty read and sleeping (lost-wakeup race).
            with self.server.store.condition:
                pending = self.server.store.events(actor, after)
                if not pending and not self.server.stopping.is_set() and not self.server.store.closed:
                    self.server.store.condition.wait(max(0.05, heartbeat_at - time.monotonic()))
                    if not self.server.store.closed:
                        actor = self.server.store.authenticate(token)
                        pending = self.server.store.events(actor, after)

    def _handle(self):
        streaming = False
        try:
            path, query = self._location()
            if path.startswith("/consent/") and self.server.consent is not None:
                return self.server.consent.handle_public(self, path)
            # Cross-origin/browser cookie authentication is deliberately absent.
            if self.headers.get("Origin") is not None:
                raise APIError(403, "forbidden", "Browser-origin requests are not enabled.")
            data = self._body()
            if path == "/health" and self.command == "GET":
                return self._respond(200, {"ok": True, "version": __version__, "protocol": "airshower/0.1"})
            token = self._token()
            if path == "/v1/enroll" and self.command == "POST":
                status, value = self.server.store.enroll(token, data)
                return self._respond(status, value)
            actor = self.server.store.authenticate(token)
            if self.server.consent is not None:
                if path == "/v1/consents" and self.command == "POST":
                    return self._respond(201, self.server.consent.create(actor, data))
                if path.startswith("/v1/consents/") and path.endswith("/cancel") and self.command == "POST":
                    identifier = path[len("/v1/consents/"):-len("/cancel")]
                    return self._respond(200, self.server.consent.cancel(actor, identifier, data))
                if path.startswith("/v1/consents/") and self.command == "GET":
                    return self._respond(200, self.server.consent.status(actor, path[len("/v1/consents/"):]))
            if path in ("/v1/events", "/v1/events/history") and self.command == "GET":
                after = self._after(query)
                if path.endswith("/history"):
                    return self._respond(200, {"events": self.server.store.events(actor, after)})
                # Validate before setting streaming to avoid suppressing errors.
                self.server.store.events(actor, after)
                streaming = True
                return self._events(token, actor, after)
            parts = path.strip("/").split("/")
            if len(parts) == 5 and parts[:2] == ["v1", "requests"] and parts[3] == "files" and self.command == "GET":
                raw, metadata = self.server.store.file(parts[2], parts[4], actor)
                filename = metadata["filename"]
                fallback = "".join(char if 32 <= ord(char) < 127 and char not in '\\"' else "_" for char in filename)
                disposition = 'attachment; filename="%s"; filename*=UTF-8\'\'%s' % (fallback, quote(filename, safe=""))
                self._headers(200, metadata["media_type"], len(raw), {"Content-Disposition": disposition, "X-Content-SHA256": metadata["sha256"]})
                return self.wfile.write(raw)
            status, value = self.server.store.dispatch(self.command, path, actor, data)
            self._respond(status, value)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except APIError as error:
            self.close_connection = True
            if not streaming:
                try:
                    self._respond(error.status, {"error": {"code": error.code, "message": error.message}})
                except (BrokenPipeError, ConnectionResetError):
                    pass
        except Exception:
            # A malformed request must never disclose SQL, filesystem paths,
            # stored source material, bearer tokens or Python tracebacks.
            self.close_connection = True
            if not streaming:
                try:
                    self._respond(500, {"error": {"code": "internal_error", "message": "An internal error occurred."}})
                except (BrokenPipeError, ConnectionResetError):
                    pass

    do_GET = _handle
    do_POST = _handle
    do_DELETE = _handle

    def do_OPTIONS(self):
        # Unsupported methods may leave an unread body. Never parse those
        # bytes as another request on a persistent connection.
        self.close_connection = True
        self._respond(405, {"error": {"code": "method_not_allowed", "message": "This HTTP method is not supported."}})

    do_PUT = do_OPTIONS
    do_PATCH = do_OPTIONS
    do_HEAD = do_OPTIONS


def create_server(store, host="127.0.0.1", port=0, public_url=None):
    """Create a server; the caller owns serve_forever, shutdown and Store.close."""
    return AirshowerServer((host, port), Handler, store, public_url)
