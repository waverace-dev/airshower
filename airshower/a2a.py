"""Experimental A2A 1.0 HTTP+JSON subset over the Airshower permission API.

This facade has no privileged credential and no agent execution capability.
It forwards each caller's bearer credential to a fixed Airshower backend.
"""

import hashlib
import ipaddress
import json
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


EXTENSION_URI = (
    "https://github.com/waverace-dev/airshower/blob/main/docs/specification.md#v0.1"
)
PROFILE_VERSION = "0.1"
MAX_BODY = 65536
MAX_BACKEND_BODY = 1048576
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_STATES = {
    "pending_approval": "TASK_STATE_WORKING",
    "waiting_human": "TASK_STATE_WORKING",
    "ready": "TASK_STATE_SUBMITTED",
    "working": "TASK_STATE_WORKING",
    "completed": "TASK_STATE_COMPLETED",
    "denied": "TASK_STATE_REJECTED",
    "cancelled": "TASK_STATE_CANCELED",
}


class A2AError(Exception):
    def __init__(self, status, message, reason=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.reason = reason


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _base_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("URLs require an HTTP(S) origin, without credentials or query")
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname == "localhost"
    if parsed.scheme == "http" and not loopback:
        raise ValueError("Remote URLs require HTTPS; HTTP is allowed only on loopback")
    return value.rstrip("/")


def _identifier(value):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value) or value in (".", ".."):
        raise A2AError(400, "Identifiers must contain 1-128 safe identifier characters")
    return value


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _decode_json(raw):
    def bad_constant(value):
        raise ValueError("Non-finite JSON number")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys,
                      parse_constant=bad_constant)


def _timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, timezone.utc).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    raise ValueError("Invalid backend timestamp")


def _task(record):
    task_id = _identifier(record["id"])
    state = record["status"]
    if state not in _STATES:
        raise ValueError("Unknown backend status")
    profile = {"version": PROFILE_VERSION, "status": state, "kind": record["kind"]}
    if state in ("pending_approval", "waiting_human"):
        profile["waitingFor"] = "owner"
    attribution = record.get("attribution")
    if isinstance(attribution, dict):
        profile["attribution"] = {
            key: attribution[key] for key in ("type", "id") if key in attribution
        }
    task = {
        "id": task_id,
        "contextId": "airshower:" + task_id,
        "status": {"state": _STATES[state], "timestamp": _timestamp(record["updated_at"])},
        "metadata": {EXTENSION_URI: profile},
    }
    result = record.get("result")
    if state == "completed" and isinstance(result, dict):
        parts = []
        if isinstance(result.get("text"), str):
            parts.append({"text": result["text"], "mediaType": "text/plain"})
        files = []
        for item in result.get("files", []):
            source_id = _identifier(item.get("source_id", item.get("id")))
            descriptor = {key: item[key] for key in
                          ("filename", "media_type", "sha256", "size") if key in item}
            descriptor["sourceId"] = source_id
            descriptor["downloadPath"] = "/v1/requests/{}/files/{}".format(
                quote(task_id, safe=""), quote(source_id, safe=""))
            descriptor["authentication"] = "Airshower caller bearer; reauthorized at download"
            files.append(descriptor)
        if files:
            parts.append({"data": {"files": files}, "mediaType": "application/json"})
        if parts:
            task["artifacts"] = [{
                "artifactId": task_id + ":result", "name": "Airshower result",
                "parts": parts, "extensions": [EXTENSION_URI],
            }]
    return task


