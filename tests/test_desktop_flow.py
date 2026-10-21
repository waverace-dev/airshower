"""Real relay/desktop-adapter protocol tests; no provider, model, or desktop app.

Two independent stdio hosts simulate native elicitation replies. This validates
the bridge's wire flow, not whether any particular installed app supports it.
Set AIRSHOWER_NODE to an existing Node executable when it is not on PATH.
"""

import hashlib
from html import unescape
from html.parser import HTMLParser
import http.client
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import queue
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.parse import urlencode, urlsplit

from airshower.consent import ConsentBroker
from airshower.server import create_server
from airshower.store import Store


ROOT = Path(__file__).resolve().parents[1]
NODE = os.environ.get("AIRSHOWER_NODE") or shutil.which("node")


class DesktopHost:
    """A minimal MCP host that handles server-initiated requests while waiting."""

    def __init__(self, command, environment):
        self.process = subprocess.Popen(
            command, cwd=str(ROOT), env=environment, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", bufsize=1,
        )
        self.messages = queue.Queue()
        self.transcript = []
        self.diagnostics = []
        self.sequence = 0
        self.forms = []
        self.notifications = []
        self.closed = False
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.error_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self.reader.start()
        self.error_reader.start()

    def _read_stdout(self):
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except (ValueError, UnicodeError):
                self.messages.put(RuntimeError("Desktop adapter emitted non-JSON stdout"))
                continue
            self.transcript.append(message)
            self.messages.put(message)
        self.messages.put(EOFError("Desktop adapter closed stdout"))

    def _read_stderr(self):
        for line in self.process.stderr:
            self.diagnostics.append(line)

    def send(self, message):
        self.process.stdin.write(json.dumps(message, allow_nan=False) + "\n")
        self.process.stdin.flush()

    def rpc(self, method, parameters=None, form=None, timeout=10):
        self.sequence += 1
        identifier = self.sequence
        message = {"jsonrpc": "2.0", "id": identifier, "method": method}
        if parameters is not None:
            message["params"] = parameters
        self.send(message)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("Desktop adapter did not answer " + method)
            try:
                response = self.messages.get(timeout=remaining)
            except queue.Empty:
                raise AssertionError("Desktop adapter did not answer " + method) from None
            if isinstance(response, Exception):
                raise response
            if response.get("method") == "elicitation/create":
                self.forms.append(response)
                if form is None:
                    self.send({"jsonrpc": "2.0", "id": response["id"], "result": {"action": "cancel"}})
                    raise AssertionError("Unexpected native form for " + method)
                answer = form(response["params"])
                after_reply = None
                if isinstance(answer, tuple):
                    answer, after_reply = answer
                self.send({"jsonrpc": "2.0", "id": response["id"], "result": answer})
                if after_reply is not None:
                    after_reply()
            elif "method" in response and "id" not in response:
                self.notifications.append(response)
            elif response.get("id") == identifier:
                return response
            else:
                raise AssertionError("Unexpected desktop-adapter JSON-RPC response")

    def initialize(self, forms=True, urls=False):
        elicitation = {}
        if forms:
            elicitation["form"] = {}
        if urls:
            elicitation["url"] = {}
        result = self.rpc("initialize", {
            "protocolVersion": "2025-11-25", "capabilities": {"elicitation": elicitation} if elicitation else {},
            "clientInfo": {"name": "Airshower protocol test host", "version": "1"},
        })
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    def tool(self, name, arguments=None, form=None):
        return self.rpc("tools/call", {"name": name, "arguments": arguments or {}}, form=form)

    def close(self):
        if self.closed:
            return
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
        if not self.process.stdin.closed:
            self.process.stdin.close()
        self.reader.join(timeout=3)
        self.error_reader.join(timeout=3)
        for stream in (self.process.stdout, self.process.stderr):
            stream.close()
        self.closed = True
        if self.reader.is_alive() or self.error_reader.is_alive():
            raise AssertionError("Desktop adapter output readers did not finish")


