"""Independent HTTP security and interoperability checks, requiring only stdlib.

Tests deliberately use public HTTP endpoints, including a real SSE connection.
No Store internals or database contents are changed to arrange authorization.
"""

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

from airshower.client import APIError as ClientAPIError, Client
from airshower.server import create_server
from airshower.store import Store


class RuntimeHTTPTests(unittest.TestCase):
    ADMIN = "test-bootstrap-secret-at-least-16-characters"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "airshower.sqlite3")
        self.start_server()
        self.avery = self.owner("Avery")
        self.morgan = self.owner("Morgan")
        self.other = self.owner("Unrelated person")
        self.a = self.agent(self.avery, "Codex")
        self.a2 = self.agent(self.avery, "Claude")
        self.s = self.agent(self.morgan, "Claude")
        self.s2 = self.agent(self.morgan, "Codex")
        self.o = self.agent(self.other, "Unrelated agent")
        self.contact(self.morgan, self.avery)

    def start_server(self):
        self.store = Store(self.db_path, self.ADMIN)
        self.server = create_server(self.store, host="127.0.0.1", port=0)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True
        )
        self.thread.start()

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.store.close()

    def tearDown(self):
        self.stop_server()
        self.temp.cleanup()

    def call(self, method, path, actor=None, data=None, expect=None, headers=None,
             raw=False):
        actual_headers = dict(headers or {})
        if actor is not None:
            token = actor["token"] if isinstance(actor, dict) else actor
            actual_headers["Authorization"] = "Bearer " + token
        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            actual_headers["Content-Type"] = "application/json"
        request = Request(self.base + path, data=body, headers=actual_headers,
                          method=method)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            status = response.status
            response_headers = response.headers
            value = response.read()
        if not raw:
            value = json.loads(value)
        if expect is None:
            self.assertIn(status, (200, 201, 202, 204), (method, path, value))
        else:
            permitted = (expect,) if isinstance(expect, int) else expect
            self.assertIn(status, permitted, (method, path, status, value))
        return value, response_headers

    def api(self, method, path, actor=None, data=None, expect=None):
        return self.call(method, path, actor, data, expect)[0]

    def owner(self, name):
        return self.api("POST", "/v1/owners", self.ADMIN, {"name": name})

    def agent(self, owner, name):
        return self.api("POST", "/v1/agents", owner, {"name": name})

    def contact(self, receiver, sender):
        return self.api("POST", "/v1/contacts", receiver,
                        {"owner_id": sender["id"]})

    def context(self, owner=None, content="private source text", label="private label"):
        return self.api("POST", "/v1/sources", owner or self.morgan,
                        {"kind": "context", "label": label, "content": content})

    def file(self, owner=None, value=b"\x00PNG fixture\xff\r\n", filename="final logo.png"):
        return self.api("POST", "/v1/sources", owner or self.morgan, {
            "kind": "file", "label": "Approved logo", "filename": filename,
            "media_type": "image/png",
            "data_base64": base64.b64encode(value).decode("ascii"),
        })

    def grant(self, kind="message", sources=(), sender=None, recipient=None,
              owner=None, expires_at=None):
        data = {
            "sender_agent_id": (sender or self.a)["id"],
            "recipient_agent_id": (recipient or self.s)["id"],
            "actions": [kind], "source_ids": [s["id"] for s in sources],
        }
        if expires_at is not None:
            data["expires_at"] = expires_at
        return self.api("POST", "/v1/grants", owner or self.morgan, data)

    def request_payload(self, kind="message", sources=(), recipient=None,
                        key=None, message="Please review this"):
        return {
            "recipient_agent_id": (recipient or self.s)["id"], "kind": kind,
            "message": message, "source_ids": [s["id"] for s in sources],
            "idempotency_key": key or uuid.uuid4().hex,
        }

    def request(self, kind="message", sources=(), sender=None, recipient=None,
                key=None, expect=None):
        return self.api("POST", "/v1/requests", sender or self.a,
                        self.request_payload(kind, sources, recipient, key), expect)

    def action(self, request, action, actor=None, data=None, expect=None):
        return self.api("POST", "/v1/requests/%s/%s" % (request["id"], action),
                        actor or self.s, {} if data is None else data, expect)

    def approve(self, request):
        return self.action(request, "decision", self.morgan, {"decision": "approve"})

    def sources(self, request, actor=None, expect=None):
        return self.api("GET", "/v1/requests/%s/sources" % request["id"],
                        actor or self.s, expect=expect)

    def complete(self, request, actor=None, sources=(), expect=None):
        return self.action(request, "complete", actor,
                           {"text": "Here is the result", "source_ids": [s["id"] for s in sources]},
                           expect)

    def download(self, request, source, actor=None, expect=None):
        return self.call("GET", "/v1/requests/%s/files/%s" % (request["id"], source["id"]),
                         actor or self.a, expect=expect, raw=True)

    def events(self, actor=None, after=0):
        return self.api("GET", "/v1/events/history?after=%s" % after,
                        actor or self.a)["events"]

    def test_health_and_identity(self):
        health = self.api("GET", "/health")
        self.assertTrue(health["ok"])
        self.assertEqual(health["protocol"], "airshower/0.1")
        for actor, role in [(self.avery, "owner"), (self.a, "agent")]:
            identity = self.api("GET", "/v1/me", actor)
            self.assertEqual(identity["id"], actor["id"])
            self.assertEqual(identity["role"], role)
            self.assertNotIn("token", identity)

    def test_missing_invalid_and_query_tokens_rejected(self):
        self.api("GET", "/v1/me", expect=401)
        self.api("GET", "/v1/me", "invalid-secret", expect=401)
        self.api("GET", "/v1/me?token=" + self.a["token"], expect=400)

    def test_bootstrap_has_no_ordinary_owner_authority(self):
        self.api("POST", "/v1/owners", self.avery, {"name": "Injected"}, 403)
        self.api("POST", "/v1/agents", self.ADMIN, {"name": "Injected"}, 403)
        self.api("GET", "/v1/sources", self.ADMIN, expect=403)

    def test_agent_cannot_administer_owner_or_approve(self):
        pending = self.request()
        for path, data in [
            ("/v1/agents", {"name": "Injected"}),
            ("/v1/contacts", {"owner_id": self.other["id"]}),
            ("/v1/sources", {"kind": "context", "label": "x", "content": "x"}),
            ("/v1/grants", {"sender_agent_id": self.a["id"], "recipient_agent_id": self.s["id"],
                            "actions": ["message"], "source_ids": []}),
        ]:
            with self.subTest(path=path):
                self.api("POST", path, self.s, data, 403)
        self.action(pending, "decision", self.s, {"decision": "approve"}, 403)

    def test_owner_cannot_impersonate_agent_for_requests(self):
        self.api("POST", "/v1/requests", self.avery, self.request_payload(), 403)

    def test_contact_approval_is_incoming_and_directional(self):
        self.request()
        self.request(sender=self.s, recipient=self.a, expect=(403, 404))
        self.request(sender=self.o, expect=(403, 404))
        ids = {a["id"] for a in self.api("GET", "/v1/agents", self.a)["agents"]}
        self.assertIn(self.s["id"], ids)
        self.assertNotIn(self.o["id"], ids)
        reverse = {a["id"] for a in self.api("GET", "/v1/agents", self.s)["agents"]}
        self.assertNotIn(self.a["id"], reverse)

    def test_unknown_or_other_owner_sources_do_not_leak_metadata(self):
        secret = self.context(self.other, "UNIQUE-SECRET-CONTENT", "UNIQUE-SECRET-LABEL")
        responses = []
        for source_id in (secret["id"], str(uuid.uuid4())):
            payload = self.request_payload("context")
            payload["source_ids"] = [source_id]
            error = self.api("POST", "/v1/requests", self.a, payload, (400, 403, 404))
            serialized = json.dumps(error)
            self.assertNotIn("UNIQUE-SECRET", serialized)
            self.assertNotIn(source_id, serialized)
            responses.append(error)
        self.assertEqual(responses[0], responses[1])

    def test_source_lists_are_owner_only_and_metadata_only(self):
        self.context(content="unique confidential body")
        asset = self.file()
        listing = self.api("GET", "/v1/sources", self.morgan)
        self.assertNotIn("unique confidential body", json.dumps(listing))
        self.assertNotIn("data_base64", json.dumps(listing))
        self.assertEqual(self.api("GET", "/v1/sources", self.avery)["sources"], [])
        self.api("GET", "/v1/sources", self.s, expect=403)
        self.api("DELETE", "/v1/sources/" + asset["id"], self.avery, expect=(403, 404))

    def test_pending_request_has_no_source_access_or_completion(self):
        source = self.context()
        pending = self.request("context", [source])
        self.assertEqual(pending["status"], "pending_approval")
        self.sources(pending, expect=(403, 409))
        self.action(pending, "claim", expect=(403, 409))
        self.complete(pending, expect=(403, 409))

    def test_grants_bind_sender_recipient_and_action(self):
        self.grant()
        self.assertEqual(self.request()["status"], "ready")
        self.assertEqual(self.request(sender=self.a2)["status"], "pending_approval")
        self.assertEqual(self.request(recipient=self.s2)["status"], "pending_approval")
        self.assertEqual(self.request("task")["status"], "pending_approval")

    def test_grants_cannot_combine_partial_source_sets(self):
        first, second = self.context(label="one"), self.context(label="two")
        self.grant("context", [first])
        self.grant("context", [second])
        request = self.request("context", [first, second])
        self.assertEqual(request["status"], "pending_approval")
        self.grant("context", [first, second])
        self.assertEqual(self.request("context", [first, second])["status"], "ready")

    def test_owner_cannot_grant_foreign_recipient_or_source(self):
        foreign = self.context(self.other)
        for recipient, source_ids in [(self.a, []), (self.s, [foreign["id"]])]:
            with self.subTest(recipient=recipient["id"]):
                self.api("POST", "/v1/grants", self.morgan, {
                    "sender_agent_id": self.a["id"], "recipient_agent_id": recipient["id"],
                    "actions": ["context"] if source_ids else ["message"],
                    "source_ids": source_ids,
                }, (400, 403, 404))

    def test_grant_without_contact_is_rejected(self):
        self.api("POST", "/v1/grants", self.morgan, {
            "sender_agent_id": self.o["id"], "recipient_agent_id": self.s["id"],
            "actions": ["message"], "source_ids": [],
        }, (403, 404))

    def test_expired_grant_cannot_authorize_request(self):
        # An implementation may reject an already expired grant at creation.
        data = {"sender_agent_id": self.a["id"], "recipient_agent_id": self.s["id"],
                "actions": ["message"], "source_ids": [], "expires_at": time.time() - 60}
        self.api("POST", "/v1/grants", self.morgan, data, (200, 201, 400))
        self.assertEqual(self.request()["status"], "pending_approval")

    def test_expiry_rechecked_after_request_creation(self):
        self.grant(expires_at=time.time() + 0.12)
        request = self.request()
        self.assertEqual(request["status"], "ready")
        time.sleep(0.15)
        self.action(request, "claim", expect=(403, 409))

    def test_revoked_grant_blocks_claim_sources_and_completion(self):
        source = self.context()
        grant = self.grant("context", [source])
        request = self.request("context", [source])
        self.api("DELETE", "/v1/grants/" + grant["id"], self.morgan)
        self.action(request, "claim", expect=(403, 409))
        self.sources(request, expect=(403, 409))
        self.complete(request, sources=[source], expect=(403, 409))

    def test_revocation_is_rechecked_for_already_claimed_work(self):
        source = self.context()
        grant = self.grant("context", [source])
        request = self.request("context", [source])
        self.assertEqual(self.action(request, "claim")["status"], "working")
        self.api("DELETE", "/v1/grants/" + grant["id"], self.morgan)
        self.sources(request, expect=(403, 409))
        self.complete(request, sources=[source], expect=(403, 409))

    def test_contact_revocation_overrides_explicit_owner_approval(self):
        request = self.request()
        self.approve(request)
        self.api("DELETE", "/v1/contacts/" + self.avery["id"], self.morgan)
        self.action(request, "claim", expect=(403, 409))
        self.complete(request, expect=(403, 409))
        self.request(expect=(403, 404))

    def test_explicit_approval_is_only_for_one_request(self):
        first = self.request()
        self.approve(first)
        self.assertEqual(self.complete(first)["status"], "completed")
        second = self.request()
        self.assertEqual(second["status"], "pending_approval")
        self.complete(second, expect=(403, 409))

    def test_request_visibility_and_actions_are_exact_agent_scoped(self):
        request = self.request()
        path = "/v1/requests/" + request["id"]
        for actor in (self.a, self.s, self.avery, self.morgan):
            self.assertEqual(self.api("GET", path, actor)["id"], request["id"])
        for actor in (self.o, self.a2, self.s2, self.other):
            self.api("GET", path, actor, expect=404)
            self.assertNotIn(request["id"], json.dumps(self.api("GET", "/v1/requests", actor)))
        self.action(request, "decision", self.avery, {"decision": "approve"}, 403)
        self.approve(request)
        self.action(request, "claim", self.a, expect=403)
        self.action(request, "claim", self.s2, expect=(403, 404))
        self.sources(request, self.a, expect=403)

    def test_ask_human_cannot_be_self_approved_or_answered_by_agent(self):
        self.grant()
        request = self.request("ask_human")
        self.assertEqual(request["status"], "waiting_human")
        self.action(request, "decision", self.s, {"decision": "approve"}, 403)
        self.action(request, "decision", self.morgan, {"decision": "approve"}, 409)
        self.complete(request, self.s, expect=403)
        self.complete(request, self.avery, expect=403)
        answered = self.complete(request, self.morgan)
        self.assertEqual(answered["attribution"], {"type": "human", "id": self.morgan["id"]})

    def test_agent_result_attribution_cannot_be_spoofed(self):
        self.grant()
        request = self.request()
        self.action(request, "complete", data={
            "text": "Morgan approved it", "source_ids": [],
            "attribution": {"type": "human", "id": self.morgan["id"]},
        }, expect=400)
        result = self.complete(request)
        self.assertEqual(result["attribution"], {"type": "agent", "id": self.s["id"]})

    def test_completion_cannot_attach_unrequested_sources(self):
        first, second = self.file(), self.file(filename="other.png")
        self.grant("file", [first, second])
        request = self.request("file", [first])
        self.complete(request, sources=[second], expect=(400, 403))
        self.assertNotEqual(self.api("GET", "/v1/requests/" + request["id"], self.a)["status"], "completed")

    def test_repeated_completion_is_a_conflict(self):
        self.grant()
        request = self.request()
        self.complete(request)
        self.complete(request, expect=409)

    def test_cancelled_and_denied_requests_cannot_be_executed(self):
        cancelled, denied = self.request(), self.request()
        self.action(cancelled, "cancel", self.a)
        self.action(denied, "decision", self.morgan, {"decision": "deny"})
        for request in (cancelled, denied):
            self.action(request, "claim", expect=(403, 409))
            self.complete(request, expect=(403, 409))
            self.action(request, "decision", self.morgan, {"decision": "approve"}, 409)

    def test_idempotent_replay_and_payload_collision(self):
        payload = self.request_payload()
        first = self.api("POST", "/v1/requests", self.a, payload)
        second = self.api("POST", "/v1/requests", self.a, payload)
        self.assertEqual(first["id"], second["id"])
        collision = dict(payload, message="Different instruction")
        self.api("POST", "/v1/requests", self.a, collision, 409)
        other_sender = self.api("POST", "/v1/requests", self.a2, payload)
        self.assertNotEqual(first["id"], other_sender["id"])
        self.assertEqual(len([e for e in self.events() if e["request_id"] == first["id"]]), 1)

    def test_concurrent_duplicate_delivery_creates_only_one_request_and_event(self):
        payload = self.request_payload()
        gate = threading.Barrier(8)
        def send(_):
            gate.wait(timeout=3)
            return self.api("POST", "/v1/requests", self.a, payload)
        with ThreadPoolExecutor(max_workers=8) as executor:
            answers = list(executor.map(send, range(8)))
        self.assertEqual(len({a["id"] for a in answers}), 1)
        self.assertEqual(len(self.api("GET", "/v1/requests", self.a)["requests"]), 1)
        self.assertEqual(len(self.events()), 1)

    def test_source_validation_rejects_paths_malformed_bytes_and_missing_fields(self):
        malformed = [
            {"kind": "context", "label": "x"},
            {"kind": "context", "label": "x", "content": "x", "path": "/etc/passwd"},
            {"kind": "file", "label": "x", "filename": "x.png", "media_type": "image/png"},
            {"kind": "file", "label": "x", "filename": "x.png", "media_type": "image/png", "data_base64": "!not-base64!"},
        ]
        for filename in ("../secret.png", "/tmp/secret.png", "dir/secret.png", "dir\\secret.png", "evil\r\nX-Evil: yes.png"):
            malformed.append({"kind": "file", "label": "x", "filename": filename,
                              "media_type": "image/png", "data_base64": "YQ=="})
        for data in malformed:
            with self.subTest(data=data):
                self.api("POST", "/v1/sources", self.morgan, data, 400)

    def test_required_request_fields_and_kind_source_consistency(self):
        source = self.context()
        file_source = self.file()
        base = self.request_payload("context", [source])
        for field in ("recipient_agent_id", "kind", "message", "source_ids", "idempotency_key"):
            data = dict(base)
            data.pop(field)
            with self.subTest(missing=field):
                self.api("POST", "/v1/requests", self.a, data, 400)
        for kind, sources in [("context", []), ("file", []), ("context", [file_source]),
                              ("file", [source]), ("message", [source]), ("ask_human", [source])]:
            with self.subTest(kind=kind, sources=sources):
                self.request(kind, sources, expect=(400, 403, 404))
        duplicate = dict(base, source_ids=[source["id"], source["id"]])
        self.api("POST", "/v1/requests", self.a, duplicate, 400)

    def test_file_and_text_size_limits(self):
        self.api("POST", "/v1/sources", self.morgan, {
            "kind": "context", "label": "oversize", "content": "x" * (65536 + 1),
        }, (400, 413))
        self.api("POST", "/v1/sources", self.morgan, {
            "kind": "file", "label": "oversize", "filename": "large.bin",
            "media_type": "application/octet-stream",
            "data_base64": base64.b64encode(b"x" * (5 * 1024 * 1024 + 1)).decode("ascii"),
        }, (400, 413))
        payload = self.request_payload(message="x" * (16384 + 1))
        self.api("POST", "/v1/requests", self.a, payload, (400, 413))

    def test_file_handoff_preserves_exact_bytes_hash_and_metadata(self):
        data = b"\x00\xffbinary logo bytes\r\n\x00"
        source = self.file(value=data, filename="Morgan final logo.png")
        self.assertEqual(source["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(source["size"], len(data))
        self.grant("file", [source])
        request = self.request("file", [source])
        material = self.sources(request)["sources"][0]
        self.assertNotIn("data_base64", material)
        self.assertEqual(material["sha256"], hashlib.sha256(data).hexdigest())
        completed = self.complete(request, sources=[source])
        self.assertNotIn("data_base64", json.dumps(completed))
        downloaded, headers = self.download(request, source)
        self.assertEqual(downloaded, data)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(headers["X-Content-SHA256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(self.download(request, source, self.avery)[0], data)

    def test_download_requires_completed_attachment_and_exact_sender(self):
        first, second = self.file(), self.file(filename="second.png")
        self.grant("file", [first, second])
        request = self.request("file", [first, second])
        self.download(request, first, expect=(403, 404, 409))
        self.complete(request, sources=[first])
        self.download(request, second, expect=(403, 404))
        for actor in (self.s, self.morgan, self.a2, self.o):
            self.download(request, first, actor, expect=(403, 404))

    def test_revoked_grant_blocks_completed_file_download(self):
        source = self.file()
        grant = self.grant("file", [source])
        request = self.request("file", [source])
        self.complete(request, sources=[source])
        self.api("DELETE", "/v1/grants/" + grant["id"], self.morgan)
        self.download(request, source, expect=(403, 404, 409))

    def test_deleted_source_blocks_work_and_preserves_request_history(self):
        source = self.file()
        self.grant("file", [source])
        request = self.request("file", [source])
        self.api("DELETE", "/v1/sources/" + source["id"], self.morgan)
        self.sources(request, expect=(403, 404, 409))
        self.complete(request, sources=[source], expect=(403, 404, 409))
        self.assertEqual(self.api("GET", "/v1/requests/" + request["id"], self.a)["id"], request["id"])

    def test_deleted_source_and_contact_revocation_block_download(self):
        for revoke in ("source", "contact"):
            with self.subTest(revoke=revoke):
                source = self.file(filename=revoke + ".png")
                request = self.request("file", [source])
                self.approve(request)
                self.complete(request, sources=[source])
                path = "/v1/sources/" + source["id"] if revoke == "source" else "/v1/contacts/" + self.avery["id"]
                self.api("DELETE", path, self.morgan)
                self.download(request, source, expect=(403, 404, 409))

    def test_readding_contact_does_not_resurrect_old_authority(self):
        source = self.file()
        self.grant("file", [source])
        completed = self.request("file", [source])
        self.complete(completed, sources=[source])
        pending_work = self.request("file", [source])
        self.api("DELETE", "/v1/contacts/" + self.avery["id"], self.morgan)
        self.contact(self.morgan, self.avery)
        self.download(completed, source, expect=(403, 404, 409))
        self.action(pending_work, "claim", expect=(403, 409))
        self.complete(pending_work, sources=[source], expect=(403, 409))
        self.assertEqual(self.request("file", [source])["status"], "pending_approval")

    def test_expiry_blocks_sources_completion_and_completed_download(self):
        source = self.file()
        expiry = time.time() + 0.15
        self.grant("file", [source], expires_at=expiry)
        completed = self.request("file", [source])
        self.complete(completed, sources=[source])
        working = self.request("file", [source])
        self.action(working, "claim")
        time.sleep(max(0, expiry - time.time()) + 0.03)
        self.sources(working, expect=(403, 409))
        self.complete(working, sources=[source], expect=(403, 409))
        self.download(completed, source, expect=(403, 404, 409))

    def test_completion_requires_text_and_explicit_source_ids(self):
        self.grant()
        request = self.request()
        for data in ({"text": "x"}, {"source_ids": []}, {},
                     {"text": "x" * 16385, "source_ids": []}):
            with self.subTest(keys=list(data)):
                self.action(request, "complete", data=data, expect=400)

    def test_wrong_json_types_are_validation_errors_not_internal_failures(self):
        cases = [
            ("/v1/agents", self.avery, {"name": []}),
            ("/v1/contacts", self.morgan, {"owner_id": {}}),
            ("/v1/sources", self.morgan, {"kind": [], "label": "x"}),
            ("/v1/sources", self.morgan, {"kind": "context", "label": "x", "content": {}}),
            ("/v1/sources", self.morgan, {"kind": "file", "label": "x", "filename": [],
                                      "media_type": "text/plain", "data_base64": "YQ=="}),
        ]
        base = self.request_payload()
        for field in base:
            cases.append(("/v1/requests", self.a, dict(base, **{field: {}})))
        grant = {"sender_agent_id": self.a["id"], "recipient_agent_id": self.s["id"],
                 "actions": ["message"], "source_ids": []}
        for field in ("sender_agent_id", "recipient_agent_id", "actions", "source_ids", "expires_at"):
            cases.append(("/v1/grants", self.morgan, dict(grant, **{field: {}})))
        for path, actor, data in cases:
            with self.subTest(path=path, data=data):
                self.api("POST", path, actor, data, 400)

    def test_multiple_maximum_size_file_sources_are_metadata_only(self):
        data = b"A" * (5 * 1024 * 1024)
        first = self.file(value=data, filename="large-one.bin")
        second = self.file(value=data, filename="large-two.bin")
        self.grant("file", [first, second])
        request = self.request("file", [first, second])
        material = self.sources(request)
        self.assertEqual(len(material["sources"]), 2)
        self.assertLess(len(json.dumps(material)), 4096)
        for source in material["sources"]:
            self.assertNotIn("data_base64", source)
            self.assertNotIn("content", source)
            self.assertEqual(source["size"], len(data))
            self.assertEqual(source["sha256"], hashlib.sha256(data).hexdigest())
        self.complete(request, sources=[first, second])
        self.assertEqual(self.download(request, first)[0], data)

    def test_context_selection_limit_counts_aggregate_utf8_bytes(self):
        sender = Client(self.base, self.a["token"])
        # Each source is individually valid. Multibyte text catches limits
        # accidentally calculated with Python character counts instead of UTF-8.
        for content in ("x" * 65536, "\u00e9" * 32768):
            with self.subTest(bytes_per_character=len(content[0].encode("utf-8"))):
                selected = [self.context(content=content, label="Context %d" % i)
                            for i in range(16)]
                selected.append(self.context(content="x", label="One byte over"))
                payload = self.request_payload("context", selected)
                with self.assertRaises(ClientAPIError) as raised:
                    sender.post("/v1/requests", payload)
                self.assertIn(raised.exception.status, (400, 413))
        self.assertEqual(sender.get("/v1/requests")["requests"], [])

    def test_maximum_escaped_context_selection_is_readable_by_live_client(self):
        # JSON turns each control byte into six bytes. The allowed selection
        # must still fit the real client's response cap after that expansion.
        content = "\x01" * 65536
        selected = [self.context(content=content, label="Escaped context %d" % i)
                    for i in range(16)]
        # A grant can cover a larger library; only material selected by one
        # request must fit the aggregate response budget.
        additional = self.context(content="x", label="Additional permitted context")
        self.grant("context", selected + [additional])
        sender = Client(self.base, self.a["token"])
        recipient = Client(self.base, self.s["token"])
        request = sender.post("/v1/requests", self.request_payload("context", selected))
        self.assertEqual(request["status"], "ready")
        material = recipient.get("/v1/requests/%s/sources" % request["id"])
        self.assertEqual(len(material["sources"]), 16)
        self.assertEqual(sum(len(source["content"].encode("utf-8"))
                             for source in material["sources"]), 1024 * 1024)
        for source in material["sources"]:
            self.assertEqual(source["content"], content)

    def test_unsupported_method_bodies_cannot_be_reinterpreted_as_requests(self):
        for method in ("OPTIONS", "PUT", "PATCH", "HEAD"):
            with self.subTest(method=method):
                injected = b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
                request = (method + " /v1/me HTTP/1.1\r\nHost: localhost\r\nContent-Length: "
                           + str(len(injected)) + "\r\n\r\n").encode("ascii") + injected
                with socket.create_connection(self.server.server_address, timeout=3) as connection:
                    connection.sendall(request)
                    chunks = []
                    while True:
                        chunk = connection.recv(65536)
                        if not chunk:
                            break
                        chunks.append(chunk)
                response = b"".join(chunks)
                self.assertTrue(response.startswith(b"HTTP/1.1 405"), response)
                self.assertEqual(response.count(b"HTTP/1.1 "), 1, response)
                if method == "HEAD":
                    self.assertEqual(response.split(b"\r\n\r\n", 1)[1], b"")

    def test_sse_does_not_emit_unrelated_requests(self):
        response = self.sse_connection(self.s)
        self.assertEqual(response.readline().strip(), b": connected")
        self.contact(self.other, self.avery)
        unrelated = self.request(recipient=self.o)
        related = self.request()
        event = self.next_sse_event(response)
        self.assertEqual(event["request_id"], related["id"])
        self.assertNotEqual(event["request_id"], unrelated["id"])

    def test_events_are_metadata_only_and_actor_isolated(self):
        source = self.context(content="private source material")
        payload = self.request_payload("context", [source], message="PRIVATE USER PROMPT")
        request = self.api("POST", "/v1/requests", self.a, payload)
        self.approve(request)
        self.complete(request, sources=[source])
        events = self.events()
        self.assertGreaterEqual(len(events), 3)
        for event in events:
            self.assertEqual(set(event), {"id", "type", "request_id", "status", "created_at"})
            self.assertEqual(event["request_id"], request["id"])
        text = json.dumps(events)
        for secret in ("PRIVATE USER PROMPT", "private source material", "Here is the result", self.a["token"]):
            self.assertNotIn(secret, text)
        for actor in (self.o, self.other, self.a2, self.s2):
            self.assertEqual(self.events(actor), [])
        self.assertEqual(self.events(self.avery), events)
        self.assertEqual(self.events(self.morgan), events)

    def sse_connection(self, actor, after=0, header=False):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        headers = {"Authorization": "Bearer " + actor["token"]}
        path = "/v1/events"
        if header:
            headers["Last-Event-ID"] = str(after)
        else:
            path += "?after=%d" % after
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertTrue(response.headers["Content-Type"].startswith("text/event-stream"))
        self.addCleanup(response.close)
        self.addCleanup(connection.close)
        return response

    def next_sse_event(self, response):
        event = {}
        for _ in range(30):
            line = response.readline().decode("utf-8").rstrip("\r\n")
            if line.startswith("id: "):
                event["id"] = int(line[4:])
            elif line.startswith("data: "):
                event["data"] = json.loads(line[6:])
            elif not line and "data" in event:
                self.assertEqual(event["id"], event["data"]["id"])
                return event["data"]
        self.fail("No SSE event arrived")

    def test_sse_push_arrives_on_already_open_connection(self):
        response = self.sse_connection(self.s)
        # Reading the initial comment establishes the stream before any request exists.
        self.assertEqual(response.readline().strip(), b": connected")
        started = time.monotonic()
        request = self.request()
        event = self.next_sse_event(response)
        self.assertEqual(event["request_id"], request["id"])
        self.assertLess(time.monotonic() - started, 2)

    def test_sse_replay_and_tokens_survive_restart(self):
        request = self.request()
        first = self.events()[0]
        self.approve(request)
        self.complete(request)
        expected = self.events(after=first["id"])
        self.stop_server()
        self.start_server()
        self.assertEqual(self.events(after=first["id"]), expected)
        response = self.sse_connection(self.a, first["id"], header=True)
        for event in expected:
            self.assertEqual(self.next_sse_event(response), event)
        new = self.request()
        live = self.next_sse_event(response)
        self.assertEqual(live["request_id"], new["id"])
        self.assertGreater(live["id"], expected[-1]["id"])

    def test_malformed_json_origin_and_event_cursors_are_rejected(self):
        self.call("GET", "/v1/me", self.a, expect=403,
                  headers={"Origin": "https://untrusted.example"})
        for cursor in ("-1", "not-a-number", "9223372036854775808"):
            self.api("GET", "/v1/events/history?after=" + cursor, self.a, expect=400)
        for raw in (b'{"name":"first","name":"second"}', b'{"expires_at":NaN}', b'[]'):
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
            try:
                connection.request("POST", "/v1/agents", body=raw, headers={
                    "Authorization": "Bearer " + self.avery["token"], "Content-Type": "application/json",
                })
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                error = response.read()
                self.assertNotIn(b"Traceback", error)
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
