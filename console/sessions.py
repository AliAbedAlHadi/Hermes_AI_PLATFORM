"""Persistent, revocable login sessions for the console (owner) and the customer portal.

Policy:
  * A session is a random 256-bit token in an HttpOnly, SameSite=Strict cookie. Only its SHA-256 is stored,
    so a copy of the database cannot be replayed as a cookie. Nothing is signed, so there is no signing
    secret to lose or regenerate on restart.
  * Every login mints a new token (no fixation: a cookie set before login is never promoted).
  * Absolute lifetime and idle timeout bound every session (owner 12 h / 2 h idle, customer 8 h / 1 h idle;
    FED_SESSION_TTL_* / FED_SESSION_IDLE_* override for tests).
  * A session is bound to the principal's credential version: changing the owner password, or issuing a new
    customer login code, invalidates every older session at its next request.
  * Logout revokes server-side; the owner can list and revoke sessions. Revocation is immediate.
  * If the store cannot be read, requests fail closed (SessionStoreError -> HTTP 503), never open.
  * Failed sign-ins are throttled persistently (survives restarts): 5 failures within 15 minutes lock the key
    for 60 s, doubling on each repeated lock up to 15 minutes (never permanent). A success clears it.
"""
import hashlib
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                 -- owner, customer
  principal TEXT NOT NULL,            -- 'owner' or a cus_ id
  cred_version TEXT NOT NULL,         -- credential the session was issued under
  created_at REAL NOT NULL, expires_at REAL NOT NULL, idle_timeout REAL NOT NULL, last_seen REAL NOT NULL,
  revoked_at REAL, revoke_reason TEXT
);
CREATE INDEX IF NOT EXISTS sessions_principal ON sessions(kind, principal);
CREATE TABLE IF NOT EXISTS throttle (
  key TEXT PRIMARY KEY,               -- 'owner' or 'portal:<client address>'
  failures INTEGER NOT NULL, window_start REAL NOT NULL,
  locked_until REAL NOT NULL DEFAULT 0, lock_count INTEGER NOT NULL DEFAULT 0
);
"""
THROTTLE_LIMIT, THROTTLE_WINDOW, LOCK_CAP = 5, 900, 900
LOCK_BASE = float(os.environ.get("FED_THROTTLE_LOCK_BASE", 60))  # tests shorten it; production keeps 60 s
POLICY = {"owner": (12 * 3600, 2 * 3600), "customer": (8 * 3600, 3600)}


class SessionStoreError(Exception):
    """The session store is unavailable; the caller must refuse the request."""


class Locked(Exception):
    """Too many failed sign-ins for this key; try again after `seconds`."""

    def __init__(self, seconds):
        super().__init__(f"too many attempts; try again in {int(seconds) + 1} s")
        self.seconds = seconds


def _hash(token):
    return hashlib.sha256(token.encode("ascii", "replace")).hexdigest()


def _valid_shape(token):
    return isinstance(token, str) and 20 <= len(token) <= 128 and all(c.isalnum() or c in "-_" for c in token)


class SessionStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._db() as c:
                c.executescript(SCHEMA)
        except (sqlite3.Error, OSError) as e:
            raise SessionStoreError(f"session store unavailable: {type(e).__name__}") from None

    def _db(self):
        c = sqlite3.connect(str(self.path), timeout=10)
        c.execute("PRAGMA busy_timeout=10000")
        return c

    @staticmethod
    def policy(kind):
        ttl, idle = POLICY[kind]
        return (float(os.environ.get(f"FED_SESSION_TTL_{kind.upper()}", ttl)),
                float(os.environ.get(f"FED_SESSION_IDLE_{kind.upper()}", idle)))

    def create(self, kind, principal, cred_version):
        ttl, idle = self.policy(kind)
        token, now = secrets.token_urlsafe(32), time.time()
        try:
            with self.lock, self._db() as c:
                c.execute("INSERT INTO sessions (token_hash, kind, principal, cred_version, created_at, expires_at, "
                          "idle_timeout, last_seen) VALUES (?,?,?,?,?,?,?,?)",
                          (_hash(token), kind, principal, str(cred_version), now, now + ttl, idle, now))
                # Housekeeping: forget sessions that ended more than a day ago.
                c.execute("DELETE FROM sessions WHERE (expires_at < ? OR revoked_at < ?)", (now - 86400, now - 86400))
        except sqlite3.Error as e:
            raise SessionStoreError(f"session store unavailable: {type(e).__name__}") from None
        return token

    def validate(self, token, kind, cred_version):
        """Principal for a live session of this kind, else None. Raises SessionStoreError if unreadable.
        cred_version: the expected version, or a function principal -> version (None = principal no longer
        valid, e.g. a deactivated customer), checked on every request."""
        if not _valid_shape(token):
            return None
        now = time.time()
        try:
            with self.lock, self._db() as c:
                r = c.execute("SELECT principal, cred_version, expires_at, idle_timeout, last_seen, revoked_at "
                              "FROM sessions WHERE token_hash=? AND kind=?", (_hash(token), kind)).fetchone()
                if r is None:
                    return None
                principal, version, expires, idle, last, revoked = r
                if revoked or now >= expires or now - last >= idle:
                    return None
                expected = cred_version(principal) if callable(cred_version) else cred_version
                if expected is None and callable(cred_version) or (expected is not None and str(expected) != version):
                    c.execute("UPDATE sessions SET revoked_at=?, revoke_reason=? WHERE token_hash=?",
                              (now, "credential changed", _hash(token)))
                    return None
                if now - last > 30:  # keep writes rare
                    c.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now, _hash(token)))
                return principal
        except sqlite3.Error as e:
            raise SessionStoreError(f"session store unavailable: {type(e).__name__}") from None

    def revoke(self, token, reason="logout"):
        if not _valid_shape(token):
            return False
        try:
            with self.lock, self._db() as c:
                return c.execute("UPDATE sessions SET revoked_at=?, revoke_reason=? WHERE token_hash=? AND revoked_at "
                                 "IS NULL", (time.time(), reason, _hash(token))).rowcount == 1
        except sqlite3.Error as e:
            raise SessionStoreError(f"session store unavailable: {type(e).__name__}") from None

    def revoke_all(self, kind, principal=None, reason="revoked by owner", keep=None):
        try:
            with self.lock, self._db() as c:
                sql, args = "UPDATE sessions SET revoked_at=?, revoke_reason=? WHERE kind=? AND revoked_at IS NULL", \
                    [time.time(), reason, kind]
                if principal:
                    sql += " AND principal=?"
                    args.append(principal)
                if keep:
                    sql += " AND token_hash != ?"
                    args.append(_hash(keep))
                return c.execute(sql, args).rowcount
        except sqlite3.Error as e:
            raise SessionStoreError(f"session store unavailable: {type(e).__name__}") from None

    def revoke_id(self, session_id, reason="revoked by owner"):
        """Revoke by the public id shown in the session list (first 12 hex chars of the hash)."""
        if not (isinstance(session_id, str) and len(session_id) == 12 and all(c in "0123456789abcdef" for c in session_id)):
            return 0
        try:
            with self.lock, self._db() as c:
                return c.execute("UPDATE sessions SET revoked_at=?, revoke_reason=? WHERE substr(token_hash,1,12)=? AND "
                                 "revoked_at IS NULL", (time.time(), reason, session_id)).rowcount
        except sqlite3.Error as e:
            raise SessionStoreError(f"session store unavailable: {type(e).__name__}") from None

    def list(self, current=None):
        """Live and recently ended sessions, without any secret: a short public id, never the token or its hash."""
        now = time.time()
        try:
            with self.lock, self._db() as c:
                rows = c.execute("SELECT token_hash, kind, principal, created_at, expires_at, idle_timeout, last_seen, "
                                 "revoked_at, revoke_reason FROM sessions ORDER BY last_seen DESC LIMIT 200").fetchall()
        except sqlite3.Error as e:
            raise SessionStoreError(f"session store unavailable: {type(e).__name__}") from None
        cur = _hash(current) if current else None
        out = []
        for h, kind, principal, created, expires, idle, last, revoked, reason in rows:
            state = ("revoked" if revoked else "expired" if now >= expires or now - last >= idle else "active")
            out.append({"id": h[:12], "kind": kind, "principal": principal, "state": state, "current": h == cur,
                        "created_at": created, "last_seen": last, "expires_at": expires, "revoke_reason": reason})
        return out

    # -- sign-in throttle (persistent, atomic)
    def throttle_check(self, key):
        try:
            with self._db() as c:
                r = c.execute("SELECT locked_until FROM throttle WHERE key=?", (key,)).fetchone()
        except sqlite3.Error as e:
            raise SessionStoreError(f"session store unavailable: {type(e).__name__}") from None
        if r and r[0] > time.time():
            raise Locked(r[0] - time.time())

    def throttle_fail(self, key):
        """Count one failure atomically; returns the lock length in seconds if this failure locked the key."""
        now = time.time()
        try:
            c = self._db()
            c.isolation_level = None
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT failures, window_start, locked_until, lock_count FROM throttle WHERE key=?", (key,)).fetchone()
            failures, start, locked, count = r if r else (0, now, 0, 0)
            if now - start > THROTTLE_WINDOW:
                failures, start = 0, now
            failures += 1
            lock = 0
            if failures >= THROTTLE_LIMIT:
                lock = min(LOCK_CAP, LOCK_BASE * 2 ** count)
                locked, count, failures, start = now + lock, count + 1, 0, now
            c.execute("INSERT INTO throttle (key, failures, window_start, locked_until, lock_count) VALUES (?,?,?,?,?) "
                      "ON CONFLICT(key) DO UPDATE SET failures=excluded.failures, window_start=excluded.window_start, "
                      "locked_until=excluded.locked_until, lock_count=excluded.lock_count", (key, failures, start, locked, count))
            c.execute("COMMIT")
            c.close()
            return lock
        except sqlite3.Error as e:
            raise SessionStoreError(f"session store unavailable: {type(e).__name__}") from None

    def throttle_clear(self, key):
        try:
            with self._db() as c:
                return c.execute("DELETE FROM throttle WHERE key=?", (key,)).rowcount
        except sqlite3.Error as e:
            raise SessionStoreError(f"session store unavailable: {type(e).__name__}") from None

    def check(self):
        try:
            with self._db() as c:
                return c.execute("PRAGMA quick_check").fetchone()[0]
        except sqlite3.Error as e:
            return f"error: {type(e).__name__}"