@unittest.skipUnless(NODE and Path(NODE).is_file() and os.access(NODE, os.X_OK),
                     "Node is unavailable; set AIRSHOWER_NODE to an existing Node executable")
class DesktopFlowTests(unittest.TestCase):
    ADMIN = "test-only-desktop-protocol-relay-admin"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name).resolve()
        self.cert = self.directory / "test-only-cert.pem"
        self.key = self.directory / "test-only-key.pem"
        self.cert.write_text(TEST_CERTIFICATE, encoding="ascii")
        self.key.write_text(TEST_PRIVATE_KEY, encoding="ascii")
        self.key.chmod(0o600)
        self.store = Store(self.directory / "relay.sqlite3", self.ADMIN)
        self.server = create_server(self.store)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, self.key)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.base = "https://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.hosts = []
        self.secrets = {self.ADMIN}

    def tearDown(self):
        for host in self.hosts:
            host.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.store.close()
        self.temp.cleanup()

    def api(self, method, path, token=None, payload=None, status=200):
        headers = {}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPSConnection(
            *self.server.server_address, context=ssl.create_default_context(cafile=str(self.cert)), timeout=5,
        )
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            value = json.loads(response.read())
            self.assertEqual(response.status, status, "Unexpected relay HTTP status")
            return value
        finally:
            connection.close()

    def enrollment(self):
        result = self.api("POST", "/v1/enrollments", self.ADMIN, {}, 201)
        self.secrets.add(result["token"])
        return result["token"]

    def value(self, message, error=False):
        self.assertNotIn("error", message, "Unexpected JSON-RPC protocol error")
        result = message["result"]
        self.assertEqual(bool(result.get("isError")), error, "Unexpected MCP tool success/error status")
        return json.loads(result["content"][0]["text"])

    def assert_private_outputs(self):
        for host in self.hosts:
            # Drain shutdown output too: stdout completion alone does not
            # synchronize the independent stderr reader thread.
            host.close()
            output = json.dumps(host.transcript) + "".join(host.diagnostics)
            for token in self.secrets:
                self.assertTrue(token not in output, "A private credential appeared in adapter output")

    def host(self, profile, name, invitation=None, forms=True, client_name="Claude Desktop", urls=False, approval_password=None):
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("AIRSHOWER_") and key not in ("NODE_EXTRA_CA_CERTS", "NODE_OPTIONS")}
        environment.update({
            "AIRSHOWER_STATE_DIR": str(profile), "AIRSHOWER_NAME": name,
            "AIRSHOWER_CLIENT_NAME": client_name, "NODE_EXTRA_CA_CERTS": str(self.cert),
        })
        if invitation is not None:
            environment["AIRSHOWER_INVITATION"] = json.dumps(invitation)
        if approval_password is not None:
            environment["AIRSHOWER_APPROVAL_PASSWORD"] = approval_password
            self.secrets.add(approval_password)
        host = DesktopHost([NODE, str(ROOT / "integrations" / "desktop" / "server.mjs")], environment)
        self.hosts.append(host)
        initialized = host.initialize(forms, urls)
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-11-25")
        tools = {item["name"] for item in host.rpc("tools/list")["result"]["tools"]}
        self.assertEqual("review_request" in tools, forms or urls)
        self.assertEqual("connect_contact" in tools, forms or urls)
        return host

    def provision(self, name, contact=None, forms=True, client_name="Claude Desktop", urls=False, approval_password=None):
        profile = self.directory / name.lower()
        invitation = {"relay": self.base, "token": self.enrollment()}
        if contact is not None:
            invitation["contact_invite"] = contact
        host = self.host(profile, name, invitation, forms, client_name, urls, approval_password)
        ready = self.value(host.tool("ready"))
        self.assertEqual(ready["status"], "relay_connected")
        self.assertEqual(ready["native_human_review"], forms)
        self.assertEqual(ready["browser_review_host_supported"], urls)
        self.assertFalse(ready["autonomous_chat_wakeup"])
        self.assertFalse(ready["peer_roundtrip_verified"])
        owner = json.loads((profile / "owner.json").read_text())
        agent = json.loads((profile / "agent.json").read_text())
        for credential in (owner, agent):
            self.secrets.add(credential["token"])
        self.assertEqual(ready["agent_id"], agent["id"])
        self.assertEqual(ready["owner_id"], owner["id"])
        # An already-configured host does not attempt another enrollment.
        self.assertEqual(self.value(host.tool("ready"))["agent_id"], agent["id"])
        self.api("POST", "/v1/enroll", invitation["token"], {"name": "Replay", "agent_name": "Replay"}, 401)
        self.assertEqual(profile.stat().st_mode & 0o777, 0o700)
        for filename in ("profile.json", "owner.json", "agent.json"):
            self.assertEqual((profile / filename).stat().st_mode & 0o777, 0o600)
        return host, profile, owner, agent

    def paired(self, bob_forms=True):
        alice = self.provision("Alice", client_name="Codex")
        invitation = self.api("POST", "/v1/contact-invites", alice[2]["token"], {}, 201)
        self.secrets.add(invitation["token"])
        bob = self.provision("Bob", contact=invitation["token"], forms=bob_forms)
        return alice, bob

    def decision(self, choice, request=None):
        def answer(parameters):
            self.assertEqual(parameters["mode"], "form")
            schema = parameters["requestedSchema"]
            self.assertEqual(set(schema["properties"]), {"decision"})
            self.assertIn(choice, schema["properties"]["decision"]["enum"])
            self.assertFalse(schema["additionalProperties"])
            if request is not None:
                self.assertIn(request["id"], parameters["message"])
                self.assertIn(request["message"], parameters["message"])
                self.assertIn(request["sender_agent_id"], parameters["message"])
            return {"action": "accept", "content": {"decision": choice}}
        return answer

    def request(self, host, recipient, key, kind="task"):
        return self.value(host.tool("request", {
            "recipient_agent_id": recipient["id"], "kind": kind,
            "message": "Coordinate a meeting next week, subject to the owner's approval.",
            "source_ids": [], "idempotency_key": key,
        }))

    def browser(self, method, path, form=None, cookie=None, status=200):
        headers = {}
        body = None
        if form is not None:
            headers.update({"Origin": self.base, "Content-Type": "application/x-www-form-urlencoded"})
            body = urlencode(form).encode("ascii")
        if cookie is not None:
            headers["Cookie"] = cookie
        connection = http.client.HTTPSConnection(
            *self.server.server_address, context=ssl.create_default_context(cafile=str(self.cert)), timeout=5,
        )
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            document = response.read().decode("utf-8")
            self.assertEqual(response.status, status, "Unexpected consent-page HTTP status")
            return document, dict(response.getheaders())
        finally:
            connection.close()

    def csrf(self, document):
        class Inputs(HTMLParser):
            def __init__(self):
                super().__init__()
                self.values = {}

            def handle_starttag(self, tag, attributes):
                attributes = dict(attributes)
                if tag == "input" and "name" in attributes:
                    self.values[attributes["name"]] = attributes.get("value", "")

        parser = Inputs()
        parser.feed(document)
        self.assertIn("_csrf", parser.values)
        self.assertTrue(parser.values["_csrf"])
        return parser.values["_csrf"]

    def browser_decision(self, host, owner, agent, password, action, unchanged, request=None, answer=None):
        def open_review(parameters):
            self.assertEqual(parameters["mode"], "url")
            self.assertNotIn("requestedSchema", parameters)
            url = urlsplit(parameters["url"])
            origin = urlsplit(self.base)
            self.assertEqual((url.scheme, url.netloc), (origin.scheme, origin.netloc))
            self.assertFalse(url.query or url.fragment)
            self.assertRegex(url.path, r"^/consent/[a-f0-9-]{36}$")
            identifier = url.path.rsplit("/", 1)[1]
            for secret in self.secrets:
                self.assertTrue(secret not in parameters["url"], "Consent URL contained an authentication credential")

            def submit_in_browser():
                # The host accepted the URL request before this callback. A
                # ping synchronizes with the child without submitting consent.
                self.assertEqual(host.rpc("ping")["result"], {})
                unchanged()
                status = self.api("GET", "/v1/consents/" + identifier, owner["token"])
                self.assertEqual(status["status"], "pending")
                self.api("GET", "/v1/consents/" + identifier, agent["token"], status=403)
                document, headers = self.browser("GET", url.path)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["Referrer-Policy"], "no-referrer")
                for private in ("Alice", "Bob", owner["id"], agent["id"]):
                    self.assertNotIn(private, document)
                if request is not None:
                    self.assertNotIn(request["message"], document)
                    self.assertNotIn(request["sender_agent_id"], document)
                csrf = self.csrf(document)
                # The public URL and its form token are insufficient without
                # the separate enrollment password and authenticated cookie.
                self.browser("POST", url.path, {"_csrf": csrf, "action": action}, status=403)
                document, headers = self.browser("POST", url.path, {
                    "_csrf": csrf, "action": "login", "password": password,
                })
                self.assertNotIn(password, document)
                cookies = SimpleCookie()
                cookies.load(headers["Set-Cookie"])
                session = cookies["airshower_consent"]
                self.assertTrue(session["secure"])
                self.assertTrue(session["httponly"])
                self.assertEqual(session["samesite"], "Strict")
                self.assertEqual(session["path"], url.path)
                self.secrets.add(session.value)
                cookie = "airshower_consent=" + session.value
                next_csrf = self.csrf(document)
                self.assertNotEqual(next_csrf, csrf)
                self.assertIn("Alice", document)
                if request is not None:
                    self.assertIn(request["message"], unescape(document))
                    self.assertIn("Codex", document)
                # Signing in also cannot authorize the pending action.
                unchanged()
                status = self.api("GET", "/v1/consents/" + identifier, owner["token"])
                self.assertEqual(status["status"], "pending")
                form = {"_csrf": next_csrf, "action": action}
                if answer is not None:
                    form["answer"] = answer
                document, _ = self.browser("POST", url.path, form, cookie)
                self.assertIn("Response recorded", document)
                self.browser("POST", url.path, form, cookie, status=410)

            # MCP's accept means only that the host opened the URL. The actual
            # decision travels from the browser form to the broker's owner API.
            return {"action": "accept"}, submit_in_browser
        return open_review

    @unittest.skipUnless(callable(getattr(hashlib, "scrypt", None)), "URL consent requires Python hashlib.scrypt")
    def test_url_only_host_pairs_and_reviews_through_authenticated_browser(self):
        self.server.consent = ConsentBroker(self.store, self.base)
        alice = self.provision("Alice", client_name="Codex")
        invitation = self.api("POST", "/v1/contact-invites", alice[2]["token"], {}, 201)
        self.secrets.add(invitation["token"])
        password = "test-only Bob browser approval password"
        bob = self.provision("Bob", invitation["token"], forms=False, urls=True, approval_password=password)
        sender, recipient = alice[0], bob[0]

        def disconnected():
            self.assertEqual(self.api("GET", "/v1/contacts", bob[2]["token"])["contacts"], [])

        connected = self.value(recipient.tool("connect_contact", form=self.browser_decision(
            recipient, bob[2], bob[3], password, "connect", disconnected,
        )))
        self.assertTrue(connected["connected"])
        self.assertEqual(connected["owner_id"], alice[2]["id"])

        for action in ("deny", "approve", "answer"):
            request = self.request(sender, bob[3], "url-" + action, kind="ask_human" if action == "answer" else "task")
            expected = "waiting_human" if action == "answer" else "pending_approval"
            answer = "I can meet Thursday at 18:30." if action == "answer" else None

            def unchanged():
                current = self.api("GET", "/v1/requests/" + request["id"], alice[3]["token"])
                self.assertEqual(current["status"], expected)

            reviewed = self.value(recipient.tool("review_request", {"request_id": request["id"]}, self.browser_decision(
                recipient, bob[2], bob[3], password, action, unchanged, request, answer,
            )))
            if action == "deny":
                self.assertEqual(reviewed["status"], "denied")
            elif action == "approve":
                self.assertEqual(reviewed["status"], "ready")
                self.value(recipient.tool("claim_request", {"request_id": request["id"]}))
                self.value(recipient.tool("complete_request", {
                    "request_id": request["id"], "text": "The owner approved this coordination request.",
                }))
            else:
                self.assertEqual(reviewed["status"], "completed")
            received = self.value(sender.tool("get_request", {"request_id": request["id"]}))
            if action == "deny":
                self.assertEqual(received["status"], "denied")
            else:
                self.assertEqual(received["status"], "completed")
                self.assertEqual(received["attribution"]["type"], "human" if action == "answer" else "agent")
                if answer is not None:
                    self.assertEqual(received["result"]["text"], answer)
        self.assertEqual(len(recipient.forms), 4)
        self.assertTrue(all(item["params"]["mode"] == "url" for item in recipient.forms))
        self.assertTrue(any(item["method"] == "notifications/elicitation/complete" for item in recipient.notifications))
        self.assert_private_outputs()

    def test_url_host_without_enrolled_password_cannot_authorize_contact(self):
        self.server.consent = ConsentBroker(self.store, self.base)
        alice = self.provision("Alice")
        invitation = self.api("POST", "/v1/contact-invites", alice[2]["token"], {}, 201)
        self.secrets.add(invitation["token"])
        bob = self.provision("Bob", invitation["token"], forms=False, urls=True)
        self.value(bob[0].tool("connect_contact"), error=True)
        self.assertEqual(bob[0].forms, [])
        self.assertEqual(self.api("GET", "/v1/contacts", bob[2]["token"])["contacts"], [])
        self.assert_private_outputs()

    def test_two_profiles_enroll_connect_review_and_return_agent_result(self):
        alice, bob = self.paired()
        sender, _, sender_owner, sender_agent = alice
        recipient, _, recipient_owner, recipient_agent = bob
        self.assertNotEqual(alice[1], bob[1])
        self.assertEqual(sender_agent["name"], "Codex")
        self.assertEqual(recipient_agent["name"], "Claude Desktop")

        # Enrollment alone does not connect anybody; a native decision does.
        self.assertEqual(self.api("GET", "/v1/contacts", sender_owner["token"])["contacts"], [])
        declined = self.value(recipient.tool("connect_contact", form=self.decision("decline")))
        self.assertFalse(declined["connected"])
        self.assertEqual(self.api("GET", "/v1/contacts", recipient_owner["token"])["contacts"], [])
        connected = self.value(recipient.tool("connect_contact", form=self.decision("connect")))
        self.assertEqual(connected, {"owner_id": sender_owner["id"], "connected": True})
        for host, peer in ((sender, recipient_agent), (recipient, sender_agent)):
            self.assertIn(peer["id"], [item["id"] for item in self.value(host.tool("peers"))["agents"]])

        denied = self.request(sender, recipient_agent, "desktop-denied")
        self.assertEqual(denied["status"], "pending_approval")
        visible = self.value(recipient.tool("wait_for_request", {"timeout_seconds": 0}))
        self.assertEqual(visible["status"], "available")
        self.assertIn(denied["id"], [item["id"] for item in visible["requests"]])
        self.assertNotIn("message", visible["requests"][0])
        # Agent credentials and model-supplied decisions cannot impersonate the owner.
        self.api("POST", "/v1/requests/%s/decision" % denied["id"], recipient_agent["token"], {"decision": "approve"}, 403)
        count = len(recipient.forms)
        self.value(recipient.tool("review_request", {"request_id": denied["id"], "decision": "approve"}), error=True)
        self.assertEqual(len(recipient.forms), count)
        self.assertEqual(self.value(recipient.tool("review_request", {"request_id": denied["id"]},
                                                   self.decision("deny", denied)))["status"], "denied")
        self.assertEqual(self.value(sender.tool("get_request", {"request_id": denied["id"]}))["status"], "denied")
        self.value(recipient.tool("complete_request", {"request_id": denied["id"], "text": "Should fail"}), error=True)

        approved = self.request(sender, recipient_agent, "desktop-approved")
        self.assertEqual(approved["status"], "pending_approval")
        self.assertEqual(self.value(recipient.tool("review_request", {"request_id": approved["id"]},
                                                   self.decision("approve", approved)))["status"], "ready")
        self.assertEqual(self.value(recipient.tool("claim_request", {"request_id": approved["id"]}))["status"], "working")
        answer = "Confirmed with the owner: Thursday at 18:30."
        completed = self.value(recipient.tool("complete_request", {"request_id": approved["id"], "text": answer}))
        self.assertEqual(completed["status"], "completed")
        waiting = self.value(sender.tool("wait_for_request", {"request_id": approved["id"], "timeout_seconds": 0}))
        self.assertEqual(waiting["status"], "available")
        received = self.value(sender.tool("get_request", {"request_id": approved["id"]}))
        self.assertEqual(received["result"]["text"], answer)
        self.assertEqual(received["attribution"]["type"], "agent")
        # Pairing and one approval never become a standing permission.
        self.assertEqual(self.request(sender, recipient_agent, "desktop-next")["status"], "pending_approval")
        self.assert_private_outputs()

    def test_human_answer_and_dismissal_stay_distinct_from_agent_completion(self):
        alice, bob = self.paired()
        sender, recipient = alice[0], bob[0]
        self.value(recipient.tool("connect_contact", form=self.decision("connect")))
        request = self.request(sender, bob[3], "desktop-human", kind="ask_human")
        self.assertEqual(request["status"], "waiting_human")
        self.value(recipient.tool("complete_request", {"request_id": request["id"], "text": "Pretend human"}), error=True)
        dismissed = self.value(recipient.tool("review_request", {"request_id": request["id"]}, lambda _: {"action": "cancel"}))
        self.assertEqual(dismissed["status"], "unchanged")
        self.assertEqual(self.value(sender.tool("get_request", {"request_id": request["id"]}))["status"], "waiting_human")

        def answer(parameters):
            self.assertIn(request["id"], parameters["message"])
            self.assertEqual(set(parameters["requestedSchema"]["properties"]), {"text"})
            return {"action": "accept", "content": {"text": "No, I am unavailable next week."}}

        self.value(recipient.tool("review_request", {"request_id": request["id"]}, answer))
        result = self.value(sender.tool("get_request", {"request_id": request["id"]}))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attribution"]["type"], "human")
        self.assertEqual(result["result"]["text"], "No, I am unavailable next week.")
        self.assert_private_outputs()

    def test_host_without_native_forms_cannot_connect_or_review(self):
        alice, bob = self.paired(bob_forms=False)
        recipient = bob[0]
        self.value(recipient.tool("connect_contact"), error=True)
        self.value(recipient.tool("review_request", {"request_id": "arbitrary-request"}), error=True)
        self.assertEqual(recipient.forms, [])
        self.assertEqual(self.api("GET", "/v1/contacts", alice[2]["token"])["contacts"], [])
        self.assertEqual(self.api("GET", "/v1/contacts", bob[2]["token"])["contacts"], [])
        # Even with a contact accepted separately through the owner's API,
        # this host cannot bypass its missing human-review capability.
        invite = json.loads((bob[1] / "contact-invite.json").read_text())
        self.api("POST", "/v1/contact-invites/accept", bob[2]["token"], {"token": invite["token"]})
        request = self.request(alice[0], bob[3], "no-native-review")
        self.value(recipient.tool("review_request", {"request_id": request["id"]}), error=True)
        self.value(recipient.tool("claim_request", {"request_id": request["id"]}), error=True)
        self.assertEqual(self.value(alice[0].tool("get_request", {"request_id": request["id"]}))["status"], "pending_approval")
        self.assertEqual(recipient.forms, [])
        self.assert_private_outputs()

    def test_owner_credential_substituted_as_agent_is_rejected(self):
        host, profile, owner, agent = self.provision("Alice")
        agent["token"] = owner["token"]
        (profile / "agent.json").write_text(json.dumps(agent), encoding="utf-8")
        self.value(host.tool("peers"), error=True)
        self.value(host.tool("request", {"recipient_agent_id": "other-agent", "kind": "message",
                                         "message": "Must not run", "idempotency_key": "wrong-role"}), error=True)
        # A distinct admin token passes the local token-inequality check, but
        # the actual relay identity must still be rejected by the adapter.
        agent["token"] = self.ADMIN
        (profile / "agent.json").write_text(json.dumps(agent), encoding="utf-8")
        result = self.value(host.tool("peers"), error=True)
        self.assertEqual(result["error"]["code"], "agent_identity_error")
        self.assertEqual(self.api("GET", "/v1/requests", owner["token"])["requests"], [])
        self.assert_private_outputs()


