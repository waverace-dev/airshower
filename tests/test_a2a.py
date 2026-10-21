"""HTTP tests of the optional facade using a permission-checking fake backend."""

import base64
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from airshower.a2a import EXTENSION_URI, create_a2a_server


class A2ATests(unittest.TestCase):
    def setUp(self):
        self.records = {}
        self.calls = []
        self.keys = {}
        self.redirect_to = None
        test = self

        class Backend(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, value):
                raw = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                auth = self.headers.get("Authorization")
                test.calls.append(("GET", self.path, auth, None))
                if test.redirect_to:
                    self.send_response(302)
                    self.send_header("Location", test.redirect_to)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if auth not in ("Bearer sender-token", "Bearer recipient-token"):
                    return self.reply(401, {"error": {"message": "secret backend detail"}})
                record = test.records.get(self.path.removeprefix("/v1/requests/"))
                if not record:
                    return self.reply(404, {})
                return self.reply(200, record)

            def do_POST(self):
                auth = self.headers.get("Authorization")
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                test.calls.append(("POST", self.path, auth, data))
                if auth != "Bearer sender-token":
                    return self.reply(403, {})
                if self.path == "/v1/requests":
                    key = data["idempotency_key"]
                    if key in test.keys:
                        old, record_id = test.keys[key]
                        if old != data:
                            return self.reply(409, {})
                        return self.reply(200, test.records[record_id])
                    record_id = "request-" + str(len(test.records) + 1)
                    record = dict(data, id=record_id, sender_agent_id="sender",
                                  status="waiting_human" if data["kind"] == "ask_human" else "pending_approval",
                                  created_at=1700000000, updated_at=1700000001)
                    test.records[record_id] = record
                    test.keys[key] = (data, record_id)
                    return self.reply(201, record)
                if self.path.endswith("/cancel"):
                    record_id = self.path.split("/")[-2]
                    record = test.records[record_id]
                    if record["status"] in ("completed", "denied", "cancelled"):
                        return self.reply(409, {})
                    record["status"] = "cancelled"
                    return self.reply(200, record)
                return self.reply(404, {})

        self.backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        self.backend_url = "http://127.0.0.1:" + str(self.backend.server_port)
        self.facade = create_a2a_server(self.backend_url, "recipient", "https://bridge.example/a2a")
        self.url = "http://127.0.0.1:" + str(self.facade.server_port)
        self.threads = []
        for server in (self.backend, self.facade):
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            self.threads.append(thread)

    def tearDown(self):
        for server in (self.facade, self.backend):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(2)

    def request(self, method, path, body=None, headers=None, raw=None):
        request_headers = {"Authorization": "Bearer sender-token", "A2A-Version": "1.0",
                           "A2A-Extensions": EXTENSION_URI, "Content-Type": "application/a2a+json"}
        for key, value in (headers or {}).items():
            if value is None:
                request_headers.pop(key, None)
            else:
                request_headers[key] = value
        if raw is None and body is not None:
            raw = json.dumps(body).encode()
        try:
            response = urlopen(Request(self.url + path, method=method, data=raw,
                                       headers=request_headers), timeout=2)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read()), response.headers

    def message(self, kind="message", sources=None):
        return {"message": {"messageId": "msg-1", "role": "ROLE_USER",
                            "parts": [{"text": "Please ask about the final logo"}],
                            "metadata": {EXTENSION_URI: {"version": "0.1", "kind": kind,
                                                         "sourceIds": sources or []}}},
                "configuration": {"returnImmediately": True}}

    def send(self, body=None):
        return self.request("POST", "/a2a/message:send", body or self.message())

    def test_card_is_public_accurate_and_uses_10_security_shape(self):
        status, card, _ = self.request("GET", "/.well-known/agent-card.json", headers={
            "Authorization": None, "A2A-Version": None, "A2A-Extensions": None})
        self.assertEqual(status, 200)
        self.assertEqual(card["supportedInterfaces"], [{"url": "https://bridge.example/a2a",
                         "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"}])
        self.assertFalse(card["capabilities"]["streaming"])
        self.assertFalse(card["capabilities"]["pushNotifications"])
        self.assertTrue(card["capabilities"]["extensions"][0]["required"])
        self.assertEqual(card["securityRequirements"][0]["schemes"]["airshowerBearer"], {"list": []})
        self.assertEqual(self.calls, [])

    def test_missing_or_wrong_version_fails_before_backend(self):
        for version in (None, "0.3", "2.0"):
            status, value, _ = self.request("POST", "/a2a/message:send", self.message(),
                                           headers={"A2A-Version": version})
            self.assertEqual(status, 400)
            self.assertEqual(value["error"]["details"][0]["reason"], "VERSION_NOT_SUPPORTED")
        self.assertEqual(self.calls, [])

    def test_version_query_supported_and_conflict_rejected(self):
        status, _, _ = self.request("POST", "/a2a/message:send?A2A-Version=1.0", self.message(),
                                    headers={"A2A-Version": None})
        self.assertEqual(status, 200)
        status, _, _ = self.request("GET", "/a2a/tasks/request-1?A2A-Version=0.3")
        self.assertEqual(status, 400)

    def test_missing_extension_and_token_fail_before_backend(self):
        status, value, _ = self.request("POST", "/a2a/message:send", self.message(),
                                        headers={"A2A-Extensions": None})
        self.assertEqual(status, 400)
        self.assertEqual(value["error"]["details"][0]["reason"], "EXTENSION_SUPPORT_REQUIRED")
        status, _, headers = self.request("POST", "/a2a/message:send", self.message(),
                                          headers={"Authorization": None})
        self.assertEqual(status, 401)
        self.assertIn("Bearer", headers["WWW-Authenticate"])
        self.assertEqual(self.calls, [])

    def test_forwarded_identity_fixed_recipient_and_pending_owner(self):
        status, value, headers = self.send()
        self.assertEqual(status, 200)
        self.assertEqual(headers["A2A-Extensions"], EXTENSION_URI)
        task = value["task"]
        self.assertEqual(task["status"]["state"], "TASK_STATE_WORKING")
        self.assertEqual(task["metadata"][EXTENSION_URI]["waitingFor"], "owner")
        self.assertEqual(self.calls[0][2], "Bearer sender-token")
        self.assertEqual(self.calls[0][3]["recipient_agent_id"], "recipient")
        self.assertNotIn("kind", task)

    def test_human_wait_is_not_client_input_required(self):
        _, value, _ = self.send(self.message("ask_human"))
        self.assertEqual(value["task"]["status"]["state"], "TASK_STATE_WORKING")
        self.assertEqual(value["task"]["metadata"][EXTENSION_URI]["status"], "waiting_human")

    def test_blocking_and_continuation_not_silently_changed(self):
        for field, value in (("returnImmediately", False), ("returnImmediately", None)):
            body = self.message()
            body["configuration"][field] = value
            self.assertEqual(self.send(body)[0], 400)
        body = self.message()
        body["message"]["contextId"] = "existing-context"
        self.assertEqual(self.send(body)[0], 400)
        self.assertEqual(self.calls, [])

    def test_request_cannot_assert_sender_or_approval(self):
        for field in ("sender_agent_id", "approved", "recipient_agent_id"):
            body = self.message()
            body["message"]["metadata"][EXTENSION_URI][field] = "attacker"
            self.assertEqual(self.send(body)[0], 400)
        self.assertEqual(self.calls, [])

    def test_explicit_sources_and_content_validation(self):
        self.assertEqual(self.send(self.message("file"))[0], 400)
        self.assertEqual(self.send(self.message("context", ["source", "source"]))[0], 400)
        body = self.message()
        body["message"]["parts"][0]["url"] = "https://untrusted.example/asset"
        self.assertEqual(self.send(body)[0], 400)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.send(self.message("file", ["source-1"]))[0], 200)

    def test_idempotent_replay_and_changed_payload_conflict(self):
        first = self.send()[1]["task"]["id"]
        self.assertEqual(self.send()[1]["task"]["id"], first)
        body = self.message()
        body["message"]["parts"][0]["text"] = "Different request"
        self.assertEqual(self.send(body)[0], 409)
        self.assertEqual(len(self.records), 1)

    def test_get_returns_task_and_filters_other_recipient(self):
        self.send()
        status, task, _ = self.request("GET", "/a2a/tasks/request-1?historyLength=0")
        self.assertEqual(status, 200)
        self.assertEqual(task["id"], "request-1")
        self.assertNotIn("task", task)
        self.assertNotIn("history", task)
        self.records["request-1"]["recipient_agent_id"] = "different-recipient"
        status, value, _ = self.request("GET", "/a2a/tasks/request-1")
        self.assertEqual(status, 404)
        self.assertEqual(value["error"]["details"][0]["reason"], "TASK_NOT_FOUND")

    def test_cancellation_is_idempotent_but_does_not_bypass_authority(self):
        self.send()
        for _ in range(2):
            status, value, _ = self.request("POST", "/a2a/tasks/request-1:cancel", {})
            self.assertEqual(status, 200)
            self.assertEqual(value["status"]["state"], "TASK_STATE_CANCELED")
        status, _, _ = self.request("POST", "/a2a/tasks/request-1:cancel", {},
                                    headers={"Authorization": "Bearer recipient-token"})
        self.assertEqual(status, 403)

    def test_completed_task_not_cancelable_and_preserves_human_attribution(self):
        self.send()
        self.records["request-1"].update(status="completed", result={"text": "Yes, use that version"},
                                         attribution={"type": "human", "id": "owner-2"})
        status, value, _ = self.request("POST", "/a2a/tasks/request-1:cancel", {})
        self.assertEqual(status, 400)
        self.assertEqual(value["error"]["details"][0]["reason"], "TASK_NOT_CANCELABLE")
        _, task, _ = self.request("GET", "/a2a/tasks/request-1")
        self.assertEqual(task["metadata"][EXTENSION_URI]["attribution"], {"type": "human", "id": "owner-2"})
        self.assertEqual(task["artifacts"][0]["parts"][0]["text"], "Yes, use that version")

    def test_file_metadata_never_turns_into_bearer_url_or_bytes(self):
        self.send(self.message("file", ["source-1"]))
        self.records["request-1"].update(status="completed", result={"text": "Here is the file",
            "files": [{"id": "source-1", "filename": "logo.png", "media_type": "image/png",
                       "sha256": "a" * 64, "size": 42, "data_base64": "SECRET", "token": "SECRET"}]})
        _, task, _ = self.request("GET", "/a2a/tasks/request-1")
        parts = task["artifacts"][0]["parts"]
        descriptor = parts[1]["data"]["files"][0]
        self.assertEqual(descriptor["downloadPath"], "/v1/requests/request-1/files/source-1")
        self.assertNotIn("SECRET", json.dumps(task))
        self.assertNotIn("sender-token", json.dumps(task))
        self.assertNotIn("url", descriptor)

    def test_unsupported_capabilities_are_explicit(self):
        for method, path in (("GET", "/a2a/tasks"), ("POST", "/a2a/message:stream"),
                             ("POST", "/a2a/tasks/request-1:subscribe")):
            status, value, _ = self.request(method, path, {} if method == "POST" else None)
            self.assertEqual(status, 400)
            self.assertEqual(value["error"]["details"][0]["reason"], "UNSUPPORTED_OPERATION")
        body = self.message()
        body["configuration"]["taskPushNotificationConfig"] = {"url": "https://example.org/hook"}
        status, value, _ = self.send(body)
        self.assertEqual(status, 400)
        self.assertEqual(value["error"]["details"][0]["reason"], "PUSH_NOTIFICATION_NOT_SUPPORTED")

    def test_bad_json_and_query_credentials_do_not_reach_backend(self):
        for raw in (b'{"message":{},"message":{}}', b'{"message":NaN}', b'[]'):
            self.assertEqual(self.request("POST", "/a2a/message:send", raw=raw)[0], 400)
        self.assertEqual(self.request("GET", "/a2a/tasks/request-1?token=secret")[0], 400)
        self.assertEqual(self.calls, [])

    def test_remote_plaintext_and_embedded_credentials_rejected(self):
        for url in ("http://remote.example", "https://user:pass@example.org", "https://example.org?token=secret"):
            with self.assertRaises(ValueError):
                create_a2a_server(url, "recipient", "https://example.org")

    def test_backend_auth_errors_are_sanitized(self):
        self.send()
        status, value, _ = self.request("GET", "/a2a/tasks/request-1", headers={"Authorization": "Bearer wrong-token"})
        self.assertEqual(status, 401)
        self.assertNotIn("secret backend detail", json.dumps(value))

    def test_backend_redirect_is_not_followed_with_caller_token(self):
        self.redirect_to = self.backend_url + "/credential-capture"
        status, _, _ = self.request("GET", "/a2a/tasks/request-1")
        self.assertEqual(status, 502)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1], "/v1/requests/request-1")


