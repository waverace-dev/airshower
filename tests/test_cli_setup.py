"""CLI wiring smoke tests, using isolated profiles and real child processes.

These verify operator commands, not desktop installation or model delivery.
"""

import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import urlsplit

from airshower.client import Client


ROOT = Path(__file__).resolve().parents[1]


class CLISetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="airshower-cli-test-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.profile = self.directory / "operator"
        # Ignore installed user configuration, including inherited relay tokens.
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("AIRSHOWER_")}
        self.env["PYTHONPATH"] = str(ROOT)

    def run_cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, "-m", "airshower", *map(str, args)],
                                cwd=ROOT, env=self.env, capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def ready(self):
        return self.run_cli("ready", "--state-dir", self.profile,
                            "--name", "Avery Test", "--agent", "Desktop test agent")

    def secrets(self):
        values = [(self.profile / "admin.token").read_text().strip()]
        values.extend(json.loads((self.profile / (role + ".json")).read_text())["token"]
                      for role in ("owner", "agent"))
        return values

    def assert_no_secrets(self, text, extra=()):
        for value in [*self.secrets(), *extra]:
            self.assertNotIn(value, text)

    def start_server(self):
        process = subprocess.Popen(
            [sys.executable, "-m", "airshower", "serve", "--profile", str(self.profile),
             "--host", "127.0.0.1", "--port", "0", "--public-url", "https://relay.example"],
            cwd=ROOT, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )

        def stop():
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)

        self.addCleanup(stop)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(timeout=10), "Relay did not report its listen address")
        line = process.stdout.readline()
        match = re.fullmatch(r"Airshower experimental reference server listening on 127\.0\.0\.1:(\d+)\n", line)
        self.assertIsNotNone(match, line)
        self.assert_no_secrets(line)
        # ready uses the conventional local port. Bind to an ephemeral port for
        # tests and point only this temporary fixture at the actual listener.
        origin = "http://127.0.0.1:" + match.group(1)
        filename = self.profile / "profile.json"
        profile = json.loads(filename.read_text())
        profile["relay"] = origin
        filename.write_text(json.dumps(profile))
        return origin

    def test_ready_cli_is_idempotent_and_only_reports_preparation(self):
        first = self.ready()
        identities = {name: (self.profile / name).read_bytes()
                      for name in ("admin.token", "owner.json", "agent.json", "profile.json")}
        second = self.ready()
        for name, content in identities.items():
            self.assertEqual((self.profile / name).read_bytes(), content)
        initial, repeated = json.loads(first.stdout), json.loads(second.stdout)
        self.assertTrue(initial["created"])
        self.assertFalse(repeated["created"])
        self.assertEqual(initial["profile"]["owner_name"], "Avery Test")
        self.assertEqual(initial["profile"]["agent_name"], "Desktop test agent")
        for key in ("relay_started", "host_configured", "host_delivery_verified", "peer_roundtrip_verified"):
            self.assertIs(initial[key], False)
        self.assert_no_secrets(first.stdout + first.stderr + second.stdout + second.stderr)

    def test_live_serve_invite_and_doctor_commands_keep_credentials_private(self):
        self.ready()
        origin = self.start_server()
        diagnostic = self.run_cli("doctor", "--profile", self.profile)
        result = json.loads(diagnostic.stdout)
        self.assertEqual(result["status"], "relay_connected")
        self.assertEqual(result["stages"]["agent_identity"], "verified")
        self.assertIs(result["fully_ready"], False)
        self.assertEqual(result["stages"]["host_delivery"], "not_verified")
        self.assertEqual(result["stages"]["peer_roundtrip"], "not_verified")

        destination = self.directory / "private-invitation.json"
        invitation_result = self.run_cli(
            "invite", "--profile", self.profile, "--public-url", "https://relay.example",
            "--expires-in", "60", "--output", destination)
        invitation = json.loads(destination.read_text())
        self.assertEqual(invitation["relay"], "https://relay.example")
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        self.assertEqual(json.loads(invitation_result.stdout)["invitation_file"], str(destination))
        self.assert_no_secrets(diagnostic.stdout + diagnostic.stderr + invitation_result.stdout + invitation_result.stderr,
                               (invitation["token"], invitation["contact_invite"]))
        before = destination.read_bytes()
        refused = self.run_cli("invite", "--profile", self.profile,
                               "--output", destination, expected=1)
        self.assertEqual(destination.read_bytes(), before)
        self.assert_no_secrets(refused.stdout + refused.stderr,
                               (invitation["token"], invitation["contact_invite"]))

        # The emitted invitation really enrolls on the running relay. Browser
        # routes also prove that serve passed its configured public origin on.
        body = {"name": "Recipient", "agent_name": "Claude Desktop test"}
        if callable(getattr(hashlib, "scrypt", None)):
            body["consent_secret"] = "test-only approval password"
        enrolled = Client(origin, invitation["token"]).post("/v1/enroll", body)
        owner = Client(origin, enrolled["owner"]["token"])
        if "consent_secret" in body:
            consent = owner.post("/v1/consents", {"contact_invite": invitation["contact_invite"]})
            self.assertEqual(consent["url"], "https://relay.example/consent/" + consent["id"])
            address = urlsplit(origin)
            connection = http.client.HTTPConnection(address.hostname, address.port, timeout=5)
            try:
                connection.request("GET", urlsplit(consent["url"]).path,
                                   headers={"Host": "relay.example"})
                response = connection.getresponse()
                page = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertRegex(page, r"type=['\"]password['\"]")
                self.assertNotIn(invitation["contact_invite"], page)
            finally:
                connection.close()
        else:
            # Basic operator tooling remains usable on runtimes without scrypt.
            self.assertEqual(owner.get("/v1/me")["id"], enrolled["owner"]["id"])

    def test_doctor_missing_profile_and_invalid_public_origin_fail_honestly(self):
        missing = self.run_cli("doctor", "--profile", self.profile)
        result = json.loads(missing.stdout)
        self.assertEqual(result["status"], "incomplete")
        self.assertIs(result["fully_ready"], False)
        self.assertFalse(self.profile.exists())
        self.ready()
        refused = self.run_cli("serve", "--profile", self.profile, "--port", "0",
                               "--public-url", "http://relay.example", expected=1)
        self.assertIn("HTTPS origin", refused.stderr)
        self.assertNotIn("listening", refused.stdout)
        self.assert_no_secrets(refused.stdout + refused.stderr)


if __name__ == "__main__":
    unittest.main()
