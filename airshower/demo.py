"""An end-to-end local demo using fictional identities and in-memory fixtures."""

import base64
import hashlib
import secrets
import tempfile
import threading
from contextlib import closing
from pathlib import Path

from .client import APIError, Client


class DemoError(RuntimeError):
    pass


def _check(condition, message):
    if not condition:
        raise DemoError(message)


def run_demo():
    """Exercise real HTTP/storage code, without invoking any model or provider."""
    from .server import create_server
    from .store import Store

    with tempfile.TemporaryDirectory(prefix="airshower-demo-") as directory:
        admin_token = secrets.token_urlsafe(32)
        store = Store(str(Path(directory) / "demo.sqlite3"), admin_token)
        server = create_server(store, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:{}".format(server.server_address[1])
            admin = Client(url, admin_token)
            avery = admin.post("/v1/owners", {"name": "Avery"})
            morgan = admin.post("/v1/owners", {"name": "Morgan"})
            avery_owner = Client(url, avery["token"])
            morgan_owner = Client(url, morgan["token"])
            codex = avery_owner.post("/v1/agents", {"name": "Codex (simulated)"})
            antigravity = avery_owner.post("/v1/agents", {"name": "Antigravity (simulated)"})
            claude = morgan_owner.post("/v1/agents", {"name": "Claude (simulated)"})
            codex_client = Client(url, codex["token"])
            antigravity_client = Client(url, antigravity["token"])
            claude_client = Client(url, claude["token"])
            avery_owner.post("/v1/contacts", {"owner_id": morgan["id"]})
            morgan_owner.post("/v1/contacts", {"owner_id": avery["id"]})

            fixture = b"Airshower demo asset: a fictional project handoff.\n"
            source = morgan_owner.post("/v1/sources", {
                "label": "Approved demo asset", "kind": "file",
                "filename": "demo-asset.txt", "media_type": "text/plain",
                "data_base64": base64.b64encode(fixture).decode("ascii"),
            })
            context = morgan_owner.post("/v1/sources", {
                "label": "Demo project context", "kind": "context",
                "content": "The fictional project uses a blue palette. The review is on Friday.",
            })

            def send(client, kind, key, message, sources=None):
                return client.post("/v1/requests", {
                    "recipient_agent_id": claude["id"], "kind": kind,
                    "message": message, "source_ids": sources or [],
                    "idempotency_key": key,
                })

            file_request = send(codex_client, "file", "demo-file", "Please send the approved demo asset.", [source["id"]])
            _check(file_request["status"] == "pending_approval", "File request must await its owner's approval")
            request_path = "/v1/requests/" + file_request["id"]
            approved = morgan_owner.post(request_path + "/decision", {"decision": "approve"})
            _check(approved["status"] == "ready", "Owner approval should make this exact request ready")
            claimed = claude_client.post(request_path + "/claim", {})
            _check(claimed["status"] == "working", "Recipient should be able to claim the approved request")
            accessible = claude_client.get(request_path + "/sources")["sources"]
            _check(len(accessible) == 1 and accessible[0]["id"] == source["id"], "Only the approved source may be returned")
            completed = claude_client.post(request_path + "/complete", {
                "text": "Here is the approved demo asset.", "source_ids": [source["id"]],
            })
            received = codex_client.download(file_request["id"], source["id"])
            digest = hashlib.sha256(received).hexdigest()
            _check(received == fixture and digest == source["sha256"], "Transferred bytes must match the uploaded file")
            _check(completed["attribution"]["type"] == "agent", "Agent reply must be attributed to an agent")

            human_request = send(antigravity_client, "ask_human", "demo-human", "Morgan, is the demo ready for review?")
            _check(human_request["status"] == "waiting_human", "A human question must wait for the person")
            human_answer = morgan_owner.post("/v1/requests/" + human_request["id"] + "/complete", {
                "text": "Yes, the fictional demo is ready for review.", "source_ids": [],
            })
            _check(human_answer["attribution"] == {"type": "human", "id": morgan["id"]}, "Only the owner can supply a human answer")

            grant = morgan_owner.post("/v1/grants", {
                "sender_agent_id": codex["id"], "recipient_agent_id": claude["id"],
                "actions": ["context"], "source_ids": [context["id"]],
            })
            context_request = send(codex_client, "context", "demo-context", "What is the demo project palette?", [context["id"]])
            _check(context_request["status"] == "ready", "An exact active grant should allow automatic handling")
            context_path = "/v1/requests/" + context_request["id"]
            claude_client.post(context_path + "/claim", {})
            context_sources = claude_client.get(context_path + "/sources")["sources"]
            _check(context_sources[0]["content"].startswith("The fictional project"), "Approved context should be available")
            claude_client.post(context_path + "/complete", {
                "text": "The shared demo context specifies a blue palette.", "source_ids": [context["id"]],
            })

            revoked_request = send(codex_client, "context", "demo-revoked", "Check the demo context once more.", [context["id"]])
            _check(revoked_request["status"] == "ready", "Request should initially have grant authority")
            morgan_owner.delete("/v1/grants/" + grant["id"])
            blocked_status = None
            try:
                claude_client.post("/v1/requests/" + revoked_request["id"] + "/claim", {})
            except APIError as exc:
                blocked_status = exc.status
            _check(blocked_status in (403, 409), "Revoking the grant must block previously queued work")

            seen_events = claude_client.get("/v1/events/history?after=0")["events"]
            cursor = max((event["id"] for event in seen_events), default=0)
            queued = send(codex_client, "message", "demo-queued", "This message was sent while the recipient was not watching.")
            with closing(claude_client.events(after=cursor, timeout=5.0)) as stream:
                replayed = next(stream)
            _check(replayed["id"] > cursor and replayed["request_id"] == queued["id"], "Reconnect should replay the queued event")

            return {
                "ok": True,
                "mode": "local HTTP demo with simulated agents; no models invoked",
                "checks": {
                    "explicit_contact_pairing": True,
                    "file_waits_for_owner_approval": True,
                    "approved_request_claimed_and_completed": True,
                    "file_bytes_verified": True,
                    "human_answer_attributed_to_owner": True,
                    "scoped_context_grant": True,
                    "revocation_blocks_queued_work": True,
                    "queued_event_replayed_over_sse": True,
                },
                "received_file": {"name": "demo-asset.txt", "bytes": len(received), "sha256": digest},
                "identities": ["Avery", "Morgan"],
                "agent_labels": ["Codex (simulated)", "Claude (simulated)", "Antigravity (simulated)"],
            }
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            store.close()