def _send_payload(body, recipient):
    if not isinstance(body, dict):
        raise A2AError(400, "Request body must be an object")
    if body.get("tenant"):
        raise A2AError(400, "This facade does not support tenant routing", "UNSUPPORTED_OPERATION")
    config = body.get("configuration", {})
    if not isinstance(config, dict):
        raise A2AError(400, "configuration must be an object")
    if config.get("taskPushNotificationConfig") is not None:
        raise A2AError(400, "Push notifications are unavailable", "PUSH_NOTIFICATION_NOT_SUPPORTED")
    if config.get("returnImmediately") is not True:
        raise A2AError(400, "This subset requires configuration.returnImmediately=true",
                       "UNSUPPORTED_OPERATION")
    history = config.get("historyLength", 0)
    if type(history) is not int or history < 0:
        raise A2AError(400, "historyLength must be a nonnegative integer")
    modes = config.get("acceptedOutputModes")
    if modes is not None and (not isinstance(modes, list) or not modes
                             or not all(isinstance(mode, str) for mode in modes)):
        raise A2AError(400, "acceptedOutputModes must be a nonempty string array")
    if modes is not None and not {"text/plain", "application/json"}.issubset(modes):
        raise A2AError(400, "This subset requires accepting text/plain and application/json",
                       "CONTENT_TYPE_NOT_SUPPORTED")
    message = body.get("message")
    if not isinstance(message, dict) or message.get("role") != "ROLE_USER":
        raise A2AError(400, "message.role must be ROLE_USER")
    message_id = _identifier(message.get("messageId"))
    if any(key in message for key in ("taskId", "contextId", "referenceTaskIds")):
        raise A2AError(400, "This subset creates independent tasks; continuation is unavailable",
                       "UNSUPPORTED_OPERATION")
    parts = message.get("parts")
    if not isinstance(parts, list) or len(parts) != 1:
        raise A2AError(400, "This subset accepts exactly one text part",
                       "CONTENT_TYPE_NOT_SUPPORTED")
    part = parts[0]
    if (not isinstance(part, dict) or not isinstance(part.get("text"), str)
            or any(key in part for key in ("data", "raw", "url"))
            or part.get("mediaType", "text/plain") != "text/plain"):
        raise A2AError(400, "Only plain text input is accepted", "CONTENT_TYPE_NOT_SUPPORTED")
    text = part["text"]
    if not text.strip() or len(text.encode("utf-8")) > 16384:
        raise A2AError(400, "Message must be nonempty and at most 16 KiB")
    metadata = message.get("metadata")
    profile = metadata.get(EXTENSION_URI) if isinstance(metadata, dict) else None
    if not isinstance(profile, dict) or profile.get("version") != PROFILE_VERSION:
        raise A2AError(400, "Message metadata must specify Airshower profile version 0.1")
    if set(profile) - {"version", "kind", "sourceIds"}:
        raise A2AError(400, "Unrecognized Airshower profile field")
    kind = profile.get("kind")
    if kind not in ("message", "ask_human", "context", "file", "task"):
        raise A2AError(400, "Unsupported Airshower request kind")
    sources = profile.get("sourceIds", [])
    if not isinstance(sources, list) or len(sources) > 100:
        raise A2AError(400, "sourceIds must be an array with at most 100 identifiers")
    sources = [_identifier(item) for item in sources]
    if len(set(sources)) != len(sources):
        raise A2AError(400, "sourceIds must be unique")
    if kind in ("context", "file") and not sources:
        raise A2AError(400, "Context and file requests require explicit sourceIds")
    if kind not in ("context", "file") and sources:
        raise A2AError(400, "Only context and file requests accept sourceIds")
    return {"recipient_agent_id": recipient, "kind": kind, "message": text,
            "source_ids": sources, "idempotency_key": "a2a:" + hashlib.sha256(
                (recipient + "\0" + message_id).encode("utf-8")).hexdigest()}


