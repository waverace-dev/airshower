"""Command-line tools for the experimental Airshower reference implementation."""

import argparse
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

from .client import AirshowerError, Client, TransportError


def _secret_file(filename):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(filename, flags)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Token file must be a regular file accessible only to its owner (mode 0600)")
        token = stream.read(4097).strip()
        if not token or len(token) > 4096 or any(character.isspace() for character in token):
            raise ValueError("Token file must contain one nonempty token")
        return token


def _environment_token(name):
    value = os.environ.get(name, "").strip()
    if not value or any(character.isspace() for character in value):
        raise ValueError("Set {} to a bearer token; do not put secrets in command arguments".format(name))
    return value


def _client():
    return Client(os.environ.get("AIRSHOWER_URL", "http://127.0.0.1:7340"), _environment_token("AIRSHOWER_TOKEN"))


def _private_database(filename):
    path = Path(filename).expanduser().absolute()
    if str(filename) == ":memory:":
        return filename
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.parent.stat().st_mode & 0o077:
        raise ValueError("Use a dedicated private database directory (mode 0700); existing directory permissions were left unchanged")
    if path.exists() and path.stat().st_mode & 0o077:
        raise ValueError("Existing database must be accessible only to its owner (mode 0600)")
    # This process creates SQLite database, journal, and WAL files with private modes.
    os.umask(0o077)
    return str(path)


def _serve(args):
    from .server import create_server
    from .store import Store

    token = _secret_file(args.admin_token_file) if args.admin_token_file else _environment_token("AIRSHOWER_ADMIN_TOKEN")
    store = Store(_private_database(args.db), token)
    server = None
    try:
        server = create_server(store, host=args.host, port=args.port)
        print("Airshower experimental reference server listening on {}:{}".format(*server.server_address[:2]), flush=True)
        print("Internet deployment requires an HTTPS reverse proxy. This server does not provide TLS or end-to-end encryption.", file=sys.stderr)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    finally:
        if server is not None:
            server.server_close()
        store.close()
    return 0


def _demo(args):
    from .demo import run_demo

    result = run_demo()
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("Airshower demo passed — local HTTP, fictional people, simulated agents.")
        for description in (
            "Both owners explicitly accepted contact.",
            "File request waited for owner approval, then was claimed and completed.",
            "Transferred file bytes and SHA-256 matched the approved fixture.",
            "A human question received an owner-attributed answer.",
            "An exact context grant allowed an agent answer.",
            "Revoking the grant blocked work that was already queued.",
            "Reconnecting over SSE replayed an unseen event.",
        ):
            print("  PASS " + description)
        print("File SHA-256: " + result["received_file"]["sha256"])
        print("No provider calls, real chats, or personal files were used. Temporary demo data was removed.")
    return 0


