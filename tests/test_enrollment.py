"""Enrollment and bilateral pairing, including transactional failure cases."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from airshower.errors import APIError
from airshower.server import create_server
from airshower.store import Store


class EnrollmentTests(unittest.TestCase):
    ADMIN = "test-enrollment-admin-secret-at-least-16"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "relay.sqlite3")
        self.store = Store(self.db_path, self.ADMIN)
        self.server = create_server(self.store)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.owner = self.call("POST", "/v1/owners", self.ADMIN, {"name": "Avery"}, 201)
        self.agent = self.call("POST", "/v1/agents", self.owner["token"], {"name": "Agent"}, 201)
        self.other = self.call("POST", "/v1/owners", self.ADMIN, {"name": "Morgan"}, 201)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.store.close()
        self.temp.cleanup()

    def call(self, method, path, token=None, data=None, status=200, extra_headers=None):
        headers = dict(extra_headers or {})
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            value = json.loads(response.read())
            self.assertEqual(response.status, status, value)
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            return value
        finally:
            connection.close()

    def enrollment(self, data=None):
        return self.call("POST", "/v1/enrollments", self.ADMIN, data or {}, 201)

    def enroll(self, token, status=201, data=None):
        return self.call("POST", "/v1/enroll", token,
                         data or {"name": "New owner", "agent_name": "Existing agent"}, status)

    def invite(self, owner=None, data=None):
        return self.call("POST", "/v1/contact-invites", (owner or self.owner)["token"], data or {}, 201)

    def accept(self, token, owner=None, status=200):
        return self.call("POST", "/v1/contact-invites/accept", (owner or self.other)["token"], {"token": token}, status)

    def contacts(self, owner):
        return self.call("GET", "/v1/contacts", owner["token"])["contacts"]

    def test_enrollment_issues_independent_working_credentials_once(self):
        before = time.time()
        invitation = self.enrollment()
        self.assertGreaterEqual(invitation["expires_at"], before + 3600)
        result = self.enroll(invitation["token"])
        self.assertEqual(set(result), {"owner", "agent"})
        self.assertEqual(set(result["owner"]), {"id", "name", "token"})
        self.assertEqual(set(result["agent"]), {"id", "owner_id", "name", "token"})
        self.assertEqual(result["agent"]["owner_id"], result["owner"]["id"])
        self.assertEqual(len({invitation["token"], result["owner"]["token"], result["agent"]["token"]}), 3)
        for kind in ("owner", "agent"):
            identity = self.call("GET", "/v1/me", result[kind]["token"])
            self.assertEqual(identity["role"], kind)
            self.assertEqual(identity["id"], result[kind]["id"])
            self.assertNotIn("token", identity)
        used = self.enroll(invitation["token"], 401)
        unknown = self.enroll("x" * 43, 401)
        self.assertEqual(used, unknown)
        self.assertEqual(self.contacts(result["owner"]), [])

    def test_enrollment_and_pairing_tokens_cannot_authenticate_ordinary_endpoints(self):
        for token in (self.enrollment()["token"], self.invite()["token"]):
            self.call("GET", "/v1/me", token, status=401)
            self.call("POST", "/v1/agents", token, {"name": "Injected"}, 401)
        for token in (self.ADMIN, self.owner["token"], self.agent["token"], self.invite()["token"]):
            self.enroll(token, 401)
        self.call("GET", "/health")
        self.assertEqual(self.call("GET", "/v1/me", self.agent["token"])["id"], self.agent["id"])

    def test_minting_and_acceptance_require_the_exact_role(self):
        invite = self.invite()
        for token in (self.owner["token"], self.agent["token"]):
            self.call("POST", "/v1/enrollments", token, {}, 403)
        for token in (self.ADMIN, self.agent["token"]):
            self.call("POST", "/v1/contact-invites", token, {}, 403)
            self.call("POST", "/v1/contact-invites/accept", token, {"token": invite["token"]}, 403)
        self.accept(invite["token"])

    def test_lifetimes_and_fields_are_strict(self):
        for path, actor in (("/v1/enrollments", self.ADMIN), ("/v1/contact-invites", self.owner["token"])):
            for data in ({"expires_in": 0}, {"expires_in": -1}, {"expires_in": 86401},
                         {"expires_in": True}, {"expires_in": 1.5}, {"expires_in": "60"},
                         {"expires_in": None}, {"unexpected": True}):
                with self.subTest(path=path, data=data):
                    self.call("POST", path, actor, data, 400)
            self.call("POST", path, actor, {"expires_in": 86400}, 201)
        invitation = self.enrollment()
        for data in ({}, {"name": "x"}, {"name": "x", "agent_name": ""},
                     {"name": " " * 3, "agent_name": "x"}, {"name": 1, "agent_name": "x"},
                     {"name": "x" * 161, "agent_name": "x"},
                     {"name": "x", "agent_name": "é" * 81},
                     {"name": "x", "agent_name": "x", "role": "admin"}):
            self.call("POST", "/v1/enroll", invitation["token"], data, 400)
        # Invalid input must not consume a valid invitation.
        self.enroll(invitation["token"])
        for data in ({}, {"token": "x" * 43, "owner_id": self.owner["id"]}):
            self.call("POST", "/v1/contact-invites/accept", self.other["token"], data, 400)

    def test_expired_credentials_fail_without_creating_accounts_or_contacts(self):
        invitation = self.enrollment({"expires_in": 1})
        invite = self.invite(data={"expires_in": 1})
        with patch("airshower.store.time.time", return_value=max(invitation["expires_at"], invite["expires_at"])):
            self.enroll(invitation["token"], 401)
            self.accept(invite["token"], status=404)
        self.assertEqual(self.contacts(self.owner), [])
        self.assertEqual(self.contacts(self.other), [])
        with self.store.lock:
            self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM owners").fetchone()[0], 2)

    def test_acceptance_requires_both_owners_and_grants_no_automatic_execution(self):
        invite = self.invite()
        self.assertEqual(self.contacts(self.owner), [])
        self.assertEqual(self.contacts(self.other), [])
        self.accept(invite["token"], self.owner, 400)
        result = self.accept(invite["token"])
        self.assertEqual(result, {"owner_id": self.owner["id"], "connected": True})
        self.assertEqual(self.contacts(self.owner), [{"owner_id": self.other["id"]}])
        self.assertEqual(self.contacts(self.other), [{"owner_id": self.owner["id"]}])
        remote = self.call("POST", "/v1/agents", self.other["token"], {"name": "Remote"}, 201)
        for sender, recipient in ((self.agent, remote), (remote, self.agent)):
            visible = self.call("GET", "/v1/agents", sender["token"])["agents"]
            self.assertIn(recipient["id"], [agent["id"] for agent in visible])
            request = self.call("POST", "/v1/requests", sender["token"], {
                "recipient_agent_id": recipient["id"], "kind": "task", "message": "Check before doing anything",
                "source_ids": [], "idempotency_key": "pairing-requires-consent",
            }, 201)
            self.assertEqual(request["status"], "pending_approval")
            self.call("POST", "/v1/requests/%s/claim" % request["id"], recipient["token"], {}, 409)
        self.accept(invite["token"], status=404)

    def test_replaying_invite_does_not_restore_revoked_contact(self):
        invite = self.invite()
        self.accept(invite["token"])
        self.call("DELETE", "/v1/contacts/" + self.other["id"], self.owner["token"])
        self.accept(invite["token"], status=404)
        self.assertEqual(self.contacts(self.owner), [])

    def test_credentials_persist_as_hashes_and_work_across_store_restart(self):
        enrollment = self.enrollment()
        invite = self.invite()
        with self.store.lock:
            for table, credential in (("enrollments", enrollment), ("contact_invites", invite)):
                row = dict(self.store.db.execute("SELECT * FROM " + table).fetchone())
                self.assertEqual(row["token_hash"], hashlib.sha256(credential["token"].encode()).hexdigest())
                self.assertNotIn(credential["token"], json.dumps(row))
        other_store = Store(self.db_path, self.ADMIN)
        try:
            status, result = other_store.enroll(enrollment["token"], {"name": "Durable", "agent_name": "Existing"})
            self.assertEqual(status, 201)
            self.assertEqual(self.store.authenticate(result["agent"]["token"])["role"], "agent")
            actor = other_store.authenticate(self.other["token"])
            other_store.dispatch("POST", "/v1/contact-invites/accept", actor, {"token": invite["token"]})
        finally:
            other_store.close()
        self.enroll(enrollment["token"], 401)
        self.accept(invite["token"], status=404)

    def test_simultaneous_enrollment_is_single_use_across_database_connections(self):
        invitation = self.enrollment()
        stores = [self.store, Store(self.db_path, self.ADMIN)]
        barrier = threading.Barrier(2)

        def run(store):
            barrier.wait(timeout=5)
            try:
                return store.enroll(invitation["token"], {"name": "Concurrent", "agent_name": "Agent"})[0]
            except APIError as error:
                return error.status

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(sorted(pool.map(run, stores)), [201, 401])
        finally:
            stores[1].close()
        with self.store.lock:
            self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM owners").fetchone()[0], 3)
            self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM agents").fetchone()[0], 2)

    def test_simultaneous_contact_acceptance_connects_exactly_one_recipient(self):
        invitation = self.invite()
        third = self.call("POST", "/v1/owners", self.ADMIN, {"name": "Third"}, 201)
        second_store = Store(self.db_path, self.ADMIN)
        barrier = threading.Barrier(2)

        def run(pair):
            store, owner = pair
            actor = store.authenticate(owner["token"])
            barrier.wait(timeout=5)
            try:
                return store.dispatch("POST", "/v1/contact-invites/accept", actor, {"token": invitation["token"]})[0]
            except APIError as error:
                return error.status

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(run, [(self.store, self.other), (second_store, third)]))
            self.assertEqual(sorted(results), [200, 404])
        finally:
            second_store.close()
        winner, loser = (self.other, third) if results[0] == 200 else (third, self.other)
        self.assertEqual(self.contacts(self.owner), [{"owner_id": winner["id"]}])
        self.assertEqual(self.contacts(winner), [{"owner_id": self.owner["id"]}])
        self.assertEqual(self.contacts(loser), [])

    def test_failed_agent_creation_rolls_back_owner_and_token_consumption(self):
        invitation = self.enrollment()
        with self.store.lock:
            self.store.db.execute("CREATE TRIGGER fail_agent BEFORE INSERT ON agents BEGIN SELECT RAISE(ABORT, 'test failure'); END")
            self.store.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.enroll(invitation["token"], {"name": "Atomic", "agent_name": "Agent"})
        with self.store.lock:
            self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM owners").fetchone()[0], 2)
            self.assertIsNone(self.store.db.execute("SELECT consumed_at FROM enrollments").fetchone()[0])
            self.store.db.execute("DROP TRIGGER fail_agent")
            self.store.db.commit()
        self.enroll(invitation["token"])

    def test_expiry_is_checked_after_waiting_for_another_database_writer(self):
        for kind in ("enrollment", "contact"):
            with self.subTest(kind=kind):
                invitation = self.enrollment() if kind == "enrollment" else self.invite()
                blocker = sqlite3.connect(self.db_path)
                blocker.execute("BEGIN IMMEDIATE")
                reached_write = threading.Event()
                now = [invitation["expires_at"] - 1]
                actor = self.store.authenticate(self.other["token"])

                def trace(statement):
                    if statement.startswith(("BEGIN IMMEDIATE", "UPDATE")):
                        reached_write.set()

                def redeem():
                    try:
                        if kind == "enrollment":
                            return self.store.enroll(invitation["token"], {"name": "New", "agent_name": "Agent"})[0]
                        return self.store.dispatch("POST", "/v1/contact-invites/accept", actor, {"token": invitation["token"]})[0]
                    except APIError as error:
                        return error.status

                self.store.db.set_trace_callback(trace)
                try:
                    with patch("airshower.store.time.time", side_effect=lambda: now[0]):
                        with ThreadPoolExecutor(max_workers=1) as pool:
                            future = pool.submit(redeem)
                            self.assertTrue(reached_write.wait(timeout=2))
                            now[0] = invitation["expires_at"]
                            blocker.rollback()
                            self.assertEqual(future.result(timeout=3), 401 if kind == "enrollment" else 404)
                finally:
                    blocker.close()
                    self.store.db.set_trace_callback(None)

    def test_failed_second_contact_insert_rolls_back_both_directions_and_consumption(self):
        invitation = self.invite()
        with self.store.lock:
            self.store.db.execute("""CREATE TRIGGER fail_contact BEFORE INSERT ON contacts
                WHEN EXISTS(SELECT 1 FROM contacts)
                BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
            self.store.db.commit()
        actor = self.store.authenticate(self.other["token"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.dispatch("POST", "/v1/contact-invites/accept", actor, {"token": invitation["token"]})
        self.assertEqual(self.contacts(self.owner), [])
        self.assertEqual(self.contacts(self.other), [])
        with self.store.lock:
            self.assertIsNone(self.store.db.execute("SELECT consumed_at FROM contact_invites").fetchone()[0])
            self.store.db.execute("DROP TRIGGER fail_contact")
            self.store.db.commit()
        self.accept(invitation["token"])

    def test_existing_browser_and_query_restrictions_apply_before_enrollment(self):
        invitation = self.enrollment()
        body = {"name": "Person", "agent_name": "Agent"}
        self.call("POST", "/v1/enroll", invitation["token"], body, 403, {"Origin": "https://example.com"})
        self.call("POST", "/v1/enroll?token=" + invitation["token"], invitation["token"], body, 400)
        self.call("POST", "/v1/enroll", None, body, 401)
        self.enroll(invitation["token"])

    @unittest.skipUnless(callable(getattr(hashlib, "scrypt", None)), "This Python runtime does not provide hashlib.scrypt")
    def test_consent_secret_is_salted_hashed_and_never_returned_or_persisted_plaintext(self):
        secret = "test-only approval password for this fixture"
        identities = []
        for _ in range(2):
            invitation = self.enrollment()
            identities.append(self.enroll(invitation["token"], data={
                "name": "Owner with approval", "agent_name": "Agent", "consent_secret": secret,
            }))
        with self.store.lock:
            rows = [dict(row) for row in self.store.db.execute("SELECT * FROM owner_consent_credentials")]
            dump = "\n".join(self.store.db.iterdump())
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["salt"], rows[1]["salt"])
        self.assertNotEqual(rows[0]["password_hash"], rows[1]["password_hash"])
        for row in rows:
            self.assertEqual(len(row["salt"]), 16)
            self.assertEqual(len(row["password_hash"]), 32)
            self.assertEqual(row["password_hash"], hashlib.scrypt(secret.encode("utf-8"), salt=row["salt"],
                                                                n=16384, r=8, p=1, dklen=32))
            self.assertEqual(row["failed_attempts"], 0)
            self.assertEqual(row["blocked_until"], 0)
        self.assertNotIn(secret, dump)
        for result in identities:
            self.assertEqual(set(result), {"owner", "agent"})
            self.assertEqual(set(result["owner"]), {"id", "name", "token"})
            self.assertEqual(set(result["agent"]), {"id", "owner_id", "name", "token"})
            self.assertNotIn(secret, json.dumps(result))
            events = self.call("GET", "/v1/events/history", result["owner"]["token"])
            self.assertEqual(events["events"], [])
        for suffix in ("", "-wal", "-shm"):
            path = Path(self.db_path + suffix)
            if path.exists():
                self.assertNotIn(secret.encode("utf-8"), path.read_bytes())

    def test_invalid_consent_secret_does_not_consume_enrollment_or_create_rows(self):
        invitation = self.enrollment()
        for secret in (None, True, 12, {}, [], "", " " * 12, "a" * 11, "a" * 129, "\ud800" * 12):
            with self.subTest(type=type(secret).__name__, length=len(secret) if isinstance(secret, str) else None):
                response = self.enroll(invitation["token"], 400, {
                    "name": "Owner", "agent_name": "Agent", "consent_secret": secret,
                })
                if isinstance(secret, str) and secret.strip() and len(secret) >= 11:
                    self.assertNotIn(secret, json.dumps(response))
        with self.store.lock:
            self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM owners").fetchone()[0], 2)
            self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM agents").fetchone()[0], 1)
            self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM owner_consent_credentials").fetchone()[0], 0)
            self.assertIsNone(self.store.db.execute("SELECT consumed_at FROM enrollments").fetchone()[0])
        result = self.enroll(invitation["token"])
        with self.store.lock:
            self.assertIsNone(self.store.db.execute("SELECT 1 FROM owner_consent_credentials WHERE owner_id=?",
                                                  (result["owner"]["id"],)).fetchone())

    @unittest.skipUnless(callable(getattr(hashlib, "scrypt", None)), "This Python runtime does not provide hashlib.scrypt")
    def test_consent_secret_unicode_boundary_and_owner_token_cannot_reset_it(self):
        secret = "\U0001f310" * 128
        result = self.enroll(self.enrollment()["token"], data={
            "name": "Owner", "agent_name": "Agent", "consent_secret": secret,
        })
        with self.store.lock:
            original = dict(self.store.db.execute("SELECT * FROM owner_consent_credentials").fetchone())
        self.enroll(result["owner"]["token"], 401, {
            "name": "Owner", "agent_name": "Agent", "consent_secret": "replacement password",
        })
        self.enroll(result["agent"]["token"], 401, {
            "name": "Owner", "agent_name": "Agent", "consent_secret": "replacement password",
        })
        with self.store.lock:
            self.assertEqual(dict(self.store.db.execute("SELECT * FROM owner_consent_credentials").fetchone()), original)
        self.assertEqual(original["password_hash"], hashlib.scrypt(secret.encode("utf-8"), salt=original["salt"],
                                                                n=16384, r=8, p=1, dklen=32))

    @unittest.skipUnless(callable(getattr(hashlib, "scrypt", None)), "This Python runtime does not provide hashlib.scrypt")
    def test_consent_hash_failure_rolls_back_all_enrollment_changes(self):
        invitation = self.enrollment()
        payload = {"name": "Owner", "agent_name": "Agent", "consent_secret": "test-only approval password"}
        with patch("airshower.store.hashlib.scrypt", side_effect=ValueError("private failure")):
            with self.assertRaises(ValueError):
                self.store.enroll(invitation["token"], payload)
        with self.store.lock:
            self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM owners").fetchone()[0], 2)
            self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM agents").fetchone()[0], 1)
            self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM owner_consent_credentials").fetchone()[0], 0)
            self.assertIsNone(self.store.db.execute("SELECT consumed_at FROM enrollments").fetchone()[0])
        self.enroll(invitation["token"], data=payload)

    def test_missing_scrypt_fails_closed_without_consuming_invitation(self):
        invitation = self.enrollment()
        with patch.object(hashlib, "scrypt", None, create=True):
            result = self.enroll(invitation["token"], 503, {
                "name": "Owner", "agent_name": "Agent", "consent_secret": "test-only approval password",
            })
        self.assertEqual(result["error"]["code"], "consent_unavailable")
        with self.store.lock:
            self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM owner_consent_credentials").fetchone()[0], 0)
            self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM owners").fetchone()[0], 2)
            self.assertIsNone(self.store.db.execute("SELECT consumed_at FROM enrollments").fetchone()[0])
        self.enroll(invitation["token"])


if __name__ == "__main__":
    unittest.main()
