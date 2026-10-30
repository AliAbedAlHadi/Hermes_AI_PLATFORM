"""Server-side conversation continuity for the console chat and the customer portal.

A conversation belongs to one principal (the owner, or one customer) and one agent, fixed when it is
created. History sent to the agent is rebuilt from this store, never taken from the browser, so a client
cannot invent "agent" turns. A conversation id from another principal, or for another agent, behaves exactly
like an unknown id (no existence leak). Each user message carries a client message id: a repeated delivery
returns the stored reply instead of running the agent again.

Separation: this store keeps chat turns only. Long-term memory stays in memory.db, task state in
workflow.db, audit in memory.db's audit table; message text is never written to logs or the audit trail.
"""
import secrets
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,                 -- conv_xxxxxxxxxxxx
  kind TEXT NOT NULL,                  -- owner, customer
  principal TEXT NOT NULL,             -- 'owner' or a cus_ id
  agent TEXT NOT NULL,                 -- fixed agent profile
  company TEXT NOT NULL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS conversations_principal ON conversations(kind, principal, agent, updated_at);
CREATE TABLE IF NOT EXISTS messages (
  conversation TEXT NOT NULL REFERENCES conversations(id),
  seq INTEGER NOT NULL,
  message_key TEXT NOT NULL,           -- client id of the user message this turn answers
  role TEXT NOT NULL,                  -- user, agent
  text TEXT NOT NULL,
  state TEXT NOT NULL,                 -- pending, done, failed
  ts REAL NOT NULL,
  PRIMARY KEY (conversation, seq),
  UNIQUE (conversation, message_key, role)
);
"""
HISTORY_TURNS = 8
MAX_TEXT = 8000


class ConversationError(Exception):
    """Not found (or not yours), or a request the store refuses."""


class Busy(ConversationError):
    """The same message is still being answered."""


class Conversations:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as c:
            c.executescript(SCHEMA)

    def _db(self):
        c = sqlite3.connect(str(self.path), timeout=10)
        c.execute("PRAGMA busy_timeout=10000")
        c.row_factory = sqlite3.Row
        return c

    def open(self, kind, principal, agent, company, conversation_id=None):
        """The caller's conversation with this agent: the given id if it is theirs, else a new one."""
        with self.lock, self._db() as c:
            if conversation_id:
                r = c.execute("SELECT id FROM conversations WHERE id=? AND kind=? AND principal=? AND agent=?",
                              (conversation_id, kind, principal, agent)).fetchone()
                if r is None:
                    raise ConversationError("conversation not found")
                return r["id"]
            cid, now = "conv_" + secrets.token_hex(6), time.time()
            c.execute("INSERT INTO conversations VALUES (?,?,?,?,?,?,?)", (cid, kind, principal, agent, company, now, now))
            return cid

    def latest(self, kind, principal, agent):
        with self._db() as c:
            r = c.execute("SELECT id FROM conversations WHERE kind=? AND principal=? AND agent=? ORDER BY updated_at "
                          "DESC LIMIT 1", (kind, principal, agent)).fetchone()
        return r["id"] if r else None

    def transcript(self, kind, principal, conversation_id):
        """Messages of a conversation the caller owns; corrupt or half-written rows are skipped, not fatal."""
        with self._db() as c:
            conv = c.execute("SELECT * FROM conversations WHERE id=? AND kind=? AND principal=?",
                             (conversation_id, kind, principal)).fetchone()
            if conv is None:
                raise ConversationError("conversation not found")
            rows = c.execute("SELECT role, text, state, message_key, ts FROM messages WHERE conversation=? ORDER BY seq",
                             (conversation_id,)).fetchall()
        msgs = [dict(r) for r in rows if r["role"] in ("user", "agent") and isinstance(r["text"], str)]
        return {"id": conv["id"], "agent": conv["agent"], "messages": msgs}

    def history(self, conversation_id):
        """Last complete turns (user message answered by the agent), oldest first."""
        with self._db() as c:
            rows = c.execute("SELECT message_key, role, text FROM messages WHERE conversation=? AND state='done' "
                             "ORDER BY seq", (conversation_id,)).fetchall()
        turns, by_key = [], {}
        for r in rows:
            if not isinstance(r["text"], str):
                continue
            by_key.setdefault(r["message_key"], {})[r["role"]] = r["text"]
        for key, t in by_key.items():
            if "user" in t and "agent" in t:
                turns += [("user", t["user"]), ("agent", t["agent"])]
        return turns[-2 * HISTORY_TURNS:]

    def begin(self, conversation_id, message_key, text):
        """Record a user message. Returns ('new', None), ('done', stored reply) for a repeat, or raises Busy."""
        if not (isinstance(message_key, str) and 8 <= len(message_key) <= 64):
            raise ConversationError("message id required")
        text = (text or "").strip()[:MAX_TEXT]
        if not text:
            raise ConversationError("message required")
        with self.lock, self._db() as c:
            prev = c.execute("SELECT role, text, state FROM messages WHERE conversation=? AND message_key=?",
                             (conversation_id, message_key)).fetchall()
            states = {r["role"]: r for r in prev}
            if "agent" in states and states["agent"]["state"] == "done":
                return "done", states["agent"]["text"]
            if "user" in states and states["user"]["state"] == "pending":
                raise Busy("this message is still being answered")
            if "user" in states:  # a failed earlier attempt: retry it
                c.execute("UPDATE messages SET state='pending', ts=? WHERE conversation=? AND message_key=? AND role='user'",
                          (time.time(), conversation_id, message_key))
                return "new", None
            seq = (c.execute("SELECT COALESCE(MAX(seq), 0) FROM messages WHERE conversation=?",
                             (conversation_id,)).fetchone()[0]) + 1
            c.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?)",
                      (conversation_id, seq, message_key, "user", text, "pending", time.time()))
            c.execute("UPDATE conversations SET updated_at=? WHERE id=?", (time.time(), conversation_id))
        return "new", None

    def finish(self, conversation_id, message_key, reply, ok):
        with self.lock, self._db() as c:
            if not ok:
                c.execute("UPDATE messages SET state='failed' WHERE conversation=? AND message_key=? AND role='user'",
                          (conversation_id, message_key))
                return
            seq = (c.execute("SELECT COALESCE(MAX(seq), 0) FROM messages WHERE conversation=?",
                             (conversation_id,)).fetchone()[0]) + 1
            c.execute("UPDATE messages SET state='done' WHERE conversation=? AND message_key=? AND role='user'",
                      (conversation_id, message_key))
            c.execute("INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?)",
                      (conversation_id, seq, message_key, "agent", (reply or "")[:MAX_TEXT], "done", time.time()))
            c.execute("UPDATE conversations SET updated_at=? WHERE id=?", (time.time(), conversation_id))

    def recover_pending(self, older_than=900):
        """After a crash or restart: messages left pending longer than `older_than` become failed (retryable)."""
        with self.lock, self._db() as c:
            return c.execute("UPDATE messages SET state='failed' WHERE state='pending' AND ts < ?",
                             (time.time() - older_than,)).rowcount

    def check(self):
        try:
            with self._db() as c:
                return c.execute("PRAGMA quick_check").fetchone()[0]
        except sqlite3.Error as e:
            return f"error: {type(e).__name__}"
