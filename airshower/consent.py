"""Password-authenticated, one-use browser review for MCP URL elicitation.

The URL contains a public identifier, never an authentication credential. The
browser records a decision; only the authenticated owner client can read it and
apply it through the existing API after rechecking current request authority.
"""
import hashlib
import hmac
import html
from http.cookies import SimpleCookie
import json
import re
import secrets
import time
from urllib.parse import parse_qsl, urlsplit
import uuid

from .errors import APIError, forbidden, invalid, missing

MAX_FORM_BYTES = 16 * 1024
TTL = 120
COOKIE = "airshower_consent"


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ConsentBroker:
    def __init__(self, store, public_url):
        parsed = urlsplit(public_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("Consent public URL must be an HTTPS origin.")
        self.store = store
        self.origin = public_url.rstrip("/")
        self.host = parsed.netloc
        with store.lock:
            store.db.execute("""CREATE TABLE IF NOT EXISTS browser_consents (
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(id),
                kind TEXT NOT NULL, target TEXT NOT NULL, canonical TEXT NOT NULL,
                csrf TEXT NOT NULL, session_hash TEXT, expires_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', content TEXT)""")
            store.db.commit()

    def _canonical_request(self, actor, request_id, recipient_agent_id):
        if not isinstance(request_id, str) or not isinstance(recipient_agent_id, str):
            raise invalid("Request and recipient identifiers are required.")
        row = self.store._request(request_id, actor)
        recipient = self.store._agent(recipient_agent_id)
        if (recipient is None or recipient["owner_id"] != actor["id"]
                or row["recipient_agent_id"] != recipient_agent_id):
            raise forbidden()
        if row["status"] not in ("pending_approval", "waiting_human"):
            raise APIError(409, "conflict", "This request is not awaiting owner review.")
        request = self.store._public_request(row)
        sender = self.store._agent(row["sender_agent_id"])
        owner = self.store.db.execute("SELECT name FROM owners WHERE id=?", (sender["owner_id"],)).fetchone()
        return {key: request[key] for key in (
            "id", "sender_agent_id", "recipient_agent_id", "kind", "message", "source_ids", "status")}, {
                "name": owner["name"], "owner_id": sender["owner_id"], "agent_name": sender["name"]}

    def create(self, actor, data):
        self.store._role(actor, "owner")
        if not isinstance(data, dict):
            raise invalid("Invalid consent request.")
        data = dict(data)
        expires_in = data.pop("expires_in", TTL)
        if type(expires_in) is not int or not 1 <= expires_in <= TTL:
            raise invalid("Consent expires_in must be an integer from 1 to 120 seconds.")
        with self.store.lock:
            credential = self.store.db.execute("SELECT owner_id FROM owner_consent_credentials WHERE owner_id=?", (actor["id"],)).fetchone()
            if credential is None:
                raise forbidden("Browser approval requires an enrollment approval password.")
            if set(data) == {"request_id", "recipient_agent_id"}:
                request, requester = self._canonical_request(actor, data["request_id"], data["recipient_agent_id"])
                kind, target = "request", request["id"]
                sources = self.store._request_sources(actor["id"], request["source_ids"], request["kind"])
                canonical = {"request": request, "requester": requester,
                             "sources": [{"id": source["id"], "label": source["label"]} for source in sources]}
            elif set(data) == {"contact_invite"}:
                digest = self.store._invite_hash(data["contact_invite"])
                row = self.store.db.execute("""SELECT i.owner_id,o.name FROM contact_invites i JOIN owners o ON o.id=i.owner_id
                    WHERE i.token_hash=? AND i.consumed_at IS NULL AND i.expires_at>?""", (digest, time.time())).fetchone()
                if row is None:
                    raise missing()
                if row["owner_id"] == actor["id"]:
                    raise invalid("You cannot connect to yourself.")
                kind, target = "contact", digest
                canonical = {"owner_id": row["owner_id"], "name": row["name"]}
            else:
                raise invalid("Consent must identify exactly one request or contact invitation.")
            identifier, expiry = str(uuid.uuid4()), time.time() + expires_in
            self.store.db.execute("""INSERT INTO browser_consents
                (id,owner_id,kind,target,canonical,csrf,expires_at) VALUES(?,?,?,?,?,?,?)""",
                (identifier, actor["id"], kind, target, json.dumps(canonical), secrets.token_urlsafe(32), expiry))
            self.store.db.commit()
            return {"id": identifier, "url": self.origin + "/consent/" + identifier, "expires_at": expiry}

    def _row(self, identifier):
        row = self.store.db.execute("SELECT * FROM browser_consents WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise missing()
        return row

    def status(self, actor, identifier):
        self.store._role(actor, "owner")
        with self.store.lock:
            row = self._row(identifier)
            if row["owner_id"] != actor["id"]:
                raise missing()
            status = row["status"]
            if row["expires_at"] <= time.time():
                status = "declined"
            return {"id": identifier, "status": status,
                    "content": json.loads(row["content"]) if status == "accepted" else {}}

    def cancel(self, actor, identifier, data):
        """Close a host-abandoned approval, including an unapplied response."""
        self.store._role(actor, "owner")
        if data != {}:
            raise invalid("Consent cancellation does not accept fields.")
        with self.store.lock:
            row = self._row(identifier)
            if row["owner_id"] != actor["id"]:
                raise missing()
            self.store.db.execute("UPDATE browser_consents SET status='declined',content=NULL,session_hash=NULL WHERE id=?", (identifier,))
            self.store.db.commit()
            return {"id": identifier, "status": "declined", "content": {}}

    @staticmethod
    def _session(row, cookie_header):
        try:
            cookies = SimpleCookie()
            cookies.load(cookie_header or "")
            value = cookies[COOKIE].value
            return bool(row["session_hash"] and hmac.compare_digest(_hash(value), row["session_hash"]))
        except (KeyError, ValueError):
            return False

    @staticmethod
    def _document(title, description, form):
        esc = html.escape
        footer = ("Your choice applies only to this request. You can close this page to leave it unapproved."
                  if form else "Your agent will check the request before continuing.")
        return ("<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>Airshower review</title><style>"
                "*{box-sizing:border-box}body{margin:0;padding:36px 18px;background:#0d1922;color:#edf5f2;font:15px -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif}"
                "main{max-width:700px;margin:auto;padding:28px;background:#142630;border:1px solid #36505a;border-radius:18px}"
                ".brand{color:#98e7c2;letter-spacing:.16em;font-size:12px;font-weight:700}h1{font-size:30px;letter-spacing:-.025em}"
                "pre,blockquote{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;line-height:1.6;color:#c6d8d8}blockquote{margin:20px 0;padding:16px;border-left:3px solid #98e7c2;background:#0d1922;border-radius:6px}label{display:block;margin:20px 0 8px}"
                "input,textarea{display:block;width:100%;padding:12px;background:#0d1922;color:#edf5f2;border:1px solid #496670;border-radius:8px;font:inherit}"
                "textarea{min-height:130px}button{margin:20px 8px 0 0;padding:11px 20px;border:1px solid #98e7c2;border-radius:8px;background:#98e7c2;color:#102d25;font:inherit;font-weight:600}"
                "button.secondary{background:transparent;color:#edf5f2}footer{font-size:12px;line-height:1.6;color:#9bb1b5;margin-top:24px}</style>"
                "<main><p class='brand'>AIRSHOWER</p><h1>" + esc(title) + "</h1><pre>" + esc(description) + "</pre>" + form +
                "<footer>" + footer + "</footer></main></html>")

    def _page(self, row, authenticated):
        path = "/consent/" + row["id"]
        hidden = "<input type='hidden' name='_csrf' value='" + html.escape(row["csrf"]) + "'>"
        if not authenticated:
            form = ("<form method='post' action='" + path + "'>" + hidden +
                    "<label for='password'>Your Airshower approval password</label><input id='password' type='password' name='password' required maxlength='128' autocomplete='current-password'>"
                    "<button name='action' value='login'>Sign in to review</button></form>")
            return self._document("Sign in to review", "Enter the approval password you chose when connecting Airshower. No request details are shown until you sign in.", form)
        canonical = json.loads(row["canonical"])
        details = ""
        if row["kind"] == "contact":
            title = "Connect with " + canonical["name"] + "?"
            description = "You will be able to send requests to each other's agents. Connecting does not share files or allow automatic actions. Each person's permissions still apply."
            controls = "<button name='action' value='connect'>Connect</button><button class='secondary' name='action' value='decline'>Decline</button>"
        else:
            request = canonical["request"]
            title = "Your answer" if request["kind"] == "ask_human" else "Review this request"
            requester = canonical["requester"]
            kinds = {"task": "Task request", "message": "Message", "ask_human": "Question for you",
                     "file": "File request", "context": "Context request"}
            description = ("From " + requester["name"] + " through " + requester["agent_name"] +
                           "\n" + kinds.get(request["kind"], "Request"))
            details = "<blockquote>" + html.escape(request["message"]) + "</blockquote>"
            sources = canonical.get("sources", [])
            if sources:
                details += "<p>This request covers " + str(len(sources)) + " selected source(s):</p><ul>" + "".join("<li>" + html.escape(source["label"]) + "</li>" for source in sources) + "</ul>"
            if request["kind"] != "ask_human":
                details += "<p>Approving lets your agent handle this request once. This does not change your rules for future requests.</p>"
            else:
                details += "<p>Your answer will be sent back as a reply from you.</p>"
            if request["kind"] == "ask_human":
                controls = "<label for='answer'>Answer to send</label><textarea id='answer' name='answer' maxlength='8192' required></textarea><button name='action' value='answer'>Send my answer</button><button class='secondary' name='action' value='decline' formnovalidate>Cancel</button>"
            else:
                controls = "<button name='action' value='approve'>Approve once</button><button class='secondary' name='action' value='deny'>Deny</button><button class='secondary' name='action' value='decline'>Cancel</button>"
        return self._document(title, description, details + "<form method='post' action='" + path + "'>" + hidden + controls + "</form>")

    def _login(self, row, password):
        if not callable(getattr(hashlib, "scrypt", None)):
            raise APIError(503, "consent_unavailable", "This server runtime cannot verify approval passwords.")
        credential = self.store.db.execute("SELECT * FROM owner_consent_credentials WHERE owner_id=?", (row["owner_id"],)).fetchone()
        now = time.time()
        if credential is None or credential["blocked_until"] > now:
            raise APIError(429, "try_later", "Approval sign-in is temporarily unavailable. Try again later.")
        if not isinstance(password, str) or len(password) > 128 or len(password.encode("utf-8")) > 512:
            raise forbidden("The approval password was not accepted.")
        digest = hashlib.scrypt(password.encode("utf-8"), salt=credential["salt"], n=16384, r=8, p=1, dklen=32)
        if not hmac.compare_digest(digest, credential["password_hash"]):
            attempts = (0 if credential["blocked_until"] and credential["blocked_until"] <= now else credential["failed_attempts"]) + 1
            self.store.db.execute("UPDATE owner_consent_credentials SET failed_attempts=?,blocked_until=? WHERE owner_id=?",
                                 (attempts, now + 60 if attempts >= 5 else 0, row["owner_id"]))
            self.store.db.commit()
            raise forbidden("The approval password was not accepted.")
        if row["expires_at"] <= time.time():
            raise APIError(410, "closed", "This review has closed.")
        session = secrets.token_urlsafe(32)
        self.store.db.execute("UPDATE owner_consent_credentials SET failed_attempts=0,blocked_until=0 WHERE owner_id=?", (row["owner_id"],))
        self.store.db.execute("UPDATE browser_consents SET session_hash=?,csrf=? WHERE id=?",
                             (_hash(session), secrets.token_urlsafe(32), row["id"]))
        self.store.db.commit()
        return session

    def _record(self, row, form):
        action = form.get("action")
        if set(form) - {"_csrf", "action", "answer"}:
            raise invalid("Invalid consent form fields.")
        canonical = json.loads(row["canonical"])
        content = {}
        if action == "decline":
            status = "declined"
        elif row["kind"] == "contact" and action == "connect" and "answer" not in form:
            # Ensure the invitation still exists and has the same owner.
            invite = self.store.db.execute("SELECT owner_id FROM contact_invites WHERE token_hash=? AND consumed_at IS NULL AND expires_at>?", (row["target"], time.time())).fetchone()
            if invite is None or invite["owner_id"] != canonical["owner_id"]:
                raise APIError(409, "conflict", "The contact invitation is no longer available.")
            status, content = "accepted", {"decision": "connect"}
        elif row["kind"] == "request":
            request, _ = self._canonical_request({"id": row["owner_id"], "role": "owner"}, row["target"], canonical["request"]["recipient_agent_id"])
            if request != canonical["request"]:
                raise APIError(409, "conflict", "The request changed while awaiting review.")
            if request["kind"] == "ask_human" and action == "answer":
                answer = form.get("answer")
                if not isinstance(answer, str) or not answer.strip() or len(answer.encode("utf-8")) > 8192:
                    raise invalid("Enter an answer within the size limit.")
                status, content = "accepted", {"answer": answer}
            elif request["kind"] != "ask_human" and action in ("approve", "deny") and "answer" not in form:
                status, content = "accepted", {"decision": action}
            else:
                raise invalid("Invalid review action.")
        else:
            raise invalid("Invalid review action.")
        updated = self.store.db.execute("UPDATE browser_consents SET status=?,content=?,session_hash=NULL WHERE id=? AND status='pending' AND expires_at>?",
                                        (status, json.dumps(content), row["id"], time.time()))
        if not updated.rowcount:
            raise APIError(409, "conflict", "This consent has already closed.")
        self.store.db.commit()

    def handle_public(self, handler, path):
        identifier = path[len("/consent/"):]
        if not re.fullmatch(r"[a-f0-9-]{36}", identifier) or handler.headers.get_all("Host", []) != [self.host]:
            raise missing()
        if handler.headers.get("Transfer-Encoding") is not None:
            raise invalid("Chunked forms are not accepted.")
        if len(handler.headers.get_all("Cookie", [])) > 1:
            raise invalid("Only one session cookie header is accepted.")
        cookie = handler.headers.get("Cookie", "")
        with self.store.lock:
            row = self._row(identifier)
            if row["status"] != "pending" or row["expires_at"] <= time.time():
                raise APIError(410, "closed", "This review has closed.")
        form = None
        if handler.command == "POST":
            if handler.headers.get_all("Origin", []) != [self.origin]:
                raise forbidden("The browser origin is not permitted.")
            types = handler.headers.get_all("Content-Type", [])
            if len(types) != 1 or types[0].split(";", 1)[0].lower().strip() != "application/x-www-form-urlencoded":
                raise APIError(415, "unsupported_media_type", "Use a browser form.")
            lengths = handler.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,8}", lengths[0]):
                raise invalid("A valid form size is required.")
            size = int(lengths[0])
            if size > MAX_FORM_BYTES:
                raise APIError(413, "body_too_large", "The form is too large.")
            try:
                raw = handler.rfile.read(size)
                if len(raw) != size:
                    raise ValueError()
                raw = raw.decode("utf-8")
                if re.search(r"%(?![0-9a-fA-F]{2})", raw):
                    raise ValueError()
                pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=5, encoding="utf-8", errors="strict")
                form = dict(pairs)
                if len(form) != len(pairs):
                    raise ValueError()
            except (ValueError, UnicodeError):
                raise invalid("Invalid browser form.")
        elif handler.command != "GET":
            raise APIError(405, "method_not_allowed", "Unsupported consent method.")
        elif handler.headers.get("Content-Length", "0") != "0":
            raise invalid("GET does not accept a body.")
        set_cookie = None
        with self.store.lock:
            row = self._row(identifier)
            if row["status"] != "pending" or row["expires_at"] <= time.time():
                raise APIError(410, "closed", "This review has closed.")
            authenticated = self._session(row, cookie)
            if form is not None:
                if (not isinstance(form.get("_csrf"), str)
                        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", form["_csrf"])
                        or not hmac.compare_digest(form["_csrf"], row["csrf"])):
                    raise forbidden("The browser form could not be verified.")
                if form.get("action") == "login":
                    if set(form) != {"_csrf", "action", "password"}:
                        raise invalid("Invalid sign-in form.")
                    session = self._login(row, form["password"])
                    set_cookie = COOKIE + "=" + session + "; Path=/consent/" + identifier + "; Secure; HttpOnly; SameSite=Strict; Max-Age=" + str(TTL)
                    row = self._row(identifier)
                    page = self._page(row, True)
                else:
                    if not authenticated:
                        raise forbidden("Sign in before reviewing this request.")
                    self._record(row, form)
                    page = self._document("Response recorded", "Return to your agent chat to check whether your response was applied. Recording your choice does not itself complete the request. You can close this page.", "")
                    set_cookie = COOKIE + "=; Path=/consent/" + identifier + "; Secure; HttpOnly; SameSite=Strict; Max-Age=0"
            else:
                page = self._page(row, authenticated)
        body = page.encode("utf-8")
        handler.close_connection = True
        handler.send_response(200)
        for key, value in {"Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(body)),
                           "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
                           "X-Frame-Options": "DENY", "Cross-Origin-Resource-Policy": "same-origin",
                           "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
                           "Connection": "close"}.items():
            handler.send_header(key, value)
        if set_cookie:
            handler.send_header("Set-Cookie", set_cookie)
        handler.end_headers()
        handler.wfile.write(body)
