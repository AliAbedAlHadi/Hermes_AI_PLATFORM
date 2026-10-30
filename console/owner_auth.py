"""Owner authentication for the Federation Console (M1: authenticated owner).

The owner's password is never stored: only a PBKDF2-SHA256 hash with a random salt, in
HERMES_HOME/federation/owner.json (or FED_OWNER_FILE). Until the owner exists, the console prints a
one-time setup code on its own terminal; whoever sets the first password must know that code, so a
visitor who reaches the page first cannot claim it. Sessions live in sessions.SessionStore (persistent,
hashed, revocable, bounded) and are bound to the owner's credential version, so a password change ends
every other session. Failed sign-ins are throttled in the same store (persists across restarts; bounded).
Recovery when locked out: wait, or run locally on the console machine (audited):
    python -I console/owner_auth.py --unlock
"""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # also when run as a script with -I (--unlock)
from sessions import Locked, SessionStore  # noqa: E402

ITERATIONS = 600_000
MIN_PASSWORD = 10
COOKIE = "fed_owner"


class AuthError(Exception):
    pass


class OwnerAuth:
    def __init__(self, path, store=None):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.store = store or SessionStore(self.path.with_name("sessions.db"))
        self.setup_code = None if self.path.exists() else secrets.token_hex(4)

    @staticmethod
    def _hash(password, salt):
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), ITERATIONS).hex()

    def exists(self):
        return self.path.exists()

    def version(self):
        """Credential version sessions are bound to; owner.json files written before versions count as 1."""
        try:
            return str(json.loads(self.path.read_text(encoding="utf-8")).get("credential_version", 1))
        except (OSError, ValueError):
            return None

    def _store(self, password):
        if len(password) < MIN_PASSWORD:
            raise AuthError(f"use at least {MIN_PASSWORD} characters")
        version = int(self.version() or 0) + 1 if self.path.exists() else 1
        salt = secrets.token_hex(16)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"algorithm": "pbkdf2_sha256", "iterations": ITERATIONS, "salt": salt,
                                   "hash": self._hash(password, salt), "credential_version": version,
                                   "updated_at": time.time()}), encoding="utf-8")
        os.replace(tmp, self.path)

    def setup(self, code, password):
        with self.lock:
            if self.exists() or not self.setup_code:
                raise AuthError("the owner already exists; sign in")
            if not hmac.compare_digest((code or "").strip(), self.setup_code):
                raise AuthError("wrong setup code (see the console's terminal)")
            self._store(password)
            self.setup_code = None
        return self._new_session()

    def check(self, password):
        rec = json.loads(self.path.read_text(encoding="utf-8"))
        return hmac.compare_digest(self._hash(password or "", rec["salt"]), rec["hash"])

    def login(self, password):
        try:
            self.store.throttle_check("owner")
        except Locked as e:
            raise AuthError(str(e)) from None
        if not self.exists():
            raise AuthError("no owner yet; use the setup code")
        if not self.check(password):
            self.store.throttle_fail("owner")
            raise AuthError("wrong password")
        self.store.throttle_clear("owner")
        return self._new_session()

    def change_password(self, current, new):
        with self.lock:
            if not self.check(current):
                raise AuthError("current password is wrong")
            self._store(new)  # bumps the credential version: older sessions die at their next request
        self.store.revoke_all("owner", reason="password changed")
        return self._new_session()

    def _new_session(self):
        return self.store.create("owner", "owner", self.version())

    def valid(self, token):
        """True for a live owner session. Raises sessions.SessionStoreError when the store is unreadable."""
        version = self.version()
        return version is not None and self.store.validate(token, "owner", version) == "owner"

    def logout(self, token):
        if token:
            self.store.revoke(token, "logout")


if __name__ == "__main__" and "--unlock" in __import__("sys").argv:
    # Local recovery: clear the sign-in lock. Needs access to this machine; writes an audit row.
    import sys
    home = Path(os.environ.get("HERMES_HOME") or (
        Path(os.environ["LOCALAPPDATA"]) / "hermes" if os.name == "nt" else Path.home() / ".hermes"))
    owner_file = Path(os.environ.get("FED_OWNER_FILE") or home / "federation" / "owner.json")
    cleared = SessionStore(owner_file.with_name("sessions.db")).throttle_clear("owner")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memory"))
    from fedmem import OWNER, Memory
    Memory(OWNER)._audit("owner.unlock", "owner", "ok", "sign-in lock cleared from the console machine")
    print("sign-in lock cleared" if cleared else "no sign-in lock was set")