def _redact(value):
    if isinstance(value, dict):
        return {key: ("<redacted>" if "token" in key.lower() or "secret" in key.lower() else _redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _load_json(filename):
    if filename is None:
        return None
    if filename == "-":
        raw = sys.stdin.read(8 * 1024 * 1024 + 1)
    else:
        with open(filename, "r", encoding="utf-8") as stream:
            raw = stream.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("JSON input exceeds 8 MiB")
    return json.loads(raw)


def _api(args):
    client = _client()
    payload = _load_json(args.json_file)
    if args.method == "POST" and args.path.split("?", 1)[0] in ("/v1/owners", "/v1/agents") and not args.output:
        raise ValueError("Creating an owner or agent returns a secret; provide --output NEW_FILE to save it privately")
    if args.output:
        # Reserve the new private destination before a potentially mutating API
        # call, so an existing file cannot cause a newly issued token to be lost.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(args.output, flags, 0o600)
        response_received = False
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                result = client.request(args.method, args.path, payload)
                response_received = True
                json.dump(result, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if not response_received:
                # Only this command's newly created destination is removed.
                Path(args.output).unlink(missing_ok=True)
        print("Response saved to a new private file: " + args.output)
    else:
        result = client.request(args.method, args.path, payload)
        print(json.dumps(_redact(result), indent=2, sort_keys=True))
    return 0


def _save_cursor(filename, cursor):
    target = Path(filename).expanduser()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="ascii", dir=str(target.parent), prefix=".airshower-cursor-", delete=False) as stream:
            temporary = stream.name
            stream.write(str(cursor) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _watch(args):
    client = _client()
    cursor = args.after
    if cursor < 0 or args.max_events < 0:
        raise ValueError("Cursor and maximum event count must be nonnegative")
    if args.cursor_file and Path(args.cursor_file).expanduser().exists():
        with Path(args.cursor_file).expanduser().open("r", encoding="ascii") as stream:
            stored = int(stream.read(128).strip())
        if stored < 0:
            raise ValueError("Saved event cursor must be nonnegative")
        cursor = max(cursor, stored)
    count, delay = 0, 1
    while True:
        stream = client.events(after=cursor)
        try:
            for event in stream:
                if event["id"] <= cursor:
                    continue
                print(json.dumps(event, sort_keys=True), flush=True)
                cursor = event["id"]
                if args.cursor_file:
                    _save_cursor(args.cursor_file, cursor)
                count += 1
                delay = 1
                if args.max_events and count >= args.max_events:
                    return 0
        except TransportError:
            pass
        finally:
            stream.close()
        print("Event stream disconnected; reconnecting from cursor {}.".format(cursor), file=sys.stderr)
        time.sleep(delay)
        delay = min(delay * 2, 15)


def _mcp(args):
    from .mcp import run_stdio

    return run_stdio() or 0


def _a2a(args):
    from .a2a import create_a2a_server

    server = create_a2a_server(
        args.backend, args.recipient_agent_id, args.public_url,
        host=args.host, port=args.port,
    )
    try:
        print("Airshower experimental A2A facade listening on {}:{}".format(*server.server_address[:2]), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    finally:
        server.server_close()
    return 0


def parser():
    result = argparse.ArgumentParser(prog="python3 -m airshower", description="Airshower experimental reference implementation")
    commands = result.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="Run the local HTTP reference server")
    serve.add_argument("--db", default=".airshower/airshower.sqlite3", help="Database inside a dedicated private directory")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=7340)
    serve.add_argument("--admin-token-file", help="Private 0600 token file; otherwise reads AIRSHOWER_ADMIN_TOKEN")
    serve.set_defaults(run=_serve)

    demo = commands.add_parser("demo", help="Run a temporary, fictional local end-to-end demonstration")
    demo.add_argument("--json", action="store_true", help="Print the checks as JSON")
    demo.set_defaults(run=_demo)

    api = commands.add_parser("api", help="Call the API using AIRSHOWER_URL and AIRSHOWER_TOKEN")
    api.add_argument("--method", choices=("GET", "POST", "DELETE"), default="GET")
    api.add_argument("--path", required=True, help="API path beginning with /; never include credentials")
    api.add_argument("--json-file", help="UTF-8 JSON request file, or - for standard input")
    api.add_argument("--output", help="Write the full response to a NEW private 0600 file (required when issuing tokens)")
    api.set_defaults(run=_api)

    watch = commands.add_parser("watch", help="Receive immediate SSE events and replay from a saved cursor")
    watch.add_argument("--after", type=int, default=0)
    watch.add_argument("--cursor-file", help="Persist the last printed event ID for reconnects")
    watch.add_argument("--max-events", type=int, default=0, help="Exit after this many events; 0 keeps watching")
    watch.set_defaults(run=_watch)

    mcp = commands.add_parser("mcp", help="Run the MCP stdio adapter using AIRSHOWER_URL and AIRSHOWER_TOKEN")
    mcp.set_defaults(run=_mcp)

    a2a = commands.add_parser("a2a", help="Run the experimental A2A compatibility facade")
    a2a.add_argument("--backend", required=True, help="Airshower backend base URL")
    a2a.add_argument("--recipient-agent-id", required=True)
    a2a.add_argument("--public-url", required=True, help="Externally advertised facade base URL")
    a2a.add_argument("--host", default="127.0.0.1")
    a2a.add_argument("--port", type=int, default=7341)
    a2a.set_defaults(run=_a2a)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return args.run(args)
    except KeyboardInterrupt:
        return 130
    except (AirshowerError, ValueError) as exc:
        print("Airshower: " + str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print("Airshower: local I/O failed ({}); check file permissions and the listen address".format(exc.__class__.__name__), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
