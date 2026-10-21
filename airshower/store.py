"""Consent-aware, durable storage for the experimental Airshower reference.

This runtime deliberately does not execute commands or launch external agents.
Agents receive capabilities for exact requests, never ambient owner authority.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid

from .errors import APIError, conflict, forbidden, invalid, missing

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_CONTEXT_BYTES = 64 * 1024
MAX_CONTEXT_SELECTION_BYTES = 1024 * 1024
MAX_TEXT_BYTES = 16 * 1024
KINDS = {"message", "ask_human", "context", "file", "task"}
ACTIONS = {"message", "context", "file", "task"}
TERMINAL = {"completed", "denied", "cancelled"}


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _id():
    return str(uuid.uuid4())


def _hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _text(value, field, maximum=MAX_TEXT_BYTES, empty=False):
    if not isinstance(value, str):
        raise invalid("%s must be a string." % field)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise invalid("%s must be valid Unicode." % field)
    if (not empty and not value.strip()) or size > maximum:
        raise invalid("%s is empty or exceeds its size limit." % field)
    return value


def _fields(data, allowed, required=()):
    if not isinstance(data, dict) or set(data) - set(allowed) or set(required) - set(data):
        raise invalid("Missing or unsupported request fields.")


def _ids(value, field="source_ids"):
    if not isinstance(value, list) or len(value) > 100:
        raise invalid("%s must be a list of at most 100 identifiers." % field)
    for item in value:
        _text(item, field, 128)
    if len(set(value)) != len(value):
        raise invalid("%s must not contain duplicates." % field)
    return value


class Store:
    """One SQLite database. All authorization decisions are serialized with writes."""

    def __init__(self, db_path, admin_token):
        if not isinstance(admin_token, str) or len(admin_token) < 16:
            raise ValueError("Use an admin token of at least 16 characters.")
        self.admin_hash = _hash(admin_token)
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.closed = False
        # Source bodies and capabilities are sensitive. New databases start
        # owner-readable only; SQLite uses these permissions for its WAL files.
        if str(db_path) != ":memory:":
            try:
                descriptor = os.open(str(db_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS owners (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS agents (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(id),
          name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS owner_consent_credentials (
          owner_id TEXT PRIMARY KEY NOT NULL REFERENCES owners(id),
          salt BLOB NOT NULL, password_hash BLOB NOT NULL,
          failed_attempts INTEGER NOT NULL DEFAULT 0,
          blocked_until REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS contacts (
          receiver_owner_id TEXT NOT NULL REFERENCES owners(id),
          sender_owner_id TEXT NOT NULL REFERENCES owners(id),
          PRIMARY KEY(receiver_owner_id, sender_owner_id)
        );
        CREATE TABLE IF NOT EXISTS enrollments (
          token_hash TEXT PRIMARY KEY, expires_at REAL NOT NULL, consumed_at REAL
        );
        CREATE TABLE IF NOT EXISTS contact_invites (
          token_hash TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(id),
          expires_at REAL NOT NULL, consumed_at REAL
        );
        CREATE TABLE IF NOT EXISTS sources (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(id),
          label TEXT NOT NULL, kind TEXT NOT NULL, content TEXT, data BLOB,
          filename TEXT, media_type TEXT, sha256 TEXT, size INTEGER,
          active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS grants (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(id),
          sender_agent_id TEXT NOT NULL REFERENCES agents(id),
          recipient_agent_id TEXT NOT NULL REFERENCES agents(id),
          actions TEXT NOT NULL, source_ids TEXT NOT NULL, expires_at REAL,
          revoked INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS requests (
          id TEXT PRIMARY KEY, sender_agent_id TEXT NOT NULL REFERENCES agents(id),
          recipient_agent_id TEXT NOT NULL REFERENCES agents(id),
          kind TEXT NOT NULL, message TEXT NOT NULL, source_ids TEXT NOT NULL,
          status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
          result TEXT, attribution TEXT, reason TEXT, approval_basis TEXT,
          idempotency_key TEXT NOT NULL, payload_hash TEXT NOT NULL,
          UNIQUE(sender_agent_id, idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL,
          request_id TEXT NOT NULL REFERENCES requests(id), status TEXT NOT NULL,
          created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS requests_sender ON requests(sender_agent_id);
        CREATE INDEX IF NOT EXISTS requests_recipient ON requests(recipient_agent_id);
        CREATE INDEX IF NOT EXISTS events_request ON events(request_id);
        """)
        self.db.commit()

    def close(self):
        with self.condition:
            if not self.closed:
                self.closed = True
                self.condition.notify_all()
                self.db.close()

    def authenticate(self, token):
        if not isinstance(token, str) or not token or len(token) > 1024:
            raise APIError(401, "unauthorized", "A valid bearer token is required.")
        digest = _hash(token)
        with self.lock:
            if self.closed:
                raise APIError(503, "unavailable", "The store is closed.")
            if hmac.compare_digest(digest, self.admin_hash):
                return {"id": "admin", "role": "admin"}
            row = self.db.execute("SELECT id,name FROM owners WHERE token_hash=?", (digest,)).fetchone()
            if row:
                return dict(row, role="owner", owner_id=row["id"])
            row = self.db.execute("SELECT id,owner_id,name FROM agents WHERE token_hash=?", (digest,)).fetchone()
            if row:
                return dict(row, role="agent")
        raise APIError(401, "unauthorized", "A valid bearer token is required.")

    @staticmethod
    def _role(actor, role):
        if actor["role"] != role:
            raise forbidden()

    def _agent(self, identifier):
        return self.db.execute("SELECT id,owner_id,name FROM agents WHERE id=?", (identifier,)).fetchone()

    def _contact(self, receiver, sender):
        return receiver == sender or self.db.execute(
            "SELECT 1 FROM contacts WHERE receiver_owner_id=? AND sender_owner_id=?", (receiver, sender)
        ).fetchone() is not None

    def _request(self, identifier, actor):
        row = self.db.execute("SELECT * FROM requests WHERE id=?", (identifier,)).fetchone()
        if row is None or not self._visible(row, actor):
            raise missing()
        return row

    def _visible(self, row, actor):
        if actor["role"] == "agent":
            return actor["id"] in (row["sender_agent_id"], row["recipient_agent_id"])
        if actor["role"] == "owner":
            return any(self._agent(row[key])["owner_id"] == actor["id"]
                       for key in ("sender_agent_id", "recipient_agent_id"))
        return False

    @staticmethod
    def _public_request(row):
        result = {key: row[key] for key in (
            "id", "sender_agent_id", "recipient_agent_id", "kind", "message", "status", "created_at", "updated_at"
        )}
        result["source_ids"] = json.loads(row["source_ids"])
        for key in ("result", "attribution"):
            if row[key] is not None:
                result[key] = json.loads(row[key])
        if row["reason"] is not None:
            result["reason"] = row["reason"]
        return result

    @staticmethod
    def _source_metadata(row):
        result = {key: row[key] for key in ("id", "label", "kind")}
        if row["kind"] == "file":
            result.update({key: row[key] for key in ("filename", "media_type", "sha256", "size")})
        return result

    @staticmethod
    def _public_grant(row):
        result = {key: row[key] for key in ("id", "sender_agent_id", "recipient_agent_id", "expires_at")}
        result["actions"] = json.loads(row["actions"])
        result["source_ids"] = json.loads(row["source_ids"])
        result["revoked"] = bool(row["revoked"])
        return result

    def _sources(self, owner_id, ids, expected_kind=None):
        sources = []
        for identifier in ids:
            row = self.db.execute("""SELECT id,owner_id,label,kind,content,filename,media_type,sha256,size,active
                FROM sources WHERE id=? AND owner_id=? AND active=1""", (identifier, owner_id)).fetchone()
            if row is None or (expected_kind is not None and row["kind"] != expected_kind):
                # Do not reveal source existence, ownership, label, filename or type.
                raise forbidden("The requested sources are not available for this request.")
            sources.append(row)
        return sources

    def _request_sources(self, owner_id, ids, kind):
        sources = self._sources(owner_id, ids, kind if kind in ("context", "file") else None)
        if kind == "context" and sum(len(row["content"].encode("utf-8")) for row in sources) > MAX_CONTEXT_SELECTION_BYTES:
            # One MiB bounds even worst-case JSON escaping below the client's
            # response cap. Grants may cover more sources; each request selects
            # its own bounded subset. Check again for persisted older requests.
            raise invalid("A context request may select at most 1 MiB of source text in total (UTF-8 bytes).")
        return sources

    def _active_grant(self, row, request):
        return (row is not None and not row["revoked"]
                and (row["expires_at"] is None or row["expires_at"] > time.time())
                and row["sender_agent_id"] == request["sender_agent_id"]
                and row["recipient_agent_id"] == request["recipient_agent_id"]
                and request["kind"] in json.loads(row["actions"])
                and set(json.loads(request["source_ids"])).issubset(json.loads(row["source_ids"])))

    def _authority(self, request, allow_human=False):
        sender = self._agent(request["sender_agent_id"])
        recipient = self._agent(request["recipient_agent_id"])
        if not self._contact(recipient["owner_id"], sender["owner_id"]):
            raise forbidden("Contact permission has been revoked.")
        ids = json.loads(request["source_ids"])
        sources = self._request_sources(recipient["owner_id"], ids, request["kind"])
        if allow_human and request["kind"] == "ask_human":
            return sources
        basis = request["approval_basis"]
        if basis == "owner":
            return sources
        if basis and basis.startswith("grant:"):
            grant = self.db.execute("SELECT * FROM grants WHERE id=?", (basis[6:],)).fetchone()
            if self._active_grant(grant, request):
                return sources
        raise forbidden("The approval for this request is no longer active.")

    def _event(self, request, event_type):
        self.db.execute("INSERT INTO events(type,request_id,status,created_at) VALUES(?,?,?,?)",
                        (event_type, request["id"], request["status"], time.time()))

    def _update(self, request, status, event_type, **changes):
        changes.update(status=status, updated_at=time.time())
        columns = ",".join(key + "=?" for key in changes)
        self.db.execute("UPDATE requests SET " + columns + " WHERE id=?", tuple(changes.values()) + (request["id"],))
        updated = self.db.execute("SELECT * FROM requests WHERE id=?", (request["id"],)).fetchone()
        self._event(updated, event_type)
        return self._public_request(updated)

    def dispatch(self, method, path, actor, data=None):
        """Return (HTTP status, JSON value); files/events use dedicated methods."""
        with self.condition:
            if self.closed:
                raise APIError(503, "unavailable", "The store is closed.")
            try:
                result = self._dispatch(method, path, actor, data)
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            self.condition.notify_all()
            return result

    @staticmethod
    def _invite_expiry(data):
        _fields(data, {"expires_in"})
        lifetime = data.get("expires_in", 3600)
        if isinstance(lifetime, bool) or not isinstance(lifetime, int) or not 1 <= lifetime <= 86400:
            raise invalid("expires_in must be an integer from 1 to 86400 seconds.")
        return time.time() + lifetime

    @staticmethod
    def _invite_hash(token):
        # These credentials are generated here, never user-selected identifiers.
        # Reject malformed values before hashing and never include them in errors.
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            return None
        return _hash(token)

    def enroll(self, token, data):
        """Consume an enrollment credential and issue one owner plus one agent.

        Only hashes are retained. A lost success response cannot be replayed to
        recover these credentials; an operator must arrange a new enrollment.
        """
        _fields(data, {"name", "agent_name", "consent_secret"}, {"name", "agent_name"})
        name = _text(data["name"], "name", 160)
        agent_name = _text(data["agent_name"], "agent_name", 160)
        consent_secret = None
        if "consent_secret" in data:
            consent_secret = _text(data["consent_secret"], "consent_secret", 512)
            if not 12 <= len(consent_secret) <= 128:
                raise invalid("consent_secret must contain 12 to 128 characters and at most 512 UTF-8 bytes.")
            if not callable(getattr(hashlib, "scrypt", None)):
                raise APIError(503, "consent_unavailable", "This relay runtime does not support secure approval-password storage.")
        digest = self._invite_hash(token)
        with self.condition:
            if self.closed:
                raise APIError(503, "unavailable", "The store is closed.")
            try:
                # Wait for any other database writer before checking expiry.
                self.db.execute("BEGIN IMMEDIATE")
                now = time.time()
                # Consume in the same transaction as provisioning. The
                # conditional write additionally prevents accidental reuse.
                consumed = self.db.execute("""UPDATE enrollments SET consumed_at=?
                    WHERE token_hash=? AND consumed_at IS NULL AND expires_at>?""", (now, digest, now))
                if not consumed.rowcount:
                    raise APIError(401, "unauthorized", "A valid unused enrollment token is required.")
                owner_id, owner_token = _id(), secrets.token_urlsafe(32)
                agent_id, agent_token = _id(), secrets.token_urlsafe(32)
                self.db.execute("INSERT INTO owners(id,name,token_hash) VALUES(?,?,?)",
                                (owner_id, name, _hash(owner_token)))
                self.db.execute("INSERT INTO agents(id,owner_id,name,token_hash) VALUES(?,?,?,?)",
                                (agent_id, owner_id, agent_name, _hash(agent_token)))
                if consent_secret is not None:
                    # This browser-approval password is enrollment-only. Owner
                    # API tokens cannot create or replace it after enrollment.
                    salt = secrets.token_bytes(16)
                    password_hash = hashlib.scrypt(consent_secret.encode("utf-8"), salt=salt,
                                                   n=16384, r=8, p=1, dklen=32)
                    self.db.execute("""INSERT INTO owner_consent_credentials(owner_id,salt,password_hash)
                        VALUES(?,?,?)""", (owner_id, salt, password_hash))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            self.condition.notify_all()
            return 201, {
                "owner": {"id": owner_id, "name": name, "token": owner_token},
                "agent": {"id": agent_id, "owner_id": owner_id, "name": agent_name, "token": agent_token},
            }

    def _dispatch(self, method, path, actor, data):
        parts = path.strip("/").split("/")
        if path == "/v1/me" and method == "GET":
            return 200, actor
        if path == "/v1/enrollments" and method == "POST":
            self._role(actor, "admin")
            expires_at = self._invite_expiry(data)
            token = secrets.token_urlsafe(32)
            self.db.execute("INSERT INTO enrollments(token_hash,expires_at) VALUES(?,?)", (_hash(token), expires_at))
            return 201, {"token": token, "expires_at": expires_at}
        if path == "/v1/contact-invites" and method == "POST":
            self._role(actor, "owner")
            expires_at = self._invite_expiry(data)
            token = secrets.token_urlsafe(32)
            self.db.execute("INSERT INTO contact_invites(token_hash,owner_id,expires_at) VALUES(?,?,?)",
                            (_hash(token), actor["id"], expires_at))
            return 201, {"token": token, "expires_at": expires_at, "owner_id": actor["id"]}
        if path == "/v1/contact-invites/accept" and method == "POST":
            self._role(actor, "owner")
            _fields(data, {"token"}, {"token"})
            digest = self._invite_hash(data["token"])
            self.db.execute("BEGIN IMMEDIATE")
            now = time.time()
            invite = self.db.execute("""SELECT owner_id FROM contact_invites
                WHERE token_hash=? AND consumed_at IS NULL AND expires_at>?""", (digest, now)).fetchone()
            if invite is None:
                raise missing()
            if invite["owner_id"] == actor["id"]:
                raise invalid("An owner cannot accept their own contact invite.")
            consumed = self.db.execute("""UPDATE contact_invites SET consumed_at=?
                WHERE token_hash=? AND consumed_at IS NULL AND expires_at>?""", (now, digest, now))
            if not consumed.rowcount:
                raise missing()
            # Issuing the invite consents to the accepting owner; acceptance
            # consents in the opposite direction. Neither grants source access.
            for receiver, sender in ((actor["id"], invite["owner_id"]), (invite["owner_id"], actor["id"])):
                self.db.execute("INSERT OR IGNORE INTO contacts(receiver_owner_id,sender_owner_id) VALUES(?,?)",
                                (receiver, sender))
            return 200, {"owner_id": invite["owner_id"], "connected": True}
        if path == "/v1/owners" and method == "POST":
            self._role(actor, "admin")
            _fields(data, {"name"}, {"name"})
            name = _text(data["name"], "name", 160)
            identifier, token = _id(), secrets.token_urlsafe(32)
            self.db.execute("INSERT INTO owners(id,name,token_hash) VALUES(?,?,?)", (identifier, name, _hash(token)))
            return 201, {"id": identifier, "name": name, "token": token}
        if path == "/v1/agents":
            if method == "POST":
                self._role(actor, "owner")
                _fields(data, {"name"}, {"name"})
                name = _text(data["name"], "name", 160)
                identifier, token = _id(), secrets.token_urlsafe(32)
                self.db.execute("INSERT INTO agents(id,owner_id,name,token_hash) VALUES(?,?,?,?)",
                                (identifier, actor["id"], name, _hash(token)))
                return 201, {"id": identifier, "owner_id": actor["id"], "name": name, "token": token}
            if method == "GET":
                if actor["role"] not in ("owner", "agent"):
                    raise forbidden()
                if actor["role"] == "owner":
                    rows = self.db.execute("SELECT id,owner_id,name FROM agents WHERE owner_id=? ORDER BY rowid", (actor["id"],)).fetchall()
                else:
                    rows = self.db.execute("""SELECT id,owner_id,name FROM agents WHERE owner_id=? OR owner_id IN
                        (SELECT receiver_owner_id FROM contacts WHERE sender_owner_id=?) ORDER BY rowid""",
                        (actor["owner_id"], actor["owner_id"])).fetchall()
                return 200, {"agents": [dict(row) for row in rows]}
        if len(parts) >= 2 and parts[:2] == ["v1", "contacts"]:
            self._role(actor, "owner")
            if len(parts) == 2 and method == "GET":
                rows = self.db.execute("SELECT sender_owner_id AS owner_id FROM contacts WHERE receiver_owner_id=? ORDER BY rowid", (actor["id"],))
                return 200, {"contacts": [dict(row) for row in rows]}
            if len(parts) == 2 and method == "POST":
                _fields(data, {"owner_id"}, {"owner_id"})
                other = _text(data["owner_id"], "owner_id", 128)
                if not self.db.execute("SELECT 1 FROM owners WHERE id=?", (other,)).fetchone():
                    raise missing()
                self.db.execute("INSERT OR IGNORE INTO contacts(receiver_owner_id,sender_owner_id) VALUES(?,?)", (actor["id"], other))
                return 201, {"owner_id": other}
            if len(parts) == 3 and method == "DELETE":
                other = parts[2]
                self.db.execute("DELETE FROM contacts WHERE receiver_owner_id=? AND sender_owner_id=?", (actor["id"], other))
                # Do not resurrect old authorizations if the contact is re-added.
                self.db.execute("""UPDATE grants SET revoked=1 WHERE owner_id=? AND sender_agent_id IN
                    (SELECT id FROM agents WHERE owner_id=?)""", (actor["id"], other))
                rows = self.db.execute("""SELECT r.* FROM requests r JOIN agents s ON s.id=r.sender_agent_id
                    JOIN agents t ON t.id=r.recipient_agent_id WHERE t.owner_id=? AND s.owner_id=?
                    AND r.status NOT IN ('completed','denied','cancelled')""", (actor["id"], other)).fetchall()
                for row in rows:
                    self._update(row, "denied", "request.denied", reason="Contact permission was revoked.", approval_basis=None)
                # Completed files must not become downloadable again on re-pairing.
                self.db.execute("""UPDATE requests SET approval_basis=NULL WHERE recipient_agent_id IN
                    (SELECT id FROM agents WHERE owner_id=?) AND sender_agent_id IN
                    (SELECT id FROM agents WHERE owner_id=?)""", (actor["id"], other))
                return 200, {"owner_id": other, "revoked": True}
        if len(parts) >= 2 and parts[:2] == ["v1", "sources"]:
            self._role(actor, "owner")
            if len(parts) == 2 and method == "GET":
                rows = self.db.execute("""SELECT id,label,kind,filename,media_type,sha256,size
                    FROM sources WHERE owner_id=? AND active=1 ORDER BY rowid""", (actor["id"],)).fetchall()
                return 200, {"sources": [self._source_metadata(row) for row in rows]}
            if len(parts) == 2 and method == "POST":
                return self._create_source(actor, data)
            if len(parts) == 3 and method == "DELETE":
                cursor = self.db.execute("UPDATE sources SET active=0,content=NULL,data=NULL WHERE id=? AND owner_id=? AND active=1", (parts[2], actor["id"]))
                if not cursor.rowcount:
                    raise missing()
                return 200, {"id": parts[2], "revoked": True}
        if len(parts) >= 2 and parts[:2] == ["v1", "grants"]:
            self._role(actor, "owner")
            if len(parts) == 2 and method == "GET":
                rows = self.db.execute("SELECT * FROM grants WHERE owner_id=? ORDER BY rowid", (actor["id"],)).fetchall()
                return 200, {"grants": [self._public_grant(row) for row in rows]}
            if len(parts) == 2 and method == "POST":
                return self._create_grant(actor, data)
            if len(parts) == 3 and method == "DELETE":
                cursor = self.db.execute("UPDATE grants SET revoked=1 WHERE id=? AND owner_id=?", (parts[2], actor["id"]))
                if not cursor.rowcount:
                    raise missing()
                return 200, {"id": parts[2], "revoked": True}
        if path == "/v1/requests":
            if method == "POST":
                return self._create_request(actor, data)
            if method == "GET":
                if actor["role"] not in ("owner", "agent"):
                    raise forbidden()
                if actor["role"] == "agent":
                    rows = self.db.execute("SELECT * FROM requests WHERE sender_agent_id=? OR recipient_agent_id=? ORDER BY created_at,id", (actor["id"], actor["id"])).fetchall()
                else:
                    rows = self.db.execute("""SELECT r.* FROM requests r JOIN agents s ON s.id=r.sender_agent_id
                        JOIN agents t ON t.id=r.recipient_agent_id WHERE s.owner_id=? OR t.owner_id=? ORDER BY r.created_at,r.id""",
                        (actor["id"], actor["id"])).fetchall()
                return 200, {"requests": [self._public_request(row) for row in rows]}
        if len(parts) in (3, 4) and parts[:2] == ["v1", "requests"]:
            request = self._request(parts[2], actor)
            if len(parts) == 3 and method == "GET":
                return 200, self._public_request(request)
            if len(parts) == 4:
                action = parts[3]
                if action == "decision" and method == "POST":
                    return 200, self._decision(request, actor, data)
                if action == "claim" and method == "POST":
                    _fields(data, set())
                    if actor["role"] != "agent" or actor["id"] != request["recipient_agent_id"]:
                        raise forbidden()
                    if request["status"] != "ready":
                        raise conflict("Only a ready request can be claimed.")
                    self._authority(request)
                    return 200, self._update(request, "working", "request.working")
                if action == "sources" and method == "GET":
                    if actor["role"] != "agent" or actor["id"] != request["recipient_agent_id"]:
                        raise forbidden()
                    if request["status"] not in ("ready", "working"):
                        raise conflict("Sources are only accessible for ready or working requests.")
                    rows = self._authority(request)
                    sources = []
                    for row in rows:
                        source = self._source_metadata(row)
                        if row["kind"] == "context":
                            source["content"] = row["content"]
                        sources.append(source)
                    return 200, {"sources": sources}
                if action == "complete" and method == "POST":
                    return 200, self._complete(request, actor, data)
                if action == "cancel" and method == "POST":
                    _fields(data, set())
                    sender = self._agent(request["sender_agent_id"])
                    if not ((actor["role"] == "agent" and actor["id"] == sender["id"])
                            or (actor["role"] == "owner" and actor["id"] == sender["owner_id"])):
                        raise forbidden()
                    if request["status"] in TERMINAL:
                        raise conflict("A terminal request cannot be cancelled.")
                    return 200, self._update(request, "cancelled", "request.cancelled")
        raise missing()

    def _create_source(self, actor, data):
        _fields(data, {"label", "kind", "content", "filename", "media_type", "data_base64"}, {"label", "kind"})
        label = _text(data["label"], "label", 160)
        kind = data["kind"]
        if kind not in ("context", "file"):
            raise invalid("Source kind must be context or file.")
        values = {"id": _id(), "owner_id": actor["id"], "label": label, "kind": kind,
                  "content": None, "data": None, "filename": None, "media_type": None, "sha256": None, "size": None}
        if kind == "context":
            if any(key in data for key in ("filename", "media_type", "data_base64")):
                raise invalid("Context sources cannot include file fields.")
            values["content"] = _text(data.get("content"), "content", MAX_CONTEXT_BYTES, empty=True)
        else:
            if "content" in data:
                raise invalid("File sources cannot include context fields.")
            filename = _text(data.get("filename"), "filename", 255)
            # Filenames are display labels only. Reject paths and control
            # characters rather than accepting an ambiguous normalized value.
            if "/" in filename or "\\" in filename or filename in (".", "..") or any(ord(char) < 32 or ord(char) == 127 for char in filename):
                raise invalid("filename must be a filename without paths or control characters.")
            media_type = data.get("media_type", "application/octet-stream")
            if not isinstance(media_type, str) or len(media_type) > 128 or not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", media_type):
                raise invalid("Invalid media_type.")
            encoded = data.get("data_base64")
            if not isinstance(encoded, str) or len(encoded) > ((MAX_FILE_BYTES + 2) // 3) * 4:
                raise invalid("File content is missing or exceeds 5 MiB.")
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (ValueError, UnicodeEncodeError):
                raise invalid("File content must be valid base64.")
            if len(raw) > MAX_FILE_BYTES:
                raise invalid("File exceeds 5 MiB.")
            values.update(data=raw, filename=filename, media_type=media_type,
                          sha256=hashlib.sha256(raw).hexdigest(), size=len(raw))
        keys = list(values)
        self.db.execute("INSERT INTO sources(" + ",".join(keys) + ") VALUES(" + ",".join("?" for _ in keys) + ")", tuple(values.values()))
        return 201, self._source_metadata(values)

    def _create_grant(self, actor, data):
        _fields(data, {"sender_agent_id", "recipient_agent_id", "actions", "source_ids", "expires_at"}, {"sender_agent_id", "recipient_agent_id", "actions"})
        sender_id = _text(data["sender_agent_id"], "sender_agent_id", 128)
        recipient_id = _text(data["recipient_agent_id"], "recipient_agent_id", 128)
        sender, recipient = self._agent(sender_id), self._agent(recipient_id)
        if recipient is None or recipient["owner_id"] != actor["id"]:
            raise missing()
        if sender is None or not self._contact(actor["id"], sender["owner_id"]):
            raise forbidden("An approved incoming contact is required.")
        actions = _ids(data["actions"], "actions")
        if not actions or not set(actions).issubset(ACTIONS):
            raise invalid("Unsupported or empty grant actions.")
        source_ids = _ids(data.get("source_ids", []))
        sources = self._sources(actor["id"], source_ids)
        for action in ("context", "file"):
            if action in actions and not any(row["kind"] == action for row in sources):
                raise invalid("Context and file grants require matching sources.")
        if any(row["kind"] not in actions for row in sources):
            raise invalid("Each source must have a matching grant action.")
        expires = data.get("expires_at")
        if expires is not None and (isinstance(expires, bool) or not isinstance(expires, (int, float))
                                    or not (time.time() < expires <= 253402300799)):
            raise invalid("expires_at must be a future Unix timestamp.")
        identifier = _id()
        self.db.execute("""INSERT INTO grants(id,owner_id,sender_agent_id,recipient_agent_id,actions,source_ids,expires_at)
            VALUES(?,?,?,?,?,?,?)""", (identifier, actor["id"], sender_id, recipient_id, _dump(actions), _dump(source_ids), expires))
        row = self.db.execute("SELECT * FROM grants WHERE id=?", (identifier,)).fetchone()
        return 201, self._public_grant(row)

    def _create_request(self, actor, data):
        self._role(actor, "agent")
        _fields(data, {"recipient_agent_id", "kind", "message", "source_ids", "idempotency_key"}, {"recipient_agent_id", "kind", "message", "source_ids", "idempotency_key"})
        recipient_id = _text(data["recipient_agent_id"], "recipient_agent_id", 128)
        kind = data["kind"]
        if not isinstance(kind, str) or kind not in KINDS:
            raise invalid("Unsupported request kind.")
        message = _text(data["message"], "message")
        source_ids = _ids(data.get("source_ids", []))
        if kind in ("file", "context") and not source_ids:
            raise invalid("File and context requests require explicit source_ids.")
        if kind not in ("file", "context") and source_ids:
            raise invalid("Only file and context requests may specify source_ids.")
        key = _text(data["idempotency_key"], "idempotency_key", 128)
        canonical = {"recipient_agent_id": recipient_id, "kind": kind, "message": message, "source_ids": source_ids}
        payload_hash = _hash(_dump(canonical))
        existing = self.db.execute("SELECT * FROM requests WHERE sender_agent_id=? AND idempotency_key=?", (actor["id"], key)).fetchone()
        if existing:
            if existing["payload_hash"] != payload_hash:
                raise conflict("This idempotency key was used for a different request.")
            return 200, self._public_request(existing)
        recipient = self._agent(recipient_id)
        if recipient is None:
            raise missing()
        if not self._contact(recipient["owner_id"], actor["owner_id"]):
            raise forbidden("An approved incoming contact is required.")
        self._request_sources(recipient["owner_id"], source_ids, kind)
        identifier, now = _id(), time.time()
        request = dict(canonical, id=identifier, sender_agent_id=actor["id"], source_ids=_dump(source_ids))
        basis, status = None, "pending_approval"
        if kind == "ask_human":
            status = "waiting_human"
        else:
            grants = self.db.execute("SELECT * FROM grants WHERE sender_agent_id=? AND recipient_agent_id=? AND revoked=0 ORDER BY rowid", (actor["id"], recipient_id)).fetchall()
            for grant in grants:
                if self._active_grant(grant, request):
                    status, basis = "ready", "grant:" + grant["id"]
                    break
        self.db.execute("""INSERT INTO requests(id,sender_agent_id,recipient_agent_id,kind,message,source_ids,status,
            created_at,updated_at,approval_basis,idempotency_key,payload_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (identifier, actor["id"], recipient_id, kind, message, _dump(source_ids), status, now, now, basis, key, payload_hash))
        row = self.db.execute("SELECT * FROM requests WHERE id=?", (identifier,)).fetchone()
        self._event(row, "request.created")
        return 201, self._public_request(row)

    def _decision(self, request, actor, data):
        self._role(actor, "owner")
        if self._agent(request["recipient_agent_id"])["owner_id"] != actor["id"]:
            raise forbidden()
        _fields(data, {"decision"}, {"decision"})
        if data["decision"] not in ("approve", "deny"):
            raise invalid("decision must be approve or deny.")
        if request["status"] != "pending_approval":
            raise conflict("Only a pending request can receive a decision.")
        if data["decision"] == "deny":
            return self._update(request, "denied", "request.denied", reason="The recipient owner denied this request.")
        sender = self._agent(request["sender_agent_id"])
        if not self._contact(actor["id"], sender["owner_id"]):
            raise forbidden("An approved incoming contact is required.")
        self._request_sources(actor["id"], json.loads(request["source_ids"]), request["kind"])
        return self._update(request, "ready", "request.approved", approval_basis="owner")

    def _complete(self, request, actor, data):
        _fields(data, {"text", "source_ids"}, {"text", "source_ids"})
        text = _text(data["text"], "text", empty=True)
        ids = _ids(data.get("source_ids", []))
        if request["kind"] == "file" and not ids:
            raise invalid("A file completion requires at least one approved source.")
        if not set(ids).issubset(json.loads(request["source_ids"])):
            raise forbidden("Result sources must be within the approved request.")
        if request["kind"] == "ask_human":
            if actor["role"] != "owner" or self._agent(request["recipient_agent_id"])["owner_id"] != actor["id"]:
                raise forbidden("Only the recipient owner can provide a human answer.")
            if request["status"] != "waiting_human":
                raise conflict("This request is not waiting for a human answer.")
            self._authority(request, allow_human=True)
            attribution = {"type": "human", "id": actor["id"]}
        else:
            if actor["role"] != "agent" or actor["id"] != request["recipient_agent_id"]:
                raise forbidden()
            if request["status"] not in ("ready", "working"):
                raise conflict("Only a ready or working request can be completed.")
            self._authority(request)
            attribution = {"type": "agent", "id": actor["id"]}
        result = {"text": text, "source_ids": ids}
        if request["kind"] == "file":
            owner_id = self._agent(request["recipient_agent_id"])["owner_id"]
            result["files"] = [self._source_metadata(row) for row in self._sources(owner_id, ids, "file")]
        return self._update(request, "completed", "request.completed", result=_dump(result), attribution=_dump(attribution))

    def file(self, request_id, source_id, actor):
        """Return immutable downloaded bytes and metadata after current authorization."""
        with self.lock:
            request = self._request(request_id, actor)
            sender = self._agent(request["sender_agent_id"])
            if not ((actor["role"] == "agent" and actor["id"] == sender["id"])
                    or (actor["role"] == "owner" and actor["id"] == sender["owner_id"])):
                raise forbidden()
            if request["kind"] != "file" or request["status"] != "completed":
                raise missing()
            if source_id not in json.loads(request["result"])["source_ids"]:
                raise missing()
            rows = self._authority(request)
            for row in rows:
                if row["id"] == source_id:
                    raw = self.db.execute("SELECT data FROM sources WHERE id=? AND active=1", (source_id,)).fetchone()
                    return bytes(raw["data"]), self._source_metadata(row)
            raise missing()

    def events(self, actor, after=0, limit=100):
        """Bounded durable replay, filtered in SQL before applying the limit."""
        if isinstance(after, bool) or not isinstance(after, int) or after < 0 or after > 9223372036854775807:
            raise invalid("after must be a nonnegative event identifier.")
        if actor["role"] not in ("owner", "agent"):
            raise forbidden()
        with self.lock:
            if self.closed:
                return []
            limit = max(1, min(int(limit), 100))
            if actor["role"] == "agent":
                predicate = "(r.sender_agent_id=? OR r.recipient_agent_id=?)"
            else:
                predicate = "(s.owner_id=? OR t.owner_id=?)"
            rows = self.db.execute("""SELECT e.id,e.type,e.request_id,e.status,e.created_at FROM events e
                JOIN requests r ON r.id=e.request_id JOIN agents s ON s.id=r.sender_agent_id
                JOIN agents t ON t.id=r.recipient_agent_id WHERE e.id>? AND """ + predicate + " ORDER BY e.id LIMIT ?",
                (after, actor["id"], actor["id"], limit)).fetchall()
            return [dict(row) for row in rows]

    def wait_for_events(self, timeout=15):
        with self.condition:
            if not self.closed:
                self.condition.wait(timeout)
