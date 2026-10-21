"""Native host protocol tests, with fake UI replies and a real HTTP relay.

These do not run provider inference or establish desktop-app compatibility.
"""

import io
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from airshower.client import Client
from airshower.host import HostServer, run_stdio
from airshower.mcp import MAX_INPUT_BYTES, MCPServer, TOOLS
from airshower.server import create_server
from airshower.store import Store


def init(server, elicitation=None, notify=True):
    capabilities = {} if elicitation is None else {"elicitation": elicitation}
    result = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2026-07-28", "capabilities": capabilities,
        "clientInfo": {"name": "test-host", "version": "1"}}})
    if notify:
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return result


def decoded(result):
    return json.loads(result["content"][0]["text"])


class FakeAgent:
    def __init__(self):
        self.history = []
        self.request = {"id": "r1", "status": "pending_approval",
                        "recipient_agent_id": "a1", "sender_agent_id": "a2"}
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        if path == "/v1/me":
            return {"id": "a1", "owner_id": "o1", "role": "agent"}
        if path.startswith("/v1/events/history"):
            after = int(path.rsplit("=", 1)[1])
            return {"events": [e for e in self.history if e["id"] > after]}
        if path.startswith("/v1/requests/"):
            return self.request
        return {"agents": []}

    def events(self, after=0, timeout=2):
        # Include duplicates deliberately: reconnection must deduplicate.
        for event in self.history + self.history:
            yield event


class HostProtocolTests(unittest.TestCase):
    def test_channel_only_after_initialized_and_no_permission_relay(self):
        messages = []
        client = FakeAgent()
        client.history = [{"id": 1, "type": "request.created", "status": "pending_approval",
                           "request_id": "r1", "secret": "do not emit"}]
        server = HostServer(client, channel=True, send=messages.append)
        self.addCleanup(server.close)
        result = init(server, notify=False)
        self.assertEqual(result["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(result["result"]["capabilities"]["experimental"], {"claude/channel": {}})
        self.assertIsNone(server.channel_thread)
        self.assertEqual(messages, [])
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        deadline = time.monotonic() + 1
        while not messages and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(len(messages), 1)
        self.assertEqual(set(messages[0]["params"]["meta"]),
                         {"event_id", "request_id", "event_type", "status"})
        self.assertNotIn("do not emit", json.dumps(messages))
        self.assertNotIn("permission", json.dumps(result))

    def test_restart_replays_without_acknowledging(self):
        client = FakeAgent()
        client.history = [{"id": 1, "type": "request.created", "status": "pending_approval", "request_id": "r1"}]
        for _ in range(2):
            messages = []
            server = HostServer(client, channel=True, send=messages.append)
            init(server)
            deadline = time.monotonic() + 1
            while not messages and time.monotonic() < deadline:
                time.sleep(.01)
            server.close()
            self.assertEqual(messages[0]["params"]["meta"]["event_id"], "1")
        self.assertEqual(len(client.history), 1)

    def test_channel_filters_working_and_stale_state(self):
        client = FakeAgent()
        server = HostServer(client)
        actor = client.get("/v1/me")
        event = {"id": 1, "type": "request.working", "status": "working", "request_id": "r1"}
        client.request["status"] = "working"
        self.assertIsNone(server._channel_event(event, actor))
        event.update(type="request.created", status="pending_approval")
        self.assertIsNone(server._channel_event(event, actor))
        event.update(type="request.completed", status="completed")
        client.request.update(status="completed", sender_agent_id="a1", recipient_agent_id="a2")
        self.assertIsNotNone(server._channel_event(event, actor))

    def test_tool_list_depends_on_form_support_without_mutating_base(self):
        original = json.dumps(TOOLS, sort_keys=True)
        for capability, expected in [(None, False), ({"url": {}}, False), ({}, True), ({"form": {}}, True)]:
            server = HostServer(FakeAgent(), owner_client=object(), send=lambda _: None)
            init(server, capability)
            listing = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            by_name = {t["name"]: t for t in listing["result"]["tools"]}
            self.assertEqual("review_request" in by_name, expected)
            self.assertIn("wait_for_request", by_name)
            if expected:
                self.assertTrue(by_name["review_request"]["_meta"]["anthropic/requiresUserInteraction"])
        self.assertEqual(json.dumps(TOOLS, sort_keys=True), original)

    def test_wait_replays_metadata_and_validates_bounds(self):
        client = FakeAgent()
        client.history = [{"id": 3, "type": "request.created", "status": "pending_approval", "request_id": "r1", "text": "secret"}]
        server = HostServer(client)
        result = server.call_tool("wait_for_request", {"after": 2, "timeout_seconds": 1})
        data = decoded(result)
        self.assertEqual(data["after"], 3)
        self.assertFalse(data["timed_out"])
        self.assertNotIn("secret", json.dumps(data))
        for args in [{"after": True}, {"after": -1}, {"timeout_seconds": 0},
                     {"timeout_seconds": 46}, {"timeout_seconds": 1.0}, {"token": "secret"}]:
            self.assertTrue(server.call_tool("wait_for_request", args)["isError"])

    def test_errors_redact_credentials_and_paths(self):
        class BadAgent(FakeAgent):
            def get(self, path):
                raise RuntimeError("https://secret-token@host/secret/path")
        result = HostServer(BadAgent()).call_tool("review_request", {"request_id": "r1"})
        self.assertTrue(result["isError"])
        self.assertNotIn("secret", json.dumps(result))

    def test_protocol_max_line_and_duplicate_keys(self):
        output = io.StringIO()
        code = run_stdio(client=FakeAgent(), input_stream=io.BytesIO(b"x" * (MAX_INPUT_BYTES + 1)), output_stream=output)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], -32700)
        output = io.StringIO()
        code = run_stdio(client=FakeAgent(), input_stream=io.BytesIO(b'{"id":1,"id":2}\n'), output_stream=output)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], -32700)


class NativeReviewHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(str(Path(self.temp.name) / "test.db"), "host-test-admin-token")
        self.addCleanup(self.store.close)
        self.http = create_server(self.store, port=0)
        self.addCleanup(self.http.server_close)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.shutdown)
        self.url = "http://127.0.0.1:{}".format(self.http.server_address[1])
        admin = Client(self.url, "host-test-admin-token")
        o1 = admin.post("/v1/owners", {"name": "Sender"})
        o2 = admin.post("/v1/owners", {"name": "Recipient"})
        self.sender_owner = Client(self.url, o1["token"])
        self.recipient_owner = Client(self.url, o2["token"])
        a1 = self.sender_owner.post("/v1/agents", {"name": "Sender agent"})
        self.a2 = self.recipient_owner.post("/v1/agents", {"name": "Recipient agent"})
        self.sender = Client(self.url, a1["token"])
        self.recipient = Client(self.url, self.a2["token"])
        self.recipient_owner.post("/v1/contacts", {"owner_id": o1["id"]})

    def request(self, kind="message"):
        return self.sender.post("/v1/requests", {
            "recipient_agent_id": self.a2["id"], "kind": kind,
            "message": "Canonical remote request: please meet next week.",
            "source_ids": [], "idempotency_key": "request-" + kind})

    def reviewing_server(self, response, owner=None, callback=None):
        messages = []
        def send(message):
            messages.append(message)
            if callback:
                callback(message)
            if response is not None:
                server.handle({"jsonrpc": "2.0", "id": message["id"], "result": response})
        server = HostServer(self.recipient, owner or self.recipient_owner,
                            send=send, review_timeout=.05)
        init(server, {})
        self.addCleanup(server.close)
        return server, messages

    def test_approve_uses_canonical_request_and_host_decision(self):
        request = self.request()
        server, messages = self.reviewing_server({"action": "accept", "content": {"decision": "approve"}})
        result = server.call_tool("review_request", {"request_id": request["id"]})
        self.assertFalse(result["isError"], result)
        self.assertEqual(decoded(result)["status"], "ready")
        self.assertIn(request["message"], messages[0]["params"]["message"])
        self.assertEqual(messages[0]["params"]["requestedSchema"]["properties"]["decision"]["enum"], ["approve", "deny"])
        self.assertNotIn(self.recipient_owner._token, json.dumps(messages) + json.dumps(result))

    def test_deny_and_host_decline_are_different(self):
        request = self.request()
        server, _ = self.reviewing_server({"action": "decline"})
        result = server.call_tool("review_request", {"request_id": request["id"]})
        self.assertFalse(decoded(result)["changed"])
        self.assertEqual(self.sender.get("/v1/requests/" + request["id"])["status"], "pending_approval")
        server, _ = self.reviewing_server({"action": "accept", "content": {"decision": "deny"}})
        self.assertEqual(decoded(server.call_tool("review_request", {"request_id": request["id"]}))["status"], "denied")

    def test_human_answer_requires_host_content_and_gets_human_attribution(self):
        request = self.request("ask_human")
        server, messages = self.reviewing_server({"action": "accept", "content": {"answer": "Thursday works for me."}})
        result = decoded(server.call_tool("review_request", {"request_id": request["id"]}))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["text"], "Thursday works for me.")
        self.assertEqual(result["attribution"]["type"], "human")
        self.assertEqual(set(messages[0]["params"]["requestedSchema"]["properties"]), {"answer"})

    def test_agent_cannot_supply_decision_answer_or_owner_token(self):
        request = self.request()
        server, messages = self.reviewing_server({"action": "accept", "content": {"decision": "approve"}})
        for extra in [{"decision": "approve"}, {"answer": "yes"}, {"owner_token": "secret"}]:
            self.assertTrue(server.call_tool("review_request", dict(request_id=request["id"], **extra))["isError"])
        self.assertEqual(messages, [])
        self.assertEqual(self.sender.get("/v1/requests/" + request["id"])["status"], "pending_approval")

    def test_wrong_owner_or_wrong_recipient_never_gets_form(self):
        request = self.request()
        server, messages = self.reviewing_server({"action": "accept", "content": {"decision": "approve"}}, owner=self.sender_owner)
        self.assertTrue(server.call_tool("review_request", {"request_id": request["id"]})["isError"])
        self.assertEqual(messages, [])
        server.client = self.sender
        server.owner_client = self.sender_owner
        self.assertTrue(server.call_tool("review_request", {"request_id": request["id"]})["isError"])
        self.assertEqual(messages, [])

    def test_no_elicitation_or_invalid_response_fails_closed(self):
        request = self.request()
        server = HostServer(self.recipient, self.recipient_owner, send=lambda _: None)
        init(server)
        self.assertTrue(server.call_tool("review_request", {"request_id": request["id"]})["isError"])
        for response in [{"action": "accept", "content": {"decision": "approve", "source_ids": ["extra"]}},
                         {"action": "accept", "content": {"decision": "yes"}},
                         {"action": "accept"}, {"action": "accept", "content": {"decision": "approve"}, "extra": True}]:
            server, _ = self.reviewing_server(response)
            self.assertTrue(server.call_tool("review_request", {"request_id": request["id"]})["isError"])
        self.assertEqual(self.sender.get("/v1/requests/" + request["id"])["status"], "pending_approval")

    def test_timeout_and_state_change_fail_closed(self):
        request = self.request()
        server, _ = self.reviewing_server(None)
        self.assertEqual(decoded(server.call_tool("review_request", {"request_id": request["id"]}))["error"]["code"], "review_timeout")
        def cancel(_):
            self.sender.post("/v1/requests/" + request["id"] + "/cancel", {})
        server, _ = self.reviewing_server({"action": "accept", "content": {"decision": "approve"}}, callback=cancel)
        self.assertTrue(server.call_tool("review_request", {"request_id": request["id"]})["isError"])
        self.assertEqual(self.sender.get("/v1/requests/" + request["id"])["status"], "cancelled")

    def test_base_agent_tools_still_cannot_approve_or_answer_as_owner(self):
        request = self.request("ask_human")
        server, _ = self.reviewing_server(None)
        self.assertTrue(server.call_tool("decision", {"request_id": request["id"], "decision": "approve"})["isError"])
        self.assertTrue(server.call_tool("complete_request", {"request_id": request["id"], "text": "yes"})["isError"])
        self.assertEqual(self.sender.get("/v1/requests/" + request["id"])["status"], "waiting_human")

    def test_real_history_wait_observes_new_request(self):
        server = HostServer(self.recipient, poll_interval=.01)
        results = []
        thread = threading.Thread(target=lambda: results.append(server.call_tool("wait_for_request", {"timeout_seconds": 2})))
        thread.start()
        request = self.request()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(decoded(results[0])["events"][0]["request_id"], request["id"])

    def test_stdio_reader_accepts_form_response_while_tool_waits(self):
        request = self.request("ask_human")
        read_fd, write_fd = os.pipe()
        source = os.fdopen(read_fd, "rb")
        writer = os.fdopen(write_fd, "wb", buffering=0)
        self.addCleanup(source.close)
        self.addCleanup(writer.close)
        replies = queue.Queue()
        class Output:
            def write(self, line):
                replies.put(json.loads(line))
            def flush(self):
                pass
        thread = threading.Thread(target=run_stdio, kwargs={
            "client": self.recipient, "owner_client": self.recipient_owner,
            "input_stream": source, "output_stream": Output(), "review_timeout": 2})
        thread.start()
        def send(message):
            writer.write((json.dumps(message) + "\n").encode())
        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {"elicitation": {}},
                "clientInfo": {"name": "fake-native-host", "version": "1"}}})
            self.assertEqual(replies.get(timeout=2)["id"], 1)
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "review_request", "arguments": {"request_id": request["id"]}}})
            form = replies.get(timeout=2)
            self.assertEqual(form["method"], "elicitation/create")
            send({"jsonrpc": "2.0", "id": form["id"], "result": {
                "action": "accept", "content": {"answer": "A real fake-host reply."}}})
            response = replies.get(timeout=2)
            self.assertEqual(response["id"], 2)
            self.assertEqual(decoded(response["result"])["result"]["text"], "A real fake-host reply.")
        finally:
            writer.close()
            thread.join(3)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
