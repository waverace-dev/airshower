"""Private setup, crash recovery, and truthful-readiness checks using stdlib."""

import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from airshower import setup
from airshower.client import APIError, TransportError
from airshower.server import create_server
from airshower.store import Store


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="airshower-setup-test-")
        # macOS /var is a system symlink. Pass the canonical temporary path to
        # exercise the same no-symlink policy used for user profile paths.
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "profile"

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, **kwargs):
        return setup.ready(self.state, "Avery", "Codex", **kwargs)

    def read_json(self, filename):
        return json.loads((self.state / filename).read_text())

    def enrollment(self, relay="https://relay.example", token="one-use-enrollment-token"):
        path = self.root / "invitation.json"
        path.write_text(json.dumps({"relay": relay, "token": token, "contact_invite": "separate-owner-consent"}))
        path.chmod(0o600)
        return path

    def enrollment_result(self):
        owner_id = str(uuid.uuid4())
        return {"owner": {"id": owner_id, "name": "Avery", "token": "private-owner-token"},
                "agent": {"id": str(uuid.uuid4()), "owner_id": owner_id, "name": "Codex", "token": "private-agent-token"}}

    def test_local_setup_is_private_idempotent_and_has_no_secret_output(self):
        first = self.prepare()
        files = ("admin.token", "owner.json", "agent.json", "profile.json", "mcp-config.json", "claude-channel.json", "codex-config.json")
        snapshots = {filename: (self.state / filename).read_bytes() for filename in files}
        second = self.prepare()
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual({filename: (self.state / filename).read_bytes() for filename in files}, snapshots)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        for path in self.state.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)
        for role in ("owner", "agent"):
            self.assertNotIn(self.read_json(role + ".json")["token"], json.dumps(first))
        self.assertNotIn((self.state / "admin.token").read_text().strip(), json.dumps(first))
        with sqlite3.connect(str(self.state / "relay.sqlite3")) as database:
            self.assertEqual(database.execute("SELECT count(*) FROM owners").fetchone()[0], 1)
            self.assertEqual(database.execute("SELECT count(*) FROM agents").fetchone()[0], 1)
        self.assertFalse(first["relay_started"])
        self.assertFalse(first["host_configured"])
        self.assertFalse(first["peer_roundtrip_verified"])

    def test_local_setup_recovers_after_credentials_written_before_database(self):
        with patch.object(setup, "Store", side_effect=OSError("simulated power loss")):
            with self.assertRaises(OSError):
                self.prepare()
        identities = {role: self.read_json(role + ".json") for role in ("owner", "agent")}
        self.assertFalse((self.state / "profile.json").exists())
        self.prepare()
        for role in ("owner", "agent"):
            self.assertEqual(self.read_json(role + ".json"), identities[role])
        with sqlite3.connect(str(self.state / "relay.sqlite3")) as database:
            self.assertEqual(database.execute("SELECT count(*) FROM owners").fetchone()[0], 1)
            self.assertEqual(database.execute("SELECT count(*) FROM agents").fetchone()[0], 1)

    def test_existing_profiles_and_custom_exports_are_not_overwritten(self):
        self.prepare()
        host = self.root / "existing-claude-config.json"
        host.write_text('{"existing":"preserve exactly"}\n')
        export = self.state / "mcp-config.json"
        export.write_text('{"custom":"preserve exactly"}\n')
        before = host.read_bytes(), export.read_bytes(), (self.state / "owner.json").read_bytes()
        self.prepare()
        with self.assertRaises(ValueError):
            setup.ready(self.state, "Different owner", "Codex")
        self.assertEqual((host.read_bytes(), export.read_bytes(), (self.state / "owner.json").read_bytes()), before)

    def test_unsafe_directory_permissions_are_rejected_without_chmod(self):
        self.state.mkdir(mode=0o755)
        self.state.chmod(0o755)
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o755)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_symlink_directory_and_credential_are_rejected(self):
        real = self.root / "real"
        real.mkdir(mode=0o700)
        self.state.symlink_to(real, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.prepare()
        self.state.unlink()
        self.prepare()
        credential = self.state / "agent.json"
        other = self.root / "outside-agent.json"
        credential.rename(other)
        credential.symlink_to(other)
        with self.assertRaises(ValueError):
            setup.agent_client(self.state)
        self.assertEqual(setup.doctor(self.state)["status"], "incomplete")

    def test_unsafe_credential_permissions_and_hardlinks_are_rejected(self):
        self.prepare()
        credential = self.state / "agent.json"
        credential.chmod(0o644)
        with self.assertRaises(ValueError):
            setup.agent_client(self.state)
        self.assertEqual(stat.S_IMODE(credential.stat().st_mode), 0o644)
        credential.chmod(0o600)
        os.link(str(credential), str(self.root / "linked-credential"))
        with self.assertRaises(ValueError):
            setup.agent_client(self.state)

    def test_named_pipes_are_rejected_without_waiting_for_a_writer(self):
        # A thread timeout could leave an indefinitely blocked reader behind.
        # Bound each check in a child process so a regression fails promptly and
        # subprocess.run cleans up the blocked process instead of hanging CI.
        script = """
import sys
from pathlib import Path
from airshower import setup
from airshower.__main__ import _secret_file
mode, path = sys.argv[1:]
try:
    if mode == 'profile':
        setup.load_profile(path)
    elif mode == 'lock':
        setup.ready(path, 'Avery', 'Codex')
    else:
        _secret_file(path)
except ValueError as error:
    if 'regular file' not in str(error):
        raise
else:
    raise AssertionError('A named pipe was accepted as a private regular file')
"""
        for mode, filename in (("profile", "profile.json"), ("lock", "setup.lock"), ("secret", "admin.token")):
            with self.subTest(mode=mode):
                directory = self.root / mode
                directory.mkdir(mode=0o700)
                fifo = directory / filename
                os.mkfifo(str(fifo), 0o600)
                argument = fifo if mode == "secret" else directory
                completed = subprocess.run(
                    [sys.executable, "-c", script, mode, str(argument)],
                    cwd=str(Path(__file__).resolve().parent.parent),
                    capture_output=True, text=True, timeout=3, check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(completed.stdout, "")
                self.assertTrue(stat.S_ISFIFO(fifo.lstat().st_mode))
    def test_setup_lock_blocks_concurrent_provisioning_without_extra_identity(self):
        self.prepare()
        with (self.state / "setup.lock").open("r+") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "Another Airshower setup"):
                self.prepare()
        self.assertFalse(self.prepare()["created"])

    def test_mcp_exports_use_absolute_paths_and_agent_profile_without_tokens(self):
        self.prepare()
        config = self.read_json("mcp-config.json")["mcpServers"]["airshower"]
        channel = self.read_json("claude-channel.json")["mcpServers"]["airshower"]
        self.assertTrue(Path(config["command"]).is_absolute())
        self.assertEqual(config["args"], ["-m", "airshower", "mcp", "--profile", str(self.state)])
        self.assertEqual(channel["args"], config["args"] + ["--channel"])
        self.assertTrue(Path(config["env"]["PYTHONPATH"]).is_absolute())
        for role in ("owner", "agent"):
            self.assertNotIn(self.read_json(role + ".json")["token"], json.dumps(config))
        self.assertNotIn("AIRSHOWER_TOKEN", config["env"])

    def test_enrollment_sends_credentials_only_to_the_exact_https_relay(self):
        invitation = self.enrollment()
        result = self.enrollment_result()
        with patch.object(setup.Client, "post", autospec=True, return_value=result) as post:
            created = self.prepare(enrollment_file=invitation)
            again = self.prepare(relay="https://relay.example", enrollment_file=invitation)
        self.assertEqual(post.call_count, 1)
        client, endpoint, body = post.call_args.args
        self.assertEqual(client.base_url, "https://relay.example")
        self.assertEqual(endpoint, "/v1/enroll")
        self.assertEqual(body, {"name": "Avery", "agent_name": "Codex"})
        self.assertEqual(created["profile"]["mode"], "remote")
        self.assertFalse(again["created"])
        self.assertEqual(self.read_json("owner.json"), result["owner"])
        self.assertFalse((self.state / "admin.token").exists())
        self.assertFalse((self.state / "relay.sqlite3").exists())
        # Enrollment does not grant reciprocal contact access automatically.
        self.assertEqual(post.call_count, 1)

    def test_enrollment_rejects_origin_mismatch_or_cleartext_without_request(self):
        invitation = self.enrollment()
        with patch.object(setup.Client, "post") as post:
            with self.assertRaisesRegex(ValueError, "does not match"):
                self.prepare(relay="https://different.example", enrollment_file=invitation)
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                self.prepare(relay="http://127.0.0.1:7340", enrollment_file=invitation)
        post.assert_not_called()
        self.assertFalse((self.state / "enrollment-pending.json").exists())

    def test_ambiguous_enrollment_is_not_retried(self):
        invitation = self.enrollment()
        with patch.object(setup.Client, "post", side_effect=TransportError("connection lost")) as post:
            with self.assertRaises(TransportError):
                self.prepare(enrollment_file=invitation)
            with self.assertRaisesRegex(ValueError, "unknown outcome"):
                self.prepare(enrollment_file=invitation)
        self.assertEqual(post.call_count, 1)
        self.assertTrue((self.state / "enrollment-pending.json").exists())
        self.assertFalse((self.state / "profile.json").exists())

    def test_known_rejected_enrollment_allows_corrected_invitation(self):
        invitation = self.enrollment()
        with patch.object(setup.Client, "post", side_effect=APIError(401, "unauthorized", "invalid invitation")):
            with self.assertRaises(APIError):
                self.prepare(enrollment_file=invitation)
        self.assertFalse((self.state / "enrollment-pending.json").exists())
        with patch.object(setup.Client, "post", return_value=self.enrollment_result()):
            self.assertTrue(self.prepare(enrollment_file=invitation)["created"])

    def test_server_error_keeps_ambiguous_enrollment_marker(self):
        invitation = self.enrollment()
        with patch.object(setup.Client, "post", side_effect=APIError(500, "internal", "unknown outcome")) as post:
            with self.assertRaises(APIError):
                self.prepare(enrollment_file=invitation)
            with self.assertRaisesRegex(ValueError, "unknown outcome"):
                self.prepare(enrollment_file=invitation)
        self.assertEqual(post.call_count, 1)

    def test_partial_setup_cannot_switch_relay_or_mode(self):
        invitation = self.enrollment()
        with patch.object(setup.Client, "post", side_effect=TransportError("lost response")) as post:
            with self.assertRaises(TransportError):
                self.prepare(enrollment_file=invitation)
            with self.assertRaisesRegex(ValueError, "different setup settings"):
                self.prepare(relay="https://attacker.example", enrollment_file=invitation)
            with self.assertRaisesRegex(ValueError, "different setup settings"):
                self.prepare()
        self.assertEqual(post.call_count, 1)
        self.assertFalse((self.state / "admin.token").exists())
        self.assertFalse((self.state / "owner.json").exists())

    def test_local_database_symlink_is_rejected_on_profile_load(self):
        self.prepare()
        database = self.state / "relay.sqlite3"
        moved = self.root / "outside.sqlite3"
        database.rename(moved)
        database.symlink_to(moved)
        with self.assertRaises(ValueError):
            setup.load_profile(self.state)

    def test_saved_enrollment_result_recovers_without_redeeming_again(self):
        invitation = self.enrollment()
        response = self.enrollment_result()
        original = setup._write_json

        def interrupt(path, value):
            if path.name == "agent.json":
                raise OSError("simulated interruption after owner write")
            return original(path, value)

        with patch.object(setup.Client, "post", return_value=response) as post:
            with patch.object(setup, "_write_json", side_effect=interrupt):
                with self.assertRaises(OSError):
                    self.prepare(enrollment_file=invitation)
            recovered = self.prepare(enrollment_file=invitation)
        self.assertEqual(post.call_count, 1)
        self.assertTrue(recovered["created"])
        self.assertEqual(self.read_json("agent.json"), response["agent"])

    def test_doctor_missing_and_offline_never_report_ready(self):
        result = setup.doctor(self.state)
        self.assertFalse(result["fully_ready"])
        self.assertEqual(result["stages"]["profile"], "missing")
        self.prepare()
        with patch.object(setup.Client, "get", side_effect=TransportError("offline")):
            result = setup.doctor(self.state)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["stages"]["relay"], "unavailable_or_rejected")
        self.assertEqual(result["stages"]["peer_roundtrip"], "not_verified")

    def test_doctor_rejects_owner_credential_substituted_into_agent_file(self):
        self.prepare()
        agent = self.read_json("agent.json")
        agent["token"] = self.read_json("owner.json")["token"]
        (self.state / "agent.json").write_text(json.dumps(agent))
        actor = {"role": "owner", "id": agent["owner_id"], "owner_id": agent["owner_id"]}
        with patch.object(setup.Client, "get", return_value=actor):
            result = setup.doctor(self.state)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["stages"]["agent_identity"], "rejected")
        self.assertFalse(result["fully_ready"])

    def test_doctor_checks_real_http_agent_identity_but_not_host_delivery(self):
        self.prepare()
        store = Store(str(self.state / "relay.sqlite3"), (self.state / "admin.token").read_text().strip())
        server = create_server(store, host="127.0.0.1", port=0)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
        thread.start()
        try:
            manifest = self.read_json("profile.json")
            manifest["relay"] = "http://127.0.0.1:{}".format(server.server_address[1])
            (self.state / "profile.json").write_text(json.dumps(manifest))
            result = setup.doctor(self.state)
            self.assertEqual(result["status"], "relay_connected")
            self.assertEqual(result["stages"]["agent_identity"], "verified")
            self.assertFalse(result["fully_ready"])
            self.assertEqual(result["stages"]["host_delivery"], "not_verified")
            self.assertEqual(result["stages"]["peer_roundtrip"], "not_verified")
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()
            store.close()


if __name__ == "__main__":
    unittest.main()
