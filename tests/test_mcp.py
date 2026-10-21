"""Protocol and authority boundary tests; no provider calls or installs."""

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from airshower.client import APIError, Client, TransportError
from airshower.mcp import MAX_INPUT_BYTES, MCPServer, PROTOCOL_VERSION, TOOLS, run_stdio
from airshower.server import create_server
from airshower.store import Store


class FakeClient:
    def __init__(self, role="agent"):
        self.role = role
        self.calls = []
        self.responses = {}
        self.file_bytes = b"asset bytes"

    def get(self, path):
        self.calls.append(("GET", path))
        if path == "/v1/me":
            return {"id": "agent-one", "role": self.role}
        if path in self.responses:
            result = self.responses[path]
            if isinstance(result, Exception):
                raise result
            return result
        if path == "/v1/agents":
            return {"agents": [{"id": "agent-two", "name": "Peer"}]}
        if path == "/v1/requests":
            return {"requests": []}
        return {"id": "request-one", "status": "ready"}

    def post(self, path, payload):
        self.calls.append(("POST", path, payload))
        return {"id": "request-one", "status": "working"}

    def download(self, request_id, source_id):
        self.calls.append(("DOWNLOAD", request_id, source_id))
        return self.file_bytes


def initialize(server, request_id=1, version=PROTOCOL_VERSION):
    result = server.handle({"jsonrpc": "2.0", "id": request_id,
                           "method": "initialize", "params": {
                               "protocolVersion": version, "capabilities": {},
                               "clientInfo": {"name": "test-host", "version": "0.1"},
                           }})
    server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return result


def decoded(result):
    return json.loads(result["content"][0]["text"])


class MCPProtocolTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.server = MCPServer(self.client)

    def test_handshake_advertises_only_tools_and_legacy_pin(self):
        result = initialize(self.server, "request-1", version="2099-01-01")
        self.assertEqual(result["id"], "request-1")
        self.assertEqual(result["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(result["result"]["serverInfo"]["version"], "0.2.0")
        self.assertEqual(result["result"]["capabilities"], {"tools": {"listChanged": False}})
        listing = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(len(listing["result"]["tools"]), 9)
        self.assertEqual(self.client.calls, [])

    def test_tools_require_initialized_notification(self):
        initialize_message = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"}}}
        self.server.handle(initialize_message)
        result = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(result["error"]["code"], -32002)
        self.assertEqual(self.client.calls, [])

    def test_ping_preserves_integer_and_string_ids(self):
        for value in (0, 42, "42", ""):
            with self.subTest(value=value):
                result = self.server.handle({"jsonrpc": "2.0", "id": value, "method": "ping"})
                self.assertEqual(result, {"jsonrpc": "2.0", "id": value, "result": {}})

    def test_invalid_ids_rejected(self):
        for value in (None, True, False, 1.5, [], {}):
            with self.subTest(value=value):
                result = self.server.handle({"jsonrpc": "2.0", "id": value, "method": "ping"})
                self.assertEqual(result["error"]["code"], -32600)
                self.assertIsNone(result["id"])

    def test_call_notifications_never_execute(self):
        initialize(self.server)
        result = self.server.handle({"jsonrpc": "2.0", "method": "tools/call",
                                     "params": {"name": "cancel_request", "arguments": {"request_id": "r1"}}})
        self.assertIsNone(result)
        self.assertEqual(self.client.calls, [])

    def test_tool_errors_are_iserror_results(self):
        initialize(self.server)
        result = self.server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                     "params": {"name": "approve_human", "arguments": {}}})
        self.assertTrue(result["result"]["isError"])
        self.assertEqual(decoded(result["result"])["error"]["code"], "unknown_tool")

    def test_strict_argument_schemas_have_no_owner_authority(self):
        names = {tool["name"] for tool in TOOLS}
        self.assertFalse(names & {"approve", "decision", "grant", "contacts", "owners", "agents"})
        for tool in TOOLS:
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
        invalid = [
            ("peers", {"url": "https://malicious.invalid"}),
            ("get_request", {"request_id": "../owners"}),
            ("get_request", {"request_id": "r1/decision"}),
            ("get_request", {"request_id": 42}),
            ("download_file", {"request_id": "r1", "source_id": "s1", "path": "/tmp/x"}),
            ("complete_request", {"request_id": "r1", "text": "yes", "attribution": {"type": "human"}}),
            ("complete_request", {"request_id": "r1", "text": "yes", "source_ids": ["s1", "s1"]}),
            ("request", {"recipient_agent_id": "a1", "kind": "approve", "message": "yes", "idempotency_key": "key"}),
        ]
        for name, arguments in invalid:
            with self.subTest(name=name, arguments=arguments):
                result = self.server.call_tool(name, arguments)
                self.assertTrue(result["isError"])
                self.assertEqual(decoded(result)["error"]["code"], "invalid_arguments")
        self.assertEqual(self.client.calls, [])

    def test_text_limit_is_in_utf8_bytes_and_rejects_surrogates(self):
        for value in ("é" * 9000, "\ud800"):
            result = self.server.call_tool("complete_request", {"request_id": "r1", "text": value})
            self.assertEqual(decoded(result)["error"]["code"], "invalid_arguments")

    def test_owner_and_admin_tokens_rejected_before_side_effects(self):
        for role in ("owner", "admin", None):
            with self.subTest(role=role):
                client = FakeClient(role)
                result = MCPServer(client).call_tool("cancel_request", {"request_id": "r1"})
                self.assertTrue(result["isError"])
                self.assertEqual(decoded(result)["error"]["code"], "agent_token_required")
                self.assertEqual(client.calls, [("GET", "/v1/me")])

    def test_role_is_rechecked_for_every_execution(self):
        first = self.server.call_tool("peers", {})
        self.assertFalse(first["isError"])
        self.client.role = "owner"
        second = self.server.call_tool("peers", {})
        self.assertTrue(second["isError"])
        self.assertEqual(self.client.calls, [("GET", "/v1/me"), ("GET", "/v1/agents"),
                                             ("GET", "/v1/me")])

    def test_request_preserves_idempotency_key(self):
        args = {"recipient_agent_id": "a2", "kind": "ask_human", "message": "When?",
                "idempotency_key": "stable-key"}
        result = self.server.call_tool("request", args)
        self.assertFalse(result["isError"])
        self.assertEqual(self.client.calls[-1], ("POST", "/v1/requests", dict(args, source_ids=[])))

    def test_sources_hide_file_bytes(self):
        self.client.responses["/v1/requests/r1/sources"] = {"sources": [
            {"id": "s1", "kind": "file", "filename": "image.png", "data_base64": "PRIVATE"},
            {"id": "s2", "kind": "context", "content": "Permitted context"},
        ]}
        result = self.server.call_tool("read_sources", {"request_id": "r1"})
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(decoded(result)["sources"][1]["content"], "Permitted context")

    def test_inbox_filters_outgoing_requests(self):
        self.client.responses["/v1/requests"] = {"requests": [
            {"id": "r1", "recipient_agent_id": "agent-one"},
            {"id": "r2", "recipient_agent_id": "someone-else"},
        ]}
        result = self.server.call_tool("inbox", {})
        self.assertEqual([item["id"] for item in decoded(result)["requests"]], ["r1"])

    def test_remote_and_local_errors_are_sanitized(self):
        for error in (APIError(403, "SECRET-CODE", "SECRET-TOKEN and private content"),
                      TransportError("SECRET-TOKEN"), RuntimeError("SECRET-TOKEN")):
            with self.subTest(error=type(error).__name__):
                self.client.responses["/v1/agents"] = error
                result = self.server.call_tool("peers", {})
                self.assertTrue(result["isError"])
                self.assertNotIn("SECRET", json.dumps(result))

    def test_stdio_json_only_bad_json_duplicate_keys_and_nonfinite(self):
        incoming = (b'{"jsonrpc":"2.0","id":"p1","method":"ping"}\n'
                    b'not-json\n'
                    b'{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}\n'
                    b'{"jsonrpc":"2.0","id":NaN,"method":"ping"}\n')
        output, diagnostics = io.StringIO(), io.StringIO()
        status = run_stdio(io.BytesIO(incoming), output, diagnostics, self.client)
        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(status, 0)
        self.assertEqual(messages[0]["id"], "p1")
        self.assertEqual([msg["error"]["code"] for msg in messages[1:]], [-32700] * 3)
        self.assertEqual(diagnostics.getvalue(), "")

    def test_stdio_oversized_input_terminates_session(self):
        output = io.StringIO()
        status = run_stdio(io.BytesIO(b"x" * (MAX_INPUT_BYTES + 1)), output, io.StringIO(), self.client)
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], -32700)
        self.assertEqual(self.client.calls, [])

    def test_stdio_default_url_matches_serve_and_sample(self):
        with patch.dict(os.environ, {"AIRSHOWER_TOKEN": "test-agent-token"}, clear=True), \
                patch("airshower.mcp.Client", return_value=self.client) as client_factory:
            self.assertEqual(run_stdio(io.BytesIO(), io.StringIO(), io.StringIO()), 0)
        client_factory.assert_called_once_with("http://127.0.0.1:7340", "test-agent-token")
        sample = Path(__file__).resolve().parents[1] / "examples" / "mcp-config.json"
        config = json.loads(sample.read_text(encoding="utf-8"))
        self.assertEqual(config["mcpServers"]["airshower"]["env"]["AIRSHOWER_URL"],
                         "http://127.0.0.1:7340")


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = FakeClient()
        self.metadata = {"id": "s1", "filename": "../../outside.sh", "media_type": "text/plain",
                         "sha256": hashlib.sha256(self.client.file_bytes).hexdigest(),
                         "size": len(self.client.file_bytes)}
        self.client.responses["/v1/requests/r1"] = {
            "id": "r1", "kind": "file", "status": "completed", "result": {"files": [self.metadata]}}
        self.server = MCPServer(self.client, str(self.root))

    def download(self):
        return self.server.call_tool("download_file", {"request_id": "r1", "source_id": "s1"})

    def test_download_is_private_generated_name_integrity_verified(self):
        result = self.download()
        self.assertFalse(result["isError"], result)
        data = decoded(result)
        target = Path(data["path"])
        self.assertEqual(target.parent, self.root.resolve())
        self.assertRegex(target.name, r"^[0-9a-f]{32}\.bin$")
        self.assertEqual(target.read_bytes(), self.client.file_bytes)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(data["original_filename"], "../../outside.sh")

    def test_hash_mismatch_leaves_no_file(self):
        self.metadata["sha256"] = "0" * 64
        result = self.download()
        self.assertEqual(decoded(result)["error"]["code"], "integrity_error")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_invalid_size_leaves_no_file_and_skips_download(self):
        self.metadata["size"] = True
        result = self.download()
        self.assertEqual(decoded(result)["error"]["code"], "invalid_file_metadata")
        self.assertFalse(any(call[0] == "DOWNLOAD" for call in self.client.calls))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_no_config_and_relative_directory_are_rejected(self):
        for value, expected in ((None, "download_disabled"), ("relative/path", "download_configuration")):
            self.server.download_dir = value
            result = self.download()
            self.assertEqual(decoded(result)["error"]["code"], expected)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_existing_file_and_symlink_are_not_overwritten(self):
        class FixedUUID:
            hex = "a" * 32

        filename = self.root / (FixedUUID.hex + ".bin")
        sentinel = self.root / "sentinel"
        sentinel.write_bytes(b"keep")
        for symlink in (False, True):
            with self.subTest(symlink=symlink):
                if symlink:
                    filename.symlink_to(sentinel)
                else:
                    filename.write_bytes(b"keep")
                with patch("airshower.mcp.uuid.uuid4", return_value=FixedUUID()):
                    result = self.download()
                self.assertEqual(decoded(result)["error"]["code"], "download_write_failed")
                self.assertEqual(filename.read_bytes(), b"keep")
                self.assertEqual(sentinel.read_bytes(), b"keep")
                filename.unlink()

    def test_not_completed_and_nonattached_files_rejected(self):
        request = self.client.responses["/v1/requests/r1"]
        request["status"] = "ready"
        self.assertEqual(decoded(self.download())["error"]["code"], "file_unavailable")
        request["status"] = "completed"
        request["result"]["files"] = []
        self.assertEqual(decoded(self.download())["error"]["code"], "file_unavailable")
        self.assertEqual(list(self.root.iterdir()), [])


class LiveMCPBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(str(Path(self.temp.name) / "test.db"), "test-only-admin-token-long")
        self.addCleanup(self.store.close)
        self.http = create_server(self.store, port=0)
        self.addCleanup(self.http.server_close)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.http.shutdown)
        self.url = "http://127.0.0.1:{}".format(self.http.server_address[1])
        admin = Client(self.url, "test-only-admin-token-long")
        owner1 = admin.post("/v1/owners", {"name": "Sender"})
        owner2 = admin.post("/v1/owners", {"name": "Recipient"})
        self.owner1 = Client(self.url, owner1["token"])
        self.owner2 = Client(self.url, owner2["token"])
        agent1 = self.owner1.post("/v1/agents", {"name": "Sender agent"})
        self.agent2 = self.owner2.post("/v1/agents", {"name": "Recipient agent"})
        self.sender = MCPServer(Client(self.url, agent1["token"]), self.temp.name)
        self.recipient = MCPServer(Client(self.url, self.agent2["token"]))
        self.owner2.post("/v1/contacts", {"owner_id": owner1["id"]})

    def send(self, kind, source_ids=None):
        result = self.sender.call_tool("request", {
            "recipient_agent_id": self.agent2["id"], "kind": kind,
            "message": "Please help with this test request.",
            "source_ids": source_ids or [], "idempotency_key": "test-request",
        })
        self.assertFalse(result["isError"], result)
        return decoded(result)

    def test_real_http_file_handoff_through_mcp(self):
        fixture = b"permission-controlled asset"
        source = self.owner2.post("/v1/sources", {
            "label": "Test asset", "kind": "file", "filename": "asset.txt",
            "media_type": "text/plain", "data_base64": base64.b64encode(fixture).decode("ascii"),
        })
        request = self.send("file", [source["id"]])
        args = {"request_id": request["id"]}
        self.assertEqual(request["status"], "pending_approval")
        self.assertTrue(self.recipient.call_tool("read_sources", args)["isError"])
        self.owner2.post("/v1/requests/" + request["id"] + "/decision", {"decision": "approve"})
        self.assertFalse(self.recipient.call_tool("claim_request", args)["isError"])
        visible = self.recipient.call_tool("read_sources", args)
        self.assertFalse(visible["isError"], visible)
        self.assertNotIn("data_base64", decoded(visible)["sources"][0])
        completed = self.recipient.call_tool("complete_request", dict(args, text="Here it is.",
                                                                         source_ids=[source["id"]]))
        self.assertEqual(decoded(completed)["attribution"]["type"], "agent")
        downloaded = self.sender.call_tool("download_file", dict(args, source_id=source["id"]))
        self.assertFalse(downloaded["isError"], downloaded)
        self.assertEqual(Path(decoded(downloaded)["path"]).read_bytes(), fixture)

    def test_real_http_human_answer_requires_owner_api(self):
        request = self.send("ask_human")
        self.assertEqual(request["status"], "waiting_human")
        args = {"request_id": request["id"], "text": "Pretend to be the human."}
        self.assertTrue(self.recipient.call_tool("complete_request", args)["isError"])
        self.assertTrue(MCPServer(self.owner2).call_tool("complete_request", args)["isError"])
        completed = self.owner2.post("/v1/requests/" + request["id"] + "/complete", {
            "text": "A human answered through the owner interface.", "source_ids": [],
        })
        self.assertEqual(completed["attribution"]["type"], "human")
        result = self.sender.call_tool("get_request", {"request_id": request["id"]})
        self.assertEqual(decoded(result)["attribution"]["type"], "human")

    def test_multiple_large_files_return_small_metadata_then_download_separately(self):
        # Construct fixtures at runtime: no binary assets committed to the repo.
        fixture = b"x" * (4 * 1024 * 1024)
        encoded = base64.b64encode(fixture).decode("ascii")
        source_ids = []
        for index in range(2):
            source = self.owner2.post("/v1/sources", {
                "label": "Large test asset {}".format(index), "kind": "file",
                "filename": "asset-{}.bin".format(index),
                "media_type": "application/octet-stream", "data_base64": encoded,
            })
            source_ids.append(source["id"])
        request = self.send("file", source_ids)
        path = "/v1/requests/" + request["id"]
        self.owner2.post(path + "/decision", {"decision": "approve"})
        args = {"request_id": request["id"]}
        raw_sources = self.recipient.client.get(path + "/sources")
        self.assertLess(len(json.dumps(raw_sources).encode("utf-8")), 4096)
        self.assertTrue(all("data_base64" not in source for source in raw_sources["sources"]))
        visible = self.recipient.call_tool("read_sources", args)
        self.assertFalse(visible["isError"], visible)
        self.assertEqual({source["id"] for source in decoded(visible)["sources"]}, set(source_ids))
        completed = self.recipient.call_tool("complete_request", dict(args, text="Two assets.",
                                                                         source_ids=source_ids))
        self.assertFalse(completed["isError"], completed)
        for source_id in source_ids:
            downloaded = self.sender.call_tool("download_file", dict(args, source_id=source_id))
            self.assertFalse(downloaded["isError"], downloaded)
            self.assertEqual(Path(decoded(downloaded)["path"]).read_bytes(), fixture)


if __name__ == "__main__":
    unittest.main()