def create_a2a_server(backend_url, recipient_agent_id, public_url, host="127.0.0.1", port=0):
    """Return a stopped HTTP server; caller owns serve_forever/shutdown/server_close.

    public_url is this facade's advertised base URL (optionally with a path).
    Neither configuration nor responses include any credential.
    """
    backend_url, public_url = _base_url(backend_url), _base_url(public_url)
    recipient_agent_id = _identifier(recipient_agent_id)
    base_path = urlsplit(public_url).path.rstrip("/")

    def backend(method, path, authorization, data=None):
        raw = json.dumps(data).encode("utf-8") if data is not None else None
        request = Request(backend_url + path, data=raw, method=method, headers={
            "Authorization": authorization, "Content-Type": "application/json",
            "Accept": "application/json",
        })
        try:
            with build_opener(_NoRedirect()).open(request, timeout=15) as response:
                content = response.read(MAX_BACKEND_BODY + 1)
                if len(content) > MAX_BACKEND_BODY:
                    raise ValueError("Oversized backend response")
                value = _decode_json(content)
                if not isinstance(value, dict):
                    raise ValueError("Invalid backend object")
                return value
        except HTTPError as error:
            status = error.code
            error.close()
            if status == 401:
                raise A2AError(401, "Invalid or missing bearer credential")
            if status == 403:
                raise A2AError(403, "Airshower permission denied")
            if status == 404:
                raise A2AError(404, "Task or resource not found or inaccessible", "TASK_NOT_FOUND")
            if status == 409:
                raise A2AError(409, "Airshower request conflicts with current state")
            if status == 400:
                raise A2AError(400, "Airshower rejected the request parameters")
            raise A2AError(502, "Airshower backend unavailable")
        except (URLError, OSError, ValueError):
            raise A2AError(502, "Airshower backend unavailable or returned invalid data")

    card = {
        "name": "Airshower " + recipient_agent_id,
        "description": "Experimental consent-aware request facade. A2A 1.0 subset only.",
        "version": "0.1.0", "documentationUrl":
            "https://github.com/waverace-dev/airshower/blob/main/docs/a2a.md",
        "supportedInterfaces": [{"url": public_url, "protocolBinding": "HTTP+JSON",
                                 "protocolVersion": "1.0"}],
        "capabilities": {"streaming": False, "pushNotifications": False,
                         "extendedAgentCard": False, "extensions": [{
                             "uri": EXTENSION_URI, "required": True,
                             "description": "Airshower 0.1 consent and request intent profile",
                             "params": {"version": PROFILE_VERSION,
                                        "supportedOperations": ["SendMessage", "GetTask", "CancelTask"],
                                        "returnImmediatelyRequired": True,
                                        "continuations": False, "history": False},
                         }]},
        "securitySchemes": {"airshowerBearer": {"httpAuthSecurityScheme": {
            "scheme": "Bearer", "description": "Caller Airshower agent or owner credential"
        }}},
        "securityRequirements": [{"schemes": {"airshowerBearer": {"list": []}}}],
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": [{"id": "airshower-requests", "name": "Permission-scoped requests",
                    "description": "Submit messages, human questions, context/file requests and tasks",
                    "tags": ["consent", "human", "context", "files"]}],
    }

    class Handler(BaseHTTPRequestHandler):
        server_version = "AirshowerA2A/0.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, format, *args):
            pass

        def _reply(self, status, value, extension=False):
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/a2a+json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("A2A-Version", "1.0")
            if extension:
                self.send_header("A2A-Extensions", EXTENSION_URI)
            if status == 401:
                self.send_header("WWW-Authenticate", 'Bearer realm="airshower"')
            self.end_headers()
            self.wfile.write(raw)

        def _error(self, error):
            status_names = {400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED", 403: "PERMISSION_DENIED",
                            404: "NOT_FOUND", 409: "ABORTED", 413: "RESOURCE_EXHAUSTED",
                            500: "INTERNAL", 502: "UNAVAILABLE"}
            value = {"code": error.status, "status": status_names.get(error.status, "UNKNOWN"),
                     "message": error.message}
            if error.reason:
                value["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                     "reason": error.reason, "domain": "a2a-protocol.org"}]
            self._reply(error.status, {"error": value})

        def _body(self):
            if self.headers.get("Transfer-Encoding"):
                raise A2AError(400, "Chunked request bodies are unsupported")
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1:
                raise A2AError(400, "Exactly one Content-Length is required")
            try:
                length = int(lengths[0])
            except ValueError:
                raise A2AError(400, "Invalid Content-Length")
            if not 0 <= length <= MAX_BODY:
                raise A2AError(413, "Request body exceeds limit")
            if length and self.headers.get_content_type() not in ("application/json", "application/a2a+json"):
                raise A2AError(400, "Expected an A2A JSON body", "CONTENT_TYPE_NOT_SUPPORTED")
            try:
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("Truncated body")
                return _decode_json(raw) if raw else {}
            except (ValueError, UnicodeError):
                raise A2AError(400, "Malformed JSON object")

        def _handle(self):
            parsed = urlsplit(self.path)
            path = parsed.path
            query = parse_qs(parsed.query, keep_blank_values=True)
            if any(key not in ("A2A-Version", "historyLength") for key in query):
                raise A2AError(400, "Unsupported query parameter; credentials belong in headers")
            if any(len(values) != 1 for values in query.values()):
                raise A2AError(400, "Repeated query parameters are unsupported")
            if self.headers.get("Origin"):
                raise A2AError(403, "Browser cross-origin requests are not enabled")
            if self.command == "GET" and path in (
                    "/.well-known/agent-card.json", base_path + "/.well-known/agent-card.json"):
                self._reply(200, card)
                return
            if base_path and path.startswith(base_path + "/"):
                path = path[len(base_path):]
            elif base_path:
                raise A2AError(404, "Endpoint not found")
            header_version = self.headers.get("A2A-Version")
            query_version = query.get("A2A-Version", [None])[0]
            if header_version and query_version and header_version != query_version:
                raise A2AError(400, "Conflicting A2A versions", "VERSION_NOT_SUPPORTED")
            if (header_version or query_version or "0.3") != "1.0":
                raise A2AError(400, "Only A2A-Version 1.0 is supported", "VERSION_NOT_SUPPORTED")
            requested_extensions = self.headers.get("A2A-Extensions", "").split(",")
            if EXTENSION_URI not in [item.strip() for item in requested_extensions]:
                raise A2AError(400, "Airshower extension activation is required",
                               "EXTENSION_SUPPORT_REQUIRED")
            authorizations = self.headers.get_all("Authorization", [])
            if (len(authorizations) != 1 or not re.fullmatch(
                    r"Bearer [A-Za-z0-9._~+/=-]{1,4096}", authorizations[0])):
                raise A2AError(401, "Caller bearer credential required")
            authorization = authorizations[0]
            if "historyLength" in query:
                history = query["historyLength"][0]
                if not history.isdigit():
                    raise A2AError(400, "historyLength must be a nonnegative integer")
            if self.command == "POST" and path == "/message:send":
                payload = _send_payload(self._body(), recipient_agent_id)
                record = backend("POST", "/v1/requests", authorization, payload)
                if record.get("recipient_agent_id") != recipient_agent_id:
                    raise A2AError(502, "Backend returned an unexpected recipient")
                self._reply(200, {"task": _task(record)}, extension=True)
                return
            match = re.fullmatch(r"/tasks/([A-Za-z0-9_.-]{1,128})(:cancel)?", path)
            if match and ((self.command == "GET" and not match.group(2))
                          or (self.command == "POST" and match.group(2))):
                task_id = _identifier(match.group(1))
                endpoint = "/v1/requests/" + quote(task_id, safe="")
                record = backend("GET", endpoint, authorization)
                if record.get("recipient_agent_id") != recipient_agent_id:
                    raise A2AError(404, "Task not found or inaccessible", "TASK_NOT_FOUND")
                if self.command == "POST":
                    body = self._body()
                    if not isinstance(body, dict) or body:
                        raise A2AError(400, "Cancellation body must be empty")
                    try:
                        record = backend("POST", endpoint + "/cancel", authorization, {})
                    except A2AError as error:
                        if error.status != 409:
                            raise
                        if record.get("status") != "cancelled":
                            raise A2AError(400, "Task cannot be cancelled", "TASK_NOT_CANCELABLE")
                self._reply(200, _task(record), extension=True)
                return
            reason = ("PUSH_NOTIFICATION_NOT_SUPPORTED" if "pushNotification" in path
                      else "UNSUPPORTED_OPERATION")
            raise A2AError(400, "Operation is outside the advertised subset", reason)

        def _dispatch(self):
            try:
                self._handle()
            except A2AError as error:
                self._error(error)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self._error(A2AError(500, "Invalid backend response or internal facade error",
                                     "INVALID_AGENT_RESPONSE"))

        do_GET = _dispatch
        do_POST = _dispatch
        do_DELETE = _dispatch

    return ThreadingHTTPServer((host, port), Handler)
