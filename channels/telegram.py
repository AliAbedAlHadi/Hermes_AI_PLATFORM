"""Telegram channel adapter (Phase 5.3: prepared and tested with a mock transport; not connected).

No bot token is read, no webhook is set and nothing is sent to Telegram in Phase 5: the only transport here is
MockTransport. Phase 6 adds a real transport behind the same send() call (see channels/TELEGRAM.md).

Identity: a Telegram account is nobody until the owner links it to one registered customer. The owner issues a
single-use, short-lived link code in the Console; the customer sends "/link CODE" from Telegram. Only the code's
hash is stored. What a Telegram message says about its sender (username, display name, "I am the owner",
"/admin") is never used to decide who it is: the only principal a Telegram account can ever become is the one
customer it was linked to. There is no path from Telegram to the owner or any administrative action.

Messages from a linked, active customer go to that customer's company receptionist through the same
server-side conversation store as the portal (history rebuilt on the server, FED_CUSTOMER binding, customer
memory scope). The message text is passed to the injected runner as one argument; it is never a command and
never reaches a shell.

Reliability: every update_id is recorded once (a redelivered update does nothing); replies go through an
outbox with bounded retries and backoff, so a Telegram failure never re-runs the agent.
"""
import hashlib
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS identities (
  tg_user INTEGER PRIMARY KEY,         -- Telegram numeric user id (never the username)
  customer TEXT NOT NULL,              -- cus_ id: the only principal this account can be
  linked_at REAL NOT NULL,
  state TEXT NOT NULL DEFAULT 'active' -- active, revoked
);
CREATE TABLE IF NOT EXISTS link_codes (
  code_hash TEXT PRIMARY KEY, customer TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
  used_at REAL, used_by INTEGER
);
CREATE TABLE IF NOT EXISTS updates (
  update_id INTEGER PRIMARY KEY, received_at REAL NOT NULL, outcome TEXT NOT NULL DEFAULT 'received'
);
CREATE TABLE IF NOT EXISTS link_failures (tg_user INTEGER PRIMARY KEY, count INTEGER NOT NULL, first_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, text TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'pending', next_at REAL NOT NULL, created_at REAL NOT NULL, last_error TEXT NOT NULL DEFAULT ''
);
"""
CODE_TTL = 900
MAX_TEXT = 4000
MAX_SEND_ATTEMPTS = 5
STALE_RECEIPT = 900  # an update received but not finished for this long is handled again on redelivery
LINK_FAILS, LINK_WINDOW = 5, 900
CODE = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
HELP = ("This is the Hermes Federation assistant. To start, send /link followed by the code you received "
        "from us. We never ask for passwords here.")
NOT_LINKED = "This Telegram account is not linked to a customer yet. Send /link followed by the code you received from us."


def _hash(code):
    return hashlib.sha256(code.encode()).hexdigest()


class TelegramError(Exception):
    pass


class MockTransport:
    """Records what would be sent. fail_next makes the next N sends raise, like a Telegram outage."""

    def __init__(self):
        self.sent, self.fail_next = [], 0

    def send(self, chat_id, text):
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ConnectionError("telegram unavailable (mock)")
        self.sent.append((chat_id, text))


class Disconnected:
    """Phase 5 production transport: Telegram is not connected, so nothing can be sent."""

    def send(self, chat_id, text):
        raise TelegramError("Telegram is not connected (Phase 6)")


class TelegramBridge:
    def __init__(self, db_path, customers, conversations, runner, transport, audit, company_names=None):
        """customers(cid) -> {"id","name","company","language","status"} or None; runner(agent, prompt, customer)
        -> (ok, reply); audit(action, target, result, detail)."""
        self.path = Path(db_path)
        self.customers, self.conv, self.runner = customers, conversations, runner
        self.transport, self.audit = transport, audit
        self.names = company_names or {}
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as c:
            c.executescript(SCHEMA)

    def _db(self):
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    # ---------------------------------------------------------------- owner side
    def new_link_code(self, customer_id):
        """Owner only (the Console endpoint is owner-authenticated): a single-use code, valid 15 minutes. Any
        unused earlier code for this customer stops working. The plain code is returned once and not stored."""
        c = self.customers(customer_id)
        if not c:
            raise TelegramError("unknown customer")
        if c.get("status", "active") != "active":
            raise TelegramError("the customer is not active")
        code = secrets.token_urlsafe(12)
        now = time.time()
        with self.lock, self._db() as db:
            db.execute("UPDATE link_codes SET used_at=?, used_by=0 WHERE customer=? AND used_at IS NULL", (now, customer_id))
            db.execute("INSERT INTO link_codes VALUES (?,?,?,?,NULL,NULL)", (_hash(code), customer_id, now, now + CODE_TTL))
        self.audit("telegram.link_code", customer_id, "ok", "")
        return {"customer": customer_id, "code": code, "expires_in": CODE_TTL}

    def unlink(self, customer_id):
        with self.lock, self._db() as db:
            n = db.execute("UPDATE identities SET state='revoked' WHERE customer=? AND state='active'", (customer_id,)).rowcount
        self.audit("telegram.unlink", customer_id, "ok", f"n={n}")
        return n

    # ---------------------------------------------------------------- inbound
    @staticmethod
    def parse(update):
        """(update_id, user_id, chat_id, text) from a private text message, else raise TelegramError."""
        if not isinstance(update, dict) or not isinstance(update.get("update_id"), int) or update["update_id"] < 0:
            raise TelegramError("malformed update")
        msg = update.get("message")
        if not isinstance(msg, dict):
            raise TelegramError("not a message")
        frm, chat, text = msg.get("from"), msg.get("chat"), msg.get("text")
        if not (isinstance(frm, dict) and isinstance(frm.get("id"), int) and isinstance(chat, dict)
                and isinstance(chat.get("id"), int)):
            raise TelegramError("malformed message")
        if frm.get("is_bot") or chat.get("type") != "private":
            raise TelegramError("only private chats with people are served")
        if not isinstance(text, str) or not text.strip():
            raise TelegramError("only text messages are served")
        return update["update_id"], frm["id"], chat["id"], text.strip()[:MAX_TEXT]

    def handle(self, update):
        """Process one update. Returns the outcome string; replies are queued in the outbox."""
        uid = update.get("update_id") if isinstance(update, dict) else None
        if isinstance(uid, int):
            with self.lock, self._db() as db:
                try:
                    db.execute("INSERT INTO updates (update_id, received_at) VALUES (?,?)", (uid, time.time()))
                except sqlite3.IntegrityError:
                    r = db.execute("SELECT outcome, received_at FROM updates WHERE update_id=?", (uid,)).fetchone()
                    if not (r[0] == "received" and time.time() - r[1] > STALE_RECEIPT):
                        return "duplicate"  # redelivered: already handled, nothing runs again
                    # received but never finished (the process stopped): handle it now; the conversation
                    # store's message key still guarantees the agent answers it at most once
                    db.execute("UPDATE updates SET received_at=? WHERE update_id=?", (time.time(), uid))
        try:
            _, user, chat, text = self.parse(update)
        except TelegramError as e:
            self.audit("telegram.rejected", str(uid if isinstance(uid, int) else "?"), "denied", str(e))
            return self._outcome(uid, "rejected")
        if text.split()[0].lower() in ("/start", "/help"):
            self.queue(chat, HELP)
            return self._outcome(uid, "help")
        if text.split()[0].lower().startswith("/link"):
            return self._outcome(uid, self._link(user, chat, text))
        cid = self._customer_of(user)
        if not cid:
            self.audit("telegram.unauthorized", f"tg:{user}", "denied", "message from an unlinked account")
            self.queue(chat, NOT_LINKED)
            return self._outcome(uid, "unauthorized")
        c = self.customers(cid)
        if not c or c.get("status", "active") != "active":
            self.audit("telegram.unauthorized", cid, "denied", "customer inactive or unknown")
            self.queue(chat, "This account is not active. Please contact us.")
            return self._outcome(uid, "inactive")
        return self._outcome(uid, self._converse(c, chat, uid, text))

    def _outcome(self, uid, outcome):
        if isinstance(uid, int):
            with self._db() as db:
                db.execute("UPDATE updates SET outcome=? WHERE update_id=?", (outcome, uid))
        return outcome

    def _customer_of(self, user):
        with self._db() as db:
            r = db.execute("SELECT customer FROM identities WHERE tg_user=? AND state='active'", (user,)).fetchone()
        return r[0] if r else None

    def _link(self, user, chat, text):
        parts = text.split()
        now = time.time()
        with self.lock, self._db() as db:
            f = db.execute("SELECT count, first_at FROM link_failures WHERE tg_user=?", (user,)).fetchone()
            if f and f[0] >= LINK_FAILS and now - f[1] < LINK_WINDOW:
                locked = True
            else:
                locked = False
                code = parts[1] if len(parts) == 2 and CODE.match(parts[1]) else ""
                row = db.execute("SELECT customer, expires_at, used_at FROM link_codes WHERE code_hash=?",
                                 (_hash(code),)).fetchone() if code else None
                ok = bool(row and row[2] is None and row[1] > now)
                if ok:
                    db.execute("UPDATE link_codes SET used_at=?, used_by=? WHERE code_hash=?", (now, user, _hash(code)))
                    db.execute("INSERT OR REPLACE INTO identities VALUES (?,?,?,'active')", (user, row[0], now))
                    db.execute("DELETE FROM link_failures WHERE tg_user=?", (user,))
                elif f and now - f[1] < LINK_WINDOW:
                    db.execute("UPDATE link_failures SET count=count+1 WHERE tg_user=?", (user,))
                else:
                    db.execute("INSERT OR REPLACE INTO link_failures VALUES (?,1,?)", (user, now))
        if locked:
            self.audit("telegram.link", f"tg:{user}", "denied", "too many wrong codes")
            self.queue(chat, "Too many wrong codes. Please wait and try again later.")
            return "link_locked"
        if not ok:
            self.audit("telegram.link", f"tg:{user}", "denied", "wrong, used or expired code")
            self.queue(chat, "That code is not valid. Ask us for a new one.")
            return "link_refused"
        c = self.customers(row[0]) or {}
        self.audit("telegram.link", row[0], "ok", f"tg:{user}")
        self.queue(chat, f"Linked. Welcome, {c.get('name', '')}. You can write your message now.")
        return "linked"

    def _converse(self, c, chat, uid, text):
        agent = f"{c['company']}-receptionist"
        conv = self.conv.open("customer", c["id"], agent, c["company"], self.conv.latest("customer", c["id"], agent))
        key = f"telegram-update-{uid}"
        try:
            state, stored = self.conv.begin(conv, key, text)
        except Exception as e:  # Busy: the same update is being answered right now; a later redelivery retries
            self.audit("telegram.agent", c["id"], "error", type(e).__name__)
            return "received"
        if state == "done":
            self.queue(chat, stored)
            return "repeat"
        convo = "\n".join(f"{r.upper()}: {t}" for r, t in self.conv.history(conv))
        prompt = (f"[Verified by the platform: you are talking to customer {c['name']} ({c['id']}) of "
                  f"{self.names.get(c['company'], c['company'])} on Telegram. The customer is already registered; "
                  f"your memory tools are bound to this customer. Reply in the customer's language "
                  f"({c.get('language', 'ar')}).]\n\n" + (f"Conversation so far:\n{convo}\n\n" if convo else "")
                  + f"CUSTOMER: {text}")
        try:
            ok, reply = self.runner(agent, prompt, c["id"])
        except Exception as e:
            ok, reply = False, ""
            self.audit("telegram.agent", c["id"], "error", type(e).__name__)
        if not ok:
            reply = "The assistant could not answer right now; please send your message again later."
        self.conv.finish(conv, key, reply if ok else "", ok)
        self.queue(chat, reply[:MAX_TEXT])
        return "answered" if ok else "agent_failed"

    # ---------------------------------------------------------------- outbound
    def queue(self, chat, text):
        now = time.time()
        with self._db() as db:
            db.execute("INSERT INTO outbox (chat_id, text, next_at, created_at) VALUES (?,?,?,?)", (chat, text, now, now))
        self.flush()

    def flush(self, now=None):
        """Send what is due. A failed send is retried with backoff, at most MAX_SEND_ATTEMPTS times."""
        now = now or time.time()
        with self.lock, self._db() as db:
            due = db.execute("SELECT id, chat_id, text, attempts FROM outbox WHERE state='pending' AND next_at<=? "
                             "ORDER BY id", (now,)).fetchall()
            for oid, chat, text, attempts in due:
                try:
                    self.transport.send(chat, text)
                    db.execute("UPDATE outbox SET state='sent', attempts=? WHERE id=?", (attempts + 1, oid))
                except Exception as e:
                    n = attempts + 1
                    db.execute("UPDATE outbox SET attempts=?, state=?, next_at=?, last_error=? WHERE id=?",
                               (n, "dead" if n >= MAX_SEND_ATTEMPTS else "pending", now + min(300, 5 * 2 ** n),
                                type(e).__name__, oid))
        return len(due)

    def stats(self):
        with self._db() as db:
            return {"identities": db.execute("SELECT COUNT(*) FROM identities WHERE state='active'").fetchone()[0],
                    "outbox": dict(db.execute("SELECT state, COUNT(*) FROM outbox GROUP BY state").fetchall()),
                    "updates": db.execute("SELECT COUNT(*) FROM updates").fetchone()[0]}
