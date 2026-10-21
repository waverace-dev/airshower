"""Private, dependency-free profile provisioning for existing agent hosts.

Setup does not install services, modify host configuration, start a model, or
claim that an existing conversation can receive events. Owner credentials never
appear in the generated MCP configuration. Files under the same operating-system
account are not a security boundary against an unrestricted local shell.
"""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import time
import uuid
from urllib.parse import urlsplit

from .client import AirshowerError, APIError, Client
from .store import Store


DEFAULT_LOCAL_RELAY = "http://127.0.0.1:7340"
MAX_PRIVATE_BYTES = 64 * 1024
PROFILE_FIELDS = {"schema_version", "mode", "relay", "owner_id", "owner_name", "agent_id", "agent_name"}


def _absolute(value):
    return Path(os.path.abspath(os.path.expanduser(str(value))))


def _no_symlinks(path):
    for part in reversed((path,) + tuple(path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("Airshower profile paths must not contain symbolic links")


def _private_directory(state_dir, create=False):
    path = _absolute(state_dir)
    _no_symlinks(path)
    if create:
        # mkdir's mode applies to the new leaf. Create missing parents separately
        # so none are accidentally created with a permissive default mode.
        missing = []
        current = path
        while not current.exists():
            missing.append(current)
            current = current.parent
        for current in reversed(missing):
            current.mkdir(mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise ValueError("Use an owned private state directory (mode 0700); existing permissions were left unchanged")
    return path


def _check_file(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
            or info.st_uid != os.getuid() or info.st_nlink != 1):
        raise ValueError("Profile files must be owned regular files with mode 0600 and no hard links")


def _read_private(path):
    _no_symlinks(path)
    # A FIFO can block in open() before fstat gets a chance to reject it.
    # Nonblocking mode has no effect on ordinary files and makes that rejection
    # immediate even when there is no writer attached to a named pipe.
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        _check_file(os.fstat(stream.fileno()))
        content = stream.read(MAX_PRIVATE_BYTES + 1)
    if len(content) > MAX_PRIVATE_BYTES:
        raise ValueError("Private profile file exceeds its size limit")
    return content


def _json_private(path):
    try:
        value = json.loads(_read_private(path))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("Private profile file is not valid JSON") from None
    if not isinstance(value, dict):
        raise ValueError("Private profile file must contain an object")
    return value


def _sync_directory(directory):
    descriptor = os.open(str(directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_new(path, content):
    """Publish a complete, fsynced file without overwriting any existing path."""
    temporary = path.parent / (".airshower-write-" + secrets.token_hex(12))
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # link publishes atomically and refuses an existing destination. The
        # temporary link is removed before the file is used as a credential.
        os.link(str(temporary), str(path))
        temporary.unlink()
        _sync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_json(path, value):
    _write_new(path, json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


@contextmanager
def _setup_lock(directory):
    path = directory / "setup.lock"
    descriptor = os.open(str(path), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK, 0o600)
    try:
        _check_file(os.fstat(descriptor))
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another Airshower setup is using this profile; retry after it finishes") from None
        yield
    finally:
        os.close(descriptor)


def _name(value, label):
    try:
        valid = isinstance(value, str) and bool(value.strip()) and len(value.encode("utf-8")) <= 160
    except UnicodeError:
        valid = False
    if not valid:
        raise ValueError(label + " must be nonempty and at most 160 UTF-8 bytes")
    return value


def _token(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or any(c.isspace() for c in value):
        raise ValueError("Private credential is missing or invalid")
    return value


def _credential(value, role):
    allowed = {"id", "name", "token"} | ({"owner_id"} if role == "agent" else set())
    if not isinstance(value, dict) or set(value) != allowed:
        raise ValueError("Private " + role + " credential has unexpected fields")
    try:
        uuid.UUID(value["id"])
        if role == "agent":
            uuid.UUID(value["owner_id"])
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Private credential has an invalid identity") from None
    _name(value["name"], role + " name")
    _token(value["token"])
    return value


def _relay(value, remote=False):
    if not isinstance(value, str) or not value:
        raise ValueError("Relay must be a nonempty HTTP(S) origin")
    Client(value, "configuration-validation-only")
    parsed = urlsplit(value)
    if parsed.path not in ("", "/"):
        raise ValueError("Relay must be an origin without a path")
    if remote and parsed.scheme != "https":
        raise ValueError("Remote enrollment requires an HTTPS relay")
    return value.rstrip("/")


def _read_identity(directory, role):
    return _credential(_json_private(directory / (role + ".json")), role)


def _validate_manifest(value):
    if (set(value) != PROFILE_FIELDS or isinstance(value["schema_version"], bool)
            or value["schema_version"] != 1 or value["mode"] not in ("local", "remote")):
        raise ValueError("Unsupported or invalid Airshower profile")
    _relay(value["relay"], remote=value["mode"] == "remote")
    _name(value["owner_name"], "Owner name")
    _name(value["agent_name"], "Agent name")
    for key in ("owner_id", "agent_id"):
        try:
            uuid.UUID(value[key])
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Profile has an invalid identity") from None
    return value


def load_profile(state_dir):
    """Return a token-free manifest and absolute paths; never return credentials."""
    directory = _private_directory(state_dir)
    result = dict(_validate_manifest(_json_private(directory / "profile.json")))
    result.update({
        "state_dir": str(directory),
        "owner_file": str(directory / "owner.json"),
        "agent_file": str(directory / "agent.json"),
        "mcp_config_file": str(directory / "mcp-config.json"),
        "claude_channel_config_file": str(directory / "claude-channel.json"),
        "codex_config_file": str(directory / "codex-config.json"),
    })
    if result["mode"] == "local":
        result["admin_token_file"] = str(directory / "admin.token")
        result["database"] = str(directory / "relay.sqlite3")
        # Validate metadata only: an agent loading its profile must not read an
        # owner/admin credential merely to discover its own configuration.
        for key in ("admin_token_file", "database"):
            path = Path(result[key])
            _no_symlinks(path)
            _check_file(path.lstat())
    return result


def _role_client(state_dir, role):
    profile = load_profile(state_dir)
    credential = _read_identity(Path(profile["state_dir"]), role)
    if (credential["id"] != profile[role + "_id"]
            or credential["name"] != profile[role + "_name"]
            or (role == "agent" and credential["owner_id"] != profile["owner_id"])):
        raise ValueError("Private credential does not match this profile")
    return Client(profile["relay"], credential["token"])


def agent_client(state_dir):
    """Construct an agent-scoped client without exposing its token in output."""
    return _role_client(state_dir, "agent")


def owner_client(state_dir):
    """For trusted local owner workflows only; never pass this client to MCP."""
    return _role_client(state_dir, "owner")


def mcp_config(state_dir, channel=False):
    """Return a portable stdio configuration containing no bearer credentials."""
    directory = _private_directory(state_dir)
    args = ["-m", "airshower", "mcp", "--profile", str(directory)]
    if channel:
        args.append("--channel")
    return {"mcpServers": {"airshower": {
        "command": os.path.abspath(sys.executable), "args": args,
        "env": {"PYTHONPATH": str(Path(__file__).resolve().parent.parent)},
    }}}


def _export_configs(directory):
    for filename, channel in (("mcp-config.json", False), ("claude-channel.json", True), ("codex-config.json", False)):
        path = directory / filename
        if path.exists() or path.is_symlink():
            # Existing exports may have been deliberately customized. Validate
            # their storage safety, but never replace their contents.
            _json_private(path)
        else:
            _write_json(path, mcp_config(directory, channel))


def _local_identities(directory, name, agent_name):
    token_path = directory / "admin.token"
    if token_path.exists() or token_path.is_symlink():
        admin = _token(_read_private(token_path).strip())
    else:
        admin = secrets.token_urlsafe(32)
        _write_new(token_path, admin + "\n")
    owner_path = directory / "owner.json"
    if owner_path.exists() or owner_path.is_symlink():
        owner = _read_identity(directory, "owner")
    else:
        owner = {"id": str(uuid.uuid4()), "name": name, "token": secrets.token_urlsafe(32)}
        _write_json(owner_path, owner)
    if owner["name"] != name:
        raise ValueError("Existing owner differs from the requested setup; files were left unchanged")
    agent_path = directory / "agent.json"
    if agent_path.exists() or agent_path.is_symlink():
        agent = _read_identity(directory, "agent")
    else:
        agent = {"id": str(uuid.uuid4()), "owner_id": owner["id"], "name": agent_name, "token": secrets.token_urlsafe(32)}
        _write_json(agent_path, agent)
    if owner["name"] != name or agent["name"] != agent_name or agent["owner_id"] != owner["id"]:
        raise ValueError("Existing identity differs from the requested setup; files were left unchanged")

    database = directory / "relay.sqlite3"
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(str(database) + suffix)
        if candidate.exists() or candidate.is_symlink():
            # Do not decode a SQLite database as text merely to check its mode.
            _no_symlinks(candidate)
            _check_file(candidate.lstat())
    store = Store(str(database), admin)
    try:
        # Persisted credentials precede the database transaction. This local
        # bootstrap can therefore recover a crash at any point without minting
        # duplicate identities or losing the only copy of a newly issued token.
        with store.lock, store.db:
            for role, identity in (("owner", owner), ("agent", agent)):
                row = store.db.execute("SELECT * FROM " + role + "s WHERE id=?", (identity["id"],)).fetchone()
                digest = hashlib.sha256(identity["token"].encode("utf-8")).hexdigest()
                if row is not None:
                    if row["token_hash"] != digest or row["name"] != identity["name"] or (role == "agent" and row["owner_id"] != owner["id"]):
                        raise ValueError("Existing database identity does not match the private profile")
                elif role == "owner":
                    store.db.execute("INSERT INTO owners(id,name,token_hash) VALUES(?,?,?)", (identity["id"], identity["name"], digest))
                else:
                    store.db.execute("INSERT INTO agents(id,owner_id,name,token_hash) VALUES(?,?,?,?)", (identity["id"], owner["id"], identity["name"], digest))
        if store.authenticate(owner["token"])["role"] != "owner" or store.authenticate(agent["token"])["role"] != "agent":
            raise ValueError("Provisioned credentials have incorrect roles")
    finally:
        store.close()
    return owner, agent


def _remote_identities(directory, name, agent_name, relay, enrollment_file):
    result_path = directory / "enrollment-result.json"
    pending_path = directory / "enrollment-pending.json"
    if result_path.exists() or result_path.is_symlink():
        pending = _json_private(pending_path)
        if pending.get("relay") != relay or pending.get("name") != name or pending.get("agent_name") != agent_name:
            raise ValueError("Saved enrollment belongs to different setup settings; use the original relay and identities")
        result = _json_private(result_path)
    elif pending_path.exists() or pending_path.is_symlink():
        _json_private(pending_path)
        raise ValueError("A previous enrollment has an unknown outcome. Do not retry its single-use invitation; ask the relay operator to recover or revoke that enrollment, then use a new profile")
    else:
        if enrollment_file is None:
            raise ValueError("Remote setup requires a private enrollment file")
        invite = _json_private(_absolute(enrollment_file))
        if set(invite) - {"relay", "token", "contact_invite", "expires_at"} or not {"relay", "token"}.issubset(invite):
            raise ValueError("Enrollment file must contain relay and token")
        invitation_relay = _relay(invite["relay"], remote=True)
        if relay != invitation_relay:
            raise ValueError("Invitation relay does not match the requested HTTPS origin")
        token = _token(invite["token"])
        _write_json(pending_path, {"relay": relay, "name": name, "agent_name": agent_name, "started_at": time.time()})
        try:
            result = Client(relay, token).post("/v1/enroll", {"name": name, "agent_name": agent_name})
        except APIError as exc:
            # A definite HTTP rejection did not issue credentials. Removing only
            # this setup's marker permits a corrected invitation on the next run.
            if 400 <= exc.status < 500:
                pending_path.unlink()
                _sync_directory(directory)
            raise
        # A transport error leaves the marker in place: the relay might already
        # have committed the single-use enrollment before the connection failed.
        if not isinstance(result, dict) or set(result) != {"owner", "agent"}:
            raise ValueError("Enrollment response has an invalid shape; contact the relay operator")
        _credential(result["owner"], "owner")
        _credential(result["agent"], "agent")
        _write_json(result_path, result)
    if not isinstance(result, dict) or set(result) != {"owner", "agent"}:
        raise ValueError("Saved enrollment result has an invalid shape")
    owner, agent = _credential(result["owner"], "owner"), _credential(result["agent"], "agent")
    if owner["name"] != name or agent["name"] != agent_name or agent["owner_id"] != owner["id"]:
        raise ValueError("Enrollment identity does not match the requested setup")
    for role, identity in (("owner", owner), ("agent", agent)):
        path = directory / (role + ".json")
        if path.exists() or path.is_symlink():
            if _read_identity(directory, role) != identity:
                raise ValueError("Existing private identity differs from the enrollment result")
        else:
            _write_json(path, identity)
    return owner, agent


def ready(state_dir, name, agent_name, relay=None, enrollment_file=None):
    """Provision one profile idempotently and return only public setup metadata.

    No relay, host, persistent service, or model is started. Local provisioning
    uses the standard-library Store in process. Remote enrollment requires a
    separately operated HTTPS relay and a private one-use invitation.
    """
    _name(name, "Owner name")
    _name(agent_name, "Agent name")
    if relay is None and enrollment_file is not None:
        invitation = _json_private(_absolute(enrollment_file))
        relay = invitation.get("relay")
        if not relay:
            raise ValueError("Enrollment file is missing its relay origin")
    mode = "remote" if relay is not None else "local"
    target_relay = _relay(relay if relay is not None else DEFAULT_LOCAL_RELAY, remote=mode == "remote")
    directory = _private_directory(state_dir, create=True)
    with _setup_lock(directory):
        intent = {"mode": mode, "relay": target_relay, "owner_name": name, "agent_name": agent_name}
        intent_path = directory / "setup-intent.json"
        if intent_path.exists() or intent_path.is_symlink():
            if _json_private(intent_path) != intent:
                raise ValueError("This profile was provisioned with different setup settings; use those settings or a new state directory")
        else:
            _write_json(intent_path, intent)
        profile_path = directory / "profile.json"
        if profile_path.exists() or profile_path.is_symlink():
            profile = load_profile(directory)
            if (profile["mode"] != mode or profile["relay"] != target_relay
                    or profile["owner_name"] != name or profile["agent_name"] != agent_name):
                raise ValueError("Existing profile differs from the requested setup; use its existing settings or a new state directory")
            agent_client(directory)
            owner_client(directory)
            created = False
        else:
            if mode == "local":
                owner, agent = _local_identities(directory, name, agent_name)
            else:
                owner, agent = _remote_identities(directory, name, agent_name, target_relay, enrollment_file)
            _write_json(profile_path, {"schema_version": 1, "mode": mode, "relay": target_relay,
                                      "owner_id": owner["id"], "owner_name": owner["name"],
                                      "agent_id": agent["id"], "agent_name": agent["name"]})
            created = True
        _export_configs(directory)
    return {"created": created, "profile": load_profile(directory), "status": "configured",
            "relay_started": False, "host_configured": False, "host_delivery_verified": False,
            "peer_roundtrip_verified": False,
            "next": "Start the relay and register the generated MCP configuration in a supported host. Contact pairing and a live roundtrip remain separate steps."}


def doctor(state_dir):
    """Return conservative, secret-free readiness stages, with a live role check."""
    stages = {"profile": "missing", "credentials": "not_checked", "relay": "not_checked",
              "agent_identity": "not_checked", "mcp_config": "not_checked",
              "event_listener": "not_verified", "host_delivery": "not_verified", "peer_roundtrip": "not_verified"}
    result = {"status": "incomplete", "stages": stages, "fully_ready": False}
    try:
        profile = load_profile(state_dir)
        result["profile"] = profile
        stages["profile"] = "configured"
        client = agent_client(state_dir)
        # Read owner metadata for local consistency, without transmitting its
        # credential or including either credential in the diagnostic result.
        owner_client(state_dir)
        stages["credentials"] = "private_and_consistent"
        config = _json_private(Path(profile["mcp_config_file"]))
        stages["mcp_config"] = "generated" if config == mcp_config(state_dir) else "customized"
    except (OSError, ValueError):
        result["detail"] = "Profile is missing, incomplete, or has unsafe permissions; run ready with the intended settings"
        return result
    try:
        actor = client.get("/v1/me")
        stages["relay"] = "reachable"
        if not isinstance(actor, dict) or actor.get("role") != "agent" or actor.get("id") != profile["agent_id"] or actor.get("owner_id") != profile["owner_id"]:
            stages["agent_identity"] = "rejected"
            result["detail"] = "Relay identity does not match this dedicated agent profile"
            return result
        stages["agent_identity"] = "verified"
        result["status"] = "relay_connected"
        result["detail"] = "Agent authentication works. Host registration, event delivery, and a peer roundtrip have not been verified by this check"
    except AirshowerError:
        stages["relay"] = "unavailable_or_rejected"
        result["detail"] = "Relay connection or authentication failed; no live host readiness has been inferred"
    return result