# Public test-only TLS fixture, never used for a deployed relay.
TEST_CERTIFICATE = '-----BEGIN CERTIFICATE-----\nMIICyzCCAbOgAwIBAgIJAJo8a5xSRC+wMA0GCSqGSIb3DQEBCwUAMBQxEjAQBgNV\nBAMMCWxvY2FsaG9zdDAgFw0yNjEwMjExNTM3NDVaGA8yMTI2MDkyNzE1Mzc0NVow\nFDESMBAGA1UEAwwJbG9jYWxob3N0MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIB\nCgKCAQEAtL2ny6rioNNpn+BLvpo8Tg+6JRUeseUZerNYqakyz/9Bf40+CPnS53SH\nhgBgwLf8MLuc0oRABhGlY5aE0+ZBAYx/RI+buoi8tUFKdKusvl56FNlgcjuyFWFi\nD1fgJVqJ1HFPK3KhSW5yy9assxB0mhbgq/7Ge+BnyoLbKzhDjGFNebmxbw7DpJe9\nlG7m8MQcgbzv7HlPh3YcECeZrngA2oj3TMjv4oGdLYsxd+nC2Yg+J6rH9c/VgsYI\nOfvnFAunMSXIDARNA1dFI5TIE0loNcOCbwyYrcyVoXEddoLGZ1YCkuaFg32bMFe6\nv1bopRgHorsdkjNHpFOlzhn2PAZoFwIDAQABox4wHDAaBgNVHREEEzARgglsb2Nh\nbGhvc3SHBH8AAAEwDQYJKoZIhvcNAQELBQADggEBABWb3T4LK7SQJfRF8pjrhBQ3\n2wyVE2VDxFHx8vzOA01xUXm97esSqtFxcYkcbxy7eI/zvU0xFVZXVCRsWsjc2FPP\nT1ghaojek9Q0WtSoaVy/Kn1HEv5tB/NTq1KkU3bmwQM5kZEoLPNAXlzAqS0pQEhL\naA1zXE55flJSjdN+U1tXOXvyNBfIENzTLIxDb+aPKGmkRzpkr0Pc9p2mtLOKo7Ce\nuC+0T4Y+16nP5uLZ/+pDCxKnkiv0lOGA7HDsUf+tbtXYksV1RrZ/Ryw4qJPDyQxq\ntDn531U+608Kfa7umShvf0EwFX+c2yXg7pQnk+wt/wVulZrcrbIprSQLDjZhdJQ=\n-----END CERTIFICATE-----\n'
TEST_PRIVATE_KEY = '-----BEGIN PRIVATE KEY-----\nMIIEvwIBADANBgkqhkiG9w0BAQEFAASCBKkwggSlAgEAAoIBAQC0vafLquKg02mf\n4Eu+mjxOD7olFR6x5Rl6s1ipqTLP/0F/jT4I+dLndIeGAGDAt/wwu5zShEAGEaVj\nloTT5kEBjH9Ej5u6iLy1QUp0q6y+XnoU2WByO7IVYWIPV+AlWonUcU8rcqFJbnLL\n1qyzEHSaFuCr/sZ74GfKgtsrOEOMYU15ubFvDsOkl72UbubwxByBvO/seU+HdhwQ\nJ5mueADaiPdMyO/igZ0tizF36cLZiD4nqsf1z9WCxgg5++cUC6cxJcgMBE0DV0Uj\nlMgTSWg1w4JvDJitzJWhcR12gsZnVgKS5oWDfZswV7q/VuilGAeiux2SM0ekU6XO\nGfY8BmgXAgMBAAECggEBAI/yGEgx0gqqNY46Ebk8l1jIV8Ue2/Jsdu1tFukQs0JK\nlfyjHcwCQ8FI4oKLnIMARR7+3Lgg0ejDdwgooaMt14oHPnj5MtrN+hUVadofvBVl\noy8lrRVti7hRdcqofwDg6bLDh30f72pPR6gmUibOrT7nh4oUADo6kA8ORsVWKx4K\nLgtYtefeTAh0KcY+qg8nirAtgBs2CEJfSj2M6df0ZCVyCzQkzvpO32pACo/rCGnS\neSxWJxxsE6udbnl9sCyczu15gjRBuOwJs+/KqVUGIXOjqcm6Sn8JGBpkGfUxkOOH\nLLbUXKPbcOy+WMdtttw+7rIX9cbUMj7//R9EKpD/C+ECgYEA12Cm/AGrx8Du3qMR\nVRMQMNPVdYfF7op7Cu115c/3IEDFRa8zZr9LM0mb1XypXu1/Aqk35wOUe5TF6jS6\nXs+pELx2gZpfv7mF3dpkwzrr/m/sMOrYsZYsREwTdFupSOT9BtMWJ5oZ1DtGM7+h\nYUjUoUHQSpU9k+aAxi6vSR+0uQcCgYEA1tSYVyqN70uYBu3A5eEWyP1nP6bAW/f+\nsEELKD+G2InDsMzrTudWPxOnxDxRrX40veuUzp6XYeB1R6lj9HuMVXAbQN0Qxx2i\nLmijbPBXdODOdV4kWo/JwbeOf+x34vtSQgTHQGsyXRG0oUtCKMsa8kUVuyH9go+v\nKDIyoZa2ZHECgYEAhpKgo7OTluNpHXk8WjDoG+owZRhpxyuFyAUpdmekHOX7nRfk\ngR2jqi8jDJiw51xz0MDU6iZlf3Ys17MMhDMyiv+G+GHx08Ti7QUJkqbdUAS0yrfi\nn8uaay4xtbGUK7TQmJQKMsJpwx8qNSlWd4ffO8b4k7F+YUxUSABhi8ogzL8CgYEA\n1WDX50wnKhVFbSxdOnb1sLCxCrHPwglyeRJByLpAb3d2XXdMCVURfxlAu1LGNbJz\nJGiHpyoyZk8Rbho7Y6wLWRk+mJB6h6tRWRMpr5yQ4VAhAMZ/CUxkMvrkmP8AnPzP\nWS9x0PQoU/SwGryre+UwYI5p5TL/n9yjNyOU0pbllqECgYBYAud4EiZeLcHTxDOV\noXJUsIpMmzMYKAnyQ2x4pKhrqprnSfZKc/F9XdTLYSCgvgwt44PXRaHs4PoVS+Me\nreDG2M3BsVxv5QvQqRfjyGefHlG0FaXQt3pLUTs3CSAV+iC3VrNhbwjr7/jCPCps\nD/q0Tpk4x0QtHjkeULwRLH3Akg==\n-----END PRIVATE KEY-----\n'
