"""Browser consent boundaries exercised through a real loopback HTTP server.

The fixture supplies the externally configured HTTPS Host and Origin without
installing a certificate or opening a public listener. It does not test TLS.
"""

import hashlib
import http.client
import json
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit
import uuid

from airshower.consent import COOKIE, MAX_FORM_BYTES
from airshower.server import create_server
from airshower.store import Store


@unittest.skipUnless(callable(getattr(hashlib, "scrypt", None)),
                     "This Python runtime does not provide hashlib.scrypt")
class ConsentTests(unittest.TestCase):
    ADMIN = "test-consent-administrator-token"
    SECRET = "test-only browser approval password"
    ORIGIN = "https://approval.example"
    REMOTE_TEXT = '<script>alert("remote request")</script> & private details'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "relay.sqlite3")
        self._start()
        self.sender_owner = self.api("POST", "/v1/owners", self.ADMIN,
                                     {"name": '<img src=x onerror="alert(1)">'}, 201)
        self.sender = self.api("POST", "/v1/agents", self.sender_owner["token"],
                               {"name": "Sender agent"}, 201)
        enrollment = self.api("POST", "/v1/enrollments", self.ADMIN, {}, 201)
        enrolled = self.api("POST", "/v1/enroll", enrollment["token"], {
            "name": "Recipient", "agent_name": "Recipient agent", "consent_secret": self.SECRET,
        }, 201)
        self.owner, self.agent = enrolled["owner"], enrolled["agent"]
        self.api("POST", "/v1/contacts", self.owner["token"],
                 {"owner_id": self.sender_owner["id"]}, 201)

    def _start(self):
        self.store = Store(self.db_path, self.ADMIN)
        self.server = create_server(self.store, public_url=self.ORIGIN)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.store.close()

    def tearDown(self):
        self._stop()
        self.temp.cleanup()

    def exchange(self, method, path, body=None, headers=()):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for key, value in headers:
                connection.putheader(key, value)
            connection.endheaders(body)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode("utf-8")
        finally:
            connection.close()

    def api(self, method, path, token=None, data=None, expected=200):
        headers = [("Host", "approval.example")]
        if token is not None:
            headers.append(("Authorization", "Bearer " + token))
        body = json.dumps(data).encode("utf-8") if data is not None else None
        if body is not None:
            headers.extend([("Content-Type", "application/json"), ("Content-Length", str(len(body)))])
        status, headers, body = self.exchange(method, path, body, headers)
        value = json.loads(body)
        self.assertEqual(status, expected, value)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        return value

    def browser(self, consent, method="GET", form=None, cookie=None, expected=200,
                raw=None, replace=None, extra=()):
        body = raw if raw is not None else urlencode(form).encode("utf-8") if form is not None else None
        headers = {"Host": "approval.example"}
        if method == "POST":
            headers.update({"Origin": self.ORIGIN, "Content-Type": "application/x-www-form-urlencoded",
                            "Content-Length": str(len(body or b""))})
        if cookie:
            headers["Cookie"] = cookie
        for key, value in (replace or {}).items():
            if value is None:
                headers.pop(key, None)
            else:
                headers[key] = value
        status, response_headers, text = self.exchange(method, urlsplit(consent["url"]).path,
                                                       body, list(headers.items()) + list(extra))
        self.assertEqual(status, expected, text)
        self.assertEqual(response_headers.get("Cache-Control"), "no-store")
        return response_headers, text

    def request(self, kind="task"):
        return self.api("POST", "/v1/requests", self.sender["token"], {
            "recipient_agent_id": self.agent["id"], "kind": kind, "message": self.REMOTE_TEXT,
            "source_ids": [], "idempotency_key": str(uuid.uuid4()),
        }, 201)

    def consent(self, request=None):
        request = request or self.request()
        return self.api("POST", "/v1/consents", self.owner["token"], {
            "request_id": request["id"], "recipient_agent_id": self.agent["id"],
        }, 201)

    def status(self, consent):
        return self.api("GET", "/v1/consents/" + consent["id"], self.owner["token"])

    def csrf(self, text):
        match = re.search(r"name='_csrf' value='([^']+)'", text)
        self.assertIsNotNone(match, text)
        return match.group(1)

    def login(self, consent):
        _, initial = self.browser(consent)
        headers, page = self.browser(consent, "POST", {
            "_csrf": self.csrf(initial), "action": "login", "password": self.SECRET,
        })
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        return cookie, self.csrf(page), headers, page

    def test_public_url_is_identifier_only_and_details_require_password(self):
        request = self.request()
        consent = self.consent(request)
        self.assertEqual(set(consent), {"id", "url", "expires_at"})
        self.assertEqual(consent["url"], self.ORIGIN + "/consent/" + consent["id"])
        self.assertAlmostEqual(consent["expires_at"] - time.time(), 120, delta=2)
        headers, page = self.browser(consent)
        self.assertIn("Sign in to review", page)
        for private in (self.REMOTE_TEXT, "remote request", self.sender_owner["name"], request["id"],
                        self.owner["token"], self.agent["token"], self.SECRET):
            self.assertNotIn(private, page)
            self.assertNotIn(private, json.dumps(consent))
            self.assertNotIn(private, json.dumps(self.status(consent)))
        self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("form-action 'self'", headers["Content-Security-Policy"])

    def test_login_escapes_canonical_request_and_sets_scoped_secure_cookie(self):
        request = self.request()
        consent = self.consent(request)
        _, first = self.browser(consent)
        cookie, csrf, headers, page = self.login(consent)
        self.assertNotEqual(csrf, self.csrf(first))
        self.assertTrue(cookie.startswith(COOKIE + "="))
        for flag in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/consent/" + consent["id"]):
            self.assertIn(flag, headers["Set-Cookie"])
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("&lt;img", page)
        self.assertNotIn("<script>", page)
        self.assertNotIn("<img", page)
        self.assertIn("Sender agent", page)
        self.assertIn("Task request", page)
        self.assertIn("remote request", page)
        self.assertNotIn(request["id"], page)
        self.assertNotIn(self.agent["id"], page)
        self.assertNotIn(self.SECRET, page)
        self.assertNotIn(self.owner["token"], page)
        _, refreshed = self.browser(consent, cookie=cookie)
        self.assertIn("remote request", refreshed)
        _, anonymous = self.browser(consent)
        self.assertNotIn("remote request", anonymous)
        session = cookie.split("=", 1)[1]
        with self.store.lock:
            stored = self.store.db.execute("SELECT session_hash FROM browser_consents WHERE id=?", (consent["id"],)).fetchone()[0]
            dump = "\n".join(self.store.db.iterdump())
        self.assertEqual(stored, hashlib.sha256(session.encode()).hexdigest())
        self.assertNotIn(session, dump)

    def test_approve_deny_and_answer_record_only_the_exact_decision(self):
        for kind, action, content in (("task", "approve", {"decision": "approve"}),
                                      ("task", "deny", {"decision": "deny"}),
                                      ("ask_human", "answer", {"answer": "Friday works for me."})):
            with self.subTest(action=action):
                request = self.request(kind)
                consent = self.consent(request)
                cookie, csrf, _, _ = self.login(consent)
                form = {"_csrf": csrf, "action": action}
                if action == "answer":
                    form.update(content)
                headers, page = self.browser(consent, "POST", form, cookie)
                self.assertIn("Response recorded", page)
                self.assertIn("does not itself complete the request", page)
                self.assertIn("Max-Age=0", headers["Set-Cookie"])
                self.assertEqual(self.status(consent), {"id": consent["id"], "status": "accepted", "content": content})
                # Recording browser intent does not itself grant or complete a request.
                current = self.api("GET", "/v1/requests/" + request["id"], self.owner["token"])
                self.assertEqual(current, request)
                self.browser(consent, "POST", form, cookie, expected=410)
                self.browser(consent, cookie=cookie, expected=410)

    def test_decline_closes_review_without_answer_or_request_mutation(self):
        request = self.request("ask_human")
        consent = self.consent(request)
        cookie, csrf, _, _ = self.login(consent)
        self.browser(consent, "POST", {"_csrf": csrf, "action": "decline", "answer": ""}, cookie)
        self.assertEqual(self.status(consent), {"id": consent["id"], "status": "declined", "content": {}})
        self.assertEqual(self.api("GET", "/v1/requests/" + request["id"], self.owner["token"]), request)

    def test_missing_approval_credentials_and_non_owner_roles_fail_closed(self):
        request = self.request()
        data = {"request_id": request["id"], "recipient_agent_id": self.agent["id"]}
        for token in (self.sender_owner["token"], self.sender["token"], self.agent["token"], self.ADMIN):
            self.api("POST", "/v1/consents", token, data, 403)
        consent = self.consent(request)
        for token in (self.agent["token"], self.ADMIN):
            self.api("GET", "/v1/consents/" + consent["id"], token, expected=403)
        self.api("GET", "/v1/consents/" + consent["id"], expected=401)
        self.api("GET", "/v1/consents/" + consent["id"], self.sender_owner["token"], expected=404)

    def test_wrong_recipient_and_foreign_request_are_rejected(self):
        request = self.request()
        for recipient in (self.sender["id"], str(uuid.uuid4())):
            self.api("POST", "/v1/consents", self.owner["token"], {
                "request_id": request["id"], "recipient_agent_id": recipient,
            }, 403)
        self.api("POST", "/v1/consents", self.owner["token"], {
            "request_id": str(uuid.uuid4()), "recipient_agent_id": self.agent["id"],
        }, 404)
        invitation = self.api("POST", "/v1/enrollments", self.ADMIN, {}, 201)
        other = self.api("POST", "/v1/enroll", invitation["token"], {
            "name": "Unrelated", "agent_name": "Other", "consent_secret": self.SECRET,
        }, 201)
        self.api("POST", "/v1/consents", other["owner"]["token"], {
            "request_id": request["id"], "recipient_agent_id": self.agent["id"],
        }, 404)

    def test_five_failed_logins_lock_out_even_correct_password_across_restart(self):
        consent = self.consent()
        _, page = self.browser(consent)
        form = {"_csrf": self.csrf(page), "action": "login", "password": "wrong-test-password"}
        for _ in range(5):
            headers, error = self.browser(consent, "POST", form, expected=403)
            self.assertNotIn("Set-Cookie", headers)
            self.assertNotIn(form["password"], error)
        self._stop()
        self._start()
        for password in ("wrong-test-password", self.SECRET):
            form["password"] = password
            self.browser(consent, "POST", form, expected=429)
        with self.store.lock:
            row = self.store.db.execute("SELECT failed_attempts,blocked_until FROM owner_consent_credentials WHERE owner_id=?", (self.owner["id"],)).fetchone()
        self.assertEqual(row["failed_attempts"], 5)
        self.assertGreater(row["blocked_until"], time.time())
        self.assertEqual(self.status(consent)["status"], "pending")

    def test_successful_sign_in_clears_failures_and_missing_scrypt_fails_closed(self):
        consent = self.consent()
        _, page = self.browser(consent)
        form = {"_csrf": self.csrf(page), "action": "login", "password": "wrong-test-password"}
        self.browser(consent, "POST", form, expected=403)
        form["password"] = self.SECRET
        with patch.object(hashlib, "scrypt", None):
            headers, error = self.browser(consent, "POST", form, expected=503)
        self.assertNotIn("Set-Cookie", headers)
        self.assertNotIn(self.SECRET, error)
        self.login(consent)
        with self.store.lock:
            row = self.store.db.execute("SELECT failed_attempts,blocked_until FROM owner_consent_credentials WHERE owner_id=?", (self.owner["id"],)).fetchone()
        self.assertEqual(tuple(row), (0, 0))

    def test_authentication_requires_cookie_current_csrf_and_exact_origin(self):
        consent = self.consent()
        _, initial = self.browser(consent)
        cookie, csrf, _, _ = self.login(consent)
        form = {"_csrf": csrf, "action": "approve"}
        for bad_cookie in (None, COOKIE + "=wrong", "unrelated=value"):
            self.browser(consent, "POST", form, bad_cookie, expected=403)
        for bad_csrf in (None, "wrong", self.csrf(initial)):
            bad_form = {"action": "approve"}
            if bad_csrf is not None:
                bad_form["_csrf"] = bad_csrf
            self.browser(consent, "POST", bad_form, cookie, expected=403)
        for origin in (None, "null", "http://approval.example", "https://attacker.example"):
            self.browser(consent, "POST", form, cookie, expected=403, replace={"Origin": origin})
        self.browser(consent, "POST", form, cookie, expected=403, extra=[("Origin", self.ORIGIN)])
        self.assertEqual(self.status(consent)["status"], "pending")
        self.browser(consent, "POST", form, cookie)

    def test_cookie_from_other_review_and_replaced_login_session_do_not_authorize(self):
        first, second = self.consent(), self.consent()
        cookie1, _, _, _ = self.login(first)
        cookie2, csrf2, _, _ = self.login(second)
        self.browser(second, "POST", {"_csrf": csrf2, "action": "approve"}, cookie1, expected=403)
        fresh_cookie, fresh_csrf, _, _ = self.login(first)
        self.browser(first, "POST", {"_csrf": fresh_csrf, "action": "approve"}, cookie1, expected=403)
        self.browser(first, "POST", {"_csrf": fresh_csrf, "action": "approve"}, fresh_cookie)
        self.browser(second, "POST", {"_csrf": csrf2, "action": "approve"}, cookie2)

    def test_host_is_exact_and_query_credentials_are_never_accepted(self):
        consent = self.consent()
        for host in (None, "attacker.example", "approval.example:443", "APPROVAL.EXAMPLE"):
            self.browser(consent, expected=404, replace={"Host": host})
        self.browser(consent, expected=404, extra=[("Host", "approval.example")])
        query = urlencode({"password": self.SECRET})
        status, _, text = self.exchange("GET", urlsplit(consent["url"]).path + "?" + query,
                                         headers=[("Host", "approval.example")])
        self.assertEqual(status, 400)
        self.assertNotIn(self.SECRET, text)
        self.assertNotIn(query, text)

    def test_malformed_duplicate_and_oversize_forms_do_not_record_a_decision(self):
        consent = self.consent()
        cookie, csrf, _, _ = self.login(consent)
        base = urlencode({"_csrf": csrf, "action": "approve"}).encode("ascii")
        for raw in (base + b"&action=deny", base + b"&x=%GG", base + b"&x=%FF",
                    base + b"&missing_equals", base + b"&x=1&y=2&z=3&w=4", b"\xff"):
            with self.subTest(raw=raw[-40:]):
                self.browser(consent, "POST", cookie=cookie, raw=raw, expected=400)
        self.browser(consent, "POST", cookie=cookie, raw=b"x" * (MAX_FORM_BYTES + 1), expected=413)
        self.browser(consent, "POST", cookie=cookie, raw=base, expected=415,
                     replace={"Content-Type": "application/json"})
        self.browser(consent, "POST", cookie=cookie, raw=base, expected=400,
                     replace={"Content-Length": None})
        self.browser(consent, "POST", cookie=cookie, raw=base, expected=400,
                     extra=[("Content-Length", str(len(base)))])
        self.browser(consent, "POST", cookie=cookie, raw=base, expected=400,
                     extra=[("Transfer-Encoding", "chunked")])
        self.browser(consent, "POST", cookie=cookie, raw=base, expected=415,
                     extra=[("Content-Type", "application/x-www-form-urlencoded")])
        self.browser(consent, "POST", cookie=cookie, raw=base, expected=400,
                     extra=[("Cookie", cookie)])
        self.browser(consent, raw=b"x", expected=400, replace={"Content-Length": "1"})
        self.browser(consent, "DELETE", expected=405)
        self.assertEqual(self.status(consent)["status"], "pending")

    def test_request_action_shape_and_answer_limits_are_enforced(self):
        consent = self.consent()
        cookie, csrf, _, _ = self.login(consent)
        for extras in ({"action": "connect"}, {"action": "answer", "answer": "bad"},
                       {"action": "approve", "answer": "bad"}, {"action": "approve", "unexpected": "x"}):
            self.browser(consent, "POST", dict(extras, _csrf=csrf), cookie, expected=400)
        human = self.consent(self.request("ask_human"))
        cookie, csrf, _, _ = self.login(human)
        for extras in ({"action": "approve"}, {"action": "deny"}, {"action": "answer"},
                       {"action": "answer", "answer": "   "},
                       {"action": "answer", "answer": "a" * 8193}):
            self.browser(human, "POST", dict(extras, _csrf=csrf), cookie, expected=400)
        self.assertEqual(self.status(human)["status"], "pending")
        self.browser(human, "POST", {"_csrf": csrf, "action": "answer", "answer": "a" * 8192}, cookie)

    def test_expired_consent_cannot_be_viewed_logged_in_or_reused(self):
        consent = self.consent()
        cookie, csrf, _, _ = self.login(consent)
        with self.store.lock:
            self.store.db.execute("UPDATE browser_consents SET expires_at=? WHERE id=?", (time.time() - 1, consent["id"]))
            self.store.db.commit()
        self.browser(consent, expected=410)
        self.browser(consent, "POST", {"_csrf": csrf, "action": "approve"}, cookie, expected=410)
        self.browser(consent, "POST", {"_csrf": csrf, "action": "login", "password": self.SECRET}, expected=410)
        self.assertEqual(self.status(consent), {"id": consent["id"], "status": "declined", "content": {}})

    def test_cancelled_request_and_revoked_contact_cannot_be_approved(self):
        for revoke in (False, True):
            request = self.request()
            consent = self.consent(request)
            cookie, csrf, _, _ = self.login(consent)
            if revoke:
                self.api("DELETE", "/v1/contacts/" + self.sender_owner["id"], self.owner["token"])
            else:
                self.api("POST", "/v1/requests/" + request["id"] + "/cancel", self.sender["token"], {})
            self.browser(consent, "POST", {"_csrf": csrf, "action": "approve"}, cookie, expected=409)
            self.consent_creation_closed(request)

    def consent_creation_closed(self, request):
        self.api("POST", "/v1/consents", self.owner["token"], {
            "request_id": request["id"], "recipient_agent_id": self.agent["id"],
        }, 409)

    def test_changed_canonical_request_is_not_approved(self):
        request = self.request()
        consent = self.consent(request)
        cookie, csrf, _, _ = self.login(consent)
        with self.store.lock:
            self.store.db.execute("UPDATE requests SET message=? WHERE id=?", ("A different request", request["id"]))
            self.store.db.commit()
        self.browser(consent, "POST", {"_csrf": csrf, "action": "approve"}, cookie, expected=409)

    def test_review_displays_only_selected_source_labels_without_source_contents(self):
        source = self.api("POST", "/v1/sources", self.owner["token"], {
            "kind": "context", "label": "Calendar <selected>", "content": "Private appointment details",
        }, 201)
        request = self.api("POST", "/v1/requests", self.sender["token"], {
            "recipient_agent_id": self.agent["id"], "kind": "context", "message": "Ask about availability",
            "source_ids": [source["id"]], "idempotency_key": str(uuid.uuid4()),
        }, 201)
        consent = self.consent(request)
        _, anonymous = self.browser(consent)
        self.assertNotIn("Calendar", anonymous)
        cookie, csrf, _, page = self.login(consent)
        self.assertIn("Calendar &lt;selected&gt;", page)
        self.assertIn("1 selected source", page)
        self.assertNotIn("Private appointment details", page)
        self.assertNotIn(source["id"], page)
        self.browser(consent, "POST", {"_csrf": csrf, "action": "approve"}, cookie)
        self.assertEqual(self.status(consent)["content"], {"decision": "approve"})
        self.assertEqual(self.api("GET", "/v1/requests/" + request["id"], self.owner["token"]), request)

    def test_public_origin_configuration_rejects_non_https_and_credential_urls(self):
        for origin in ("http://approval.example", "https://name:password@approval.example",
                       "https://approval.example/path", "https://approval.example?secret=x",
                       "https://approval.example#fragment", "https:///missing-host"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                create_server(self.store, public_url=origin)

    def contact_consent(self):
        inviter = self.api("POST", "/v1/owners", self.ADMIN, {"name": "New contact <untrusted>"}, 201)
        invitation = self.api("POST", "/v1/contact-invites", inviter["token"], {}, 201)
        consent = self.api("POST", "/v1/consents", self.owner["token"], {"contact_invite": invitation["token"]}, 201)
        return inviter, invitation, consent

    def test_contact_review_records_intent_without_pairing_or_exposing_invite_token(self):
        inviter, invitation, consent = self.contact_consent()
        before = self.api("GET", "/v1/contacts", self.owner["token"])
        _, anonymous = self.browser(consent)
        self.assertNotIn(inviter["name"], anonymous)
        cookie, csrf, _, page = self.login(consent)
        self.assertIn("New contact &lt;untrusted&gt;", page)
        for value in (json.dumps(consent), anonymous, page, json.dumps(self.status(consent))):
            self.assertNotIn(invitation["token"], value)
        self.browser(consent, "POST", {"_csrf": csrf, "action": "connect"}, cookie)
        self.assertEqual(self.status(consent)["content"], {"decision": "connect"})
        self.assertEqual(self.api("GET", "/v1/contacts", self.owner["token"]), before)
        self.assertEqual(self.api("GET", "/v1/contacts", inviter["token"]), {"contacts": []})
        # Only the later authenticated owner-client operation performs pairing.
        self.api("POST", "/v1/contact-invites/accept", self.owner["token"], {"token": invitation["token"]})
        self.assertIn({"owner_id": inviter["id"]}, self.api("GET", "/v1/contacts", self.owner["token"])["contacts"])

    def test_expired_or_consumed_contact_invite_cannot_be_accepted_in_browser(self):
        for consumed in (False, True):
            _, invitation, consent = self.contact_consent()
            cookie, csrf, _, _ = self.login(consent)
            if consumed:
                self.api("POST", "/v1/contact-invites/accept", self.owner["token"], {"token": invitation["token"]})
            else:
                with self.store.lock:
                    self.store.db.execute("UPDATE contact_invites SET expires_at=? WHERE token_hash=?",
                                          (time.time() - 1, hashlib.sha256(invitation["token"].encode()).hexdigest()))
                    self.store.db.commit()
            self.browser(consent, "POST", {"_csrf": csrf, "action": "connect"}, cookie, expected=409)
            self.api("POST", "/v1/consents", self.owner["token"], {"contact_invite": invitation["token"]}, 404)

    def test_create_rejects_ambiguous_fields_self_invite_and_unknown_invite(self):
        request = self.request()
        base = {"request_id": request["id"], "recipient_agent_id": self.agent["id"]}
        for data in ({}, {"request_id": request["id"]}, dict(base, extra=True),
                     dict(base, contact_invite="x" * 43), dict(base, recipient_agent_id=None)):
            self.api("POST", "/v1/consents", self.owner["token"], data, 400)
        own_invite = self.api("POST", "/v1/contact-invites", self.owner["token"], {}, 201)
        self.api("POST", "/v1/consents", self.owner["token"], {"contact_invite": own_invite["token"]}, 400)
        self.api("POST", "/v1/consents", self.owner["token"], {"contact_invite": "x" * 43}, 404)

    def test_explicit_lifetimes_apply_to_request_and_contact_reviews(self):
        request = self.request()
        _, invitation, _ = self.contact_consent()
        payloads = ({"request_id": request["id"], "recipient_agent_id": self.agent["id"]},
                    {"contact_invite": invitation["token"]})
        now = time.time()
        for payload in payloads:
            for lifetime in (1, 120):
                with self.subTest(payload=tuple(payload), lifetime=lifetime):
                    with patch("airshower.consent.time.time", return_value=now):
                        consent = self.api("POST", "/v1/consents", self.owner["token"],
                                           dict(payload, expires_in=lifetime), 201)
                        self.assertEqual(consent["expires_at"], now + lifetime)
                        self.assertEqual(self.status(consent)["status"], "pending")
                    with patch("airshower.consent.time.time", return_value=now + lifetime):
                        self.browser(consent, expected=410)
                        self.assertEqual(self.status(consent)["status"], "declined")

    def test_invalid_lifetime_never_creates_a_review(self):
        request = self.request()
        _, invitation, _ = self.contact_consent()
        payloads = ({"request_id": request["id"], "recipient_agent_id": self.agent["id"]},
                    {"contact_invite": invitation["token"]})
        with self.store.lock:
            before = self.store.db.execute("SELECT COUNT(*) FROM browser_consents").fetchone()[0]
        for payload in payloads:
            for lifetime in (True, False, 0, -1, 121, 1.5, "1", None, [], {}):
                with self.subTest(payload=tuple(payload), lifetime=lifetime):
                    self.api("POST", "/v1/consents", self.owner["token"],
                             dict(payload, expires_in=lifetime), 400)
        with self.store.lock:
            self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM browser_consents").fetchone()[0], before)

    def test_short_lifetime_closes_an_already_authenticated_browser(self):
        request = self.request()
        now = time.time()
        with patch("airshower.consent.time.time", return_value=now):
            consent = self.api("POST", "/v1/consents", self.owner["token"], {
                "request_id": request["id"], "recipient_agent_id": self.agent["id"], "expires_in": 1,
            }, 201)
            cookie, csrf, _, _ = self.login(consent)
        with patch("airshower.consent.time.time", return_value=now + 1):
            self.browser(consent, cookie=cookie, expected=410)
            self.browser(consent, "POST", {"_csrf": csrf, "action": "approve"}, cookie, expected=410)
            self.assertEqual(self.status(consent), {"id": consent["id"], "status": "declined", "content": {}})
        self.assertEqual(self.api("GET", "/v1/requests/" + request["id"], self.owner["token"]), request)

    def test_cancel_is_idempotent_and_invalidates_pending_and_accepted_reviews(self):
        for accepted in (False, True):
            with self.subTest(accepted=accepted):
                request = self.request("ask_human")
                consent = self.consent(request)
                cookie, csrf, _, _ = self.login(consent)
                form = {"_csrf": csrf, "action": "answer", "answer": "Discard this cancelled answer"}
                if accepted:
                    self.browser(consent, "POST", form, cookie)
                    self.assertEqual(self.status(consent)["content"], {"answer": form["answer"]})
                path = "/v1/consents/" + consent["id"] + "/cancel"
                for _ in range(2):
                    self.api("POST", path, self.owner["token"], {})
                    self.assertEqual(self.status(consent), {"id": consent["id"], "status": "declined", "content": {}})
                with self.store.lock:
                    row = self.store.db.execute("SELECT session_hash,content FROM browser_consents WHERE id=?", (consent["id"],)).fetchone()
                self.assertIsNone(row["session_hash"])
                self.assertIn(row["content"], (None, "{}"))
                self.browser(consent, expected=410)
                self.browser(consent, cookie=cookie, expected=410)
                self.browser(consent, "POST", form, cookie, expected=410)
                self.browser(consent, "POST", {"_csrf": csrf, "action": "login", "password": self.SECRET}, expected=410)
                self.assertEqual(self.api("GET", "/v1/requests/" + request["id"], self.owner["token"]), request)

    def test_cancel_requires_exact_owner_and_empty_object_body(self):
        consent = self.consent()
        path = "/v1/consents/" + consent["id"] + "/cancel"
        self.api("POST", path, data={}, expected=401)
        self.api("POST", path, self.sender_owner["token"], {}, 404)
        for token in (self.agent["token"], self.sender["token"], self.ADMIN):
            self.api("POST", path, token, {}, 403)
        for body in ({"decision": "deny"}, {"id": consent["id"]}, [], "cancel", False):
            self.api("POST", path, self.owner["token"], body, 400)
        self.assertEqual(self.status(consent)["status"], "pending")
        self.api("POST", "/v1/consents/" + str(uuid.uuid4()) + "/cancel", self.owner["token"], {}, 404)
        self.api("POST", path, self.owner["token"], {})
        self.assertEqual(self.status(consent)["status"], "declined")

    def test_cancelling_accepted_contact_intent_does_not_create_contacts(self):
        inviter, _, consent = self.contact_consent()
        before = self.api("GET", "/v1/contacts", self.owner["token"])
        cookie, csrf, _, _ = self.login(consent)
        self.browser(consent, "POST", {"_csrf": csrf, "action": "connect"}, cookie)
        self.assertEqual(self.status(consent)["content"], {"decision": "connect"})
        self.api("POST", "/v1/consents/" + consent["id"] + "/cancel", self.owner["token"], {})
        self.assertEqual(self.status(consent)["content"], {})
        self.assertEqual(self.status(consent)["status"], "declined")
        self.browser(consent, "POST", {"_csrf": csrf, "action": "connect"}, cookie, expected=410)
        self.assertEqual(self.api("GET", "/v1/contacts", self.owner["token"]), before)
        self.assertEqual(self.api("GET", "/v1/contacts", inviter["token"]), {"contacts": []})


if __name__ == "__main__":
    unittest.main()