class A2ABackendIntegrationTests(unittest.TestCase):
    def test_real_permission_backend_human_reply_and_file_download(self):
        from airshower.client import APIError, Client
        from airshower.server import create_server
        from airshower.store import Store

        with tempfile.TemporaryDirectory(prefix="airshower-a2a-test-") as temp:
            store = Store(temp + "/state.sqlite3", "local-test-bootstrap-token")
            backend = create_server(store)
            backend_thread = threading.Thread(target=backend.serve_forever,
                                              kwargs={"poll_interval": 0.01}, daemon=True)
            backend_thread.start()
            facade = None
            facade_thread = None
            try:
                backend_url = "http://127.0.0.1:" + str(backend.server_port)
                admin = Client(backend_url, "local-test-bootstrap-token")
                first = admin.post("/v1/owners", {"name": "First owner"})
                second = admin.post("/v1/owners", {"name": "Second owner"})
                first_owner = Client(backend_url, first["token"])
                second_owner = Client(backend_url, second["token"])
                sender = first_owner.post("/v1/agents", {"name": "Sender"})
                recipient = second_owner.post("/v1/agents", {"name": "Recipient"})
                sender_client = Client(backend_url, sender["token"])
                recipient_client = Client(backend_url, recipient["token"])
                second_owner.post("/v1/contacts", {"owner_id": first["id"]})
                facade = create_a2a_server(backend_url, recipient["id"], "http://127.0.0.1:7341")
                facade_thread = threading.Thread(target=facade.serve_forever,
                                                 kwargs={"poll_interval": 0.01}, daemon=True)
                facade_thread.start()
                facade_url = "http://127.0.0.1:" + str(facade.server_port)

                def a2a(method, path, body=None):
                    raw = None if body is None else json.dumps(body).encode()
                    headers = {"Authorization": "Bearer " + sender["token"], "A2A-Version": "1.0",
                               "A2A-Extensions": EXTENSION_URI, "Content-Type": "application/a2a+json"}
                    with urlopen(Request(facade_url + path, data=raw, method=method,
                                         headers=headers), timeout=2) as response:
                        return json.loads(response.read())

                def submit(message_id, kind, sources=None):
                    return a2a("POST", "/message:send", {
                        "message": {"messageId": message_id, "role": "ROLE_USER",
                                    "parts": [{"text": "Please help with this request"}],
                                    "metadata": {EXTENSION_URI: {"version": "0.1", "kind": kind,
                                                                 "sourceIds": sources or []}}},
                        "configuration": {"returnImmediately": True},
                    })["task"]

                human = submit("human-001", "ask_human")
                self.assertEqual(human["metadata"][EXTENSION_URI]["status"], "waiting_human")
                second_owner.post("/v1/requests/" + human["id"] + "/complete",
                                  {"text": "My own answer", "source_ids": []})
                answered = a2a("GET", "/tasks/" + human["id"])
                self.assertEqual(answered["metadata"][EXTENSION_URI]["attribution"]["type"], "human")

                file_bytes = b"the original asset bytes"
                source = second_owner.post("/v1/sources", {
                    "label": "Approved asset", "kind": "file", "filename": "asset.bin",
                    "media_type": "application/octet-stream",
                    "data_base64": base64.b64encode(file_bytes).decode(),
                })
                grant = second_owner.post("/v1/grants", {
                    "sender_agent_id": sender["id"], "recipient_agent_id": recipient["id"],
                    "actions": ["file"], "source_ids": [source["id"]],
                })
                file_task = submit("file-001", "file", [source["id"]])
                self.assertEqual(file_task["status"]["state"], "TASK_STATE_SUBMITTED")
                recipient_client.post("/v1/requests/" + file_task["id"] + "/complete",
                                      {"text": "Approved file", "source_ids": [source["id"]]})
                completed = a2a("GET", "/tasks/" + file_task["id"])
                descriptor = completed["artifacts"][0]["parts"][1]["data"]["files"][0]
                self.assertEqual(descriptor["sha256"], source["sha256"])
                self.assertEqual(sender_client.download(file_task["id"], source["id"]), file_bytes)
                second_owner.delete("/v1/grants/" + grant["id"])
                with self.assertRaises(APIError):
                    sender_client.download(file_task["id"], source["id"])
            finally:
                if facade:
                    facade.shutdown()
                    facade.server_close()
                    facade_thread.join(2)
                backend.shutdown()
                backend.server_close()
                backend_thread.join(2)
                store.close()


if __name__ == "__main__":
    unittest.main()
