"""LIVE conversation-continuity test with a real agent on the local model (not the stub).

Starts a second console on port 8770 against the real Hermes home, with its OWN owner file, sessions and
conversations databases and its sync loop disabled (it never competes with the real console). The chats
are ordinary Hermes sessions of the chosen agent; no task, memory record or customer is changed.

Checks: a fact given in turn 1 is answered in turn 2 only through the server-rebuilt history, also after a
console restart; a repeated delivery runs the agent zero extra times (Hermes session count); a new
conversation does not inherit the fact.

    python -I tests/live_conversation.py [agent]
"""
import os
import secrets
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_harness import REAL, Client, Console  # noqa: E402

AGENT = sys.argv[1] if len(sys.argv) > 1 else "isc-business-analyst"
WORD = "AMBER-" + str(secrets.randbelow(900) + 100)
results = []


def check(name, ok, info=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {info}" if info and not ok else ""), flush=True)


def hermes_sessions():
    db = REAL / "profiles" / AGENT / "state.db"
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    n = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    c.close()
    return n


tmp = Path(tempfile.mkdtemp(prefix="fed-live-"))
con = Console(port=8770)
con.dir = tmp
con.home = REAL  # real agents, real model; everything the console itself writes goes to tmp
env = {"HERMES_HOME": str(REAL), "FED_OWNER_FILE": str(tmp / "owner.json"),
       "FED_CONVERSATIONS_DB": str(tmp / "conversations.db"), "FED_DISABLE_SYNC": "1", "GROQ_API_KEY": "",
       "FED_MEMORY_DB": str(REAL / "federation" / "memory.db"), "FED_WORKFLOW_DB": str(REAL / "federation" / "workflow.db")}


def start():
    con.extra_env = dict(env)
    con.env = lambda: {**{k: v for k, v in os.environ.items() if k not in ("AGENT_PROVIDER", "AGENT_MODEL")},
                       **con.extra_env, "CONSOLE_PORT": "8770", "CONSOLE_HOST": "127.0.0.1", "PYTHONIOENCODING": "utf-8"}
    return con.start()


try:
    start()
    owner = con.setup_owner()
    print(f"live test with {AGENT}; code word {WORD}", flush=True)
    s, r1 = owner.post("/api/chat", {"profile": AGENT, "message": f"For this conversation remember the code word {WORD}. "
                                     "Do not save it with any tool. Reply only with: noted.", "message_id": "live-msg-0001"},
                       timeout=900)
    conv = r1.get("conversation_id")
    check("turn 1 answered by the real agent", s == 200 and r1.get("ok") and conv, r1)
    con.restart()
    s, r2 = owner.post("/api/chat", {"profile": AGENT, "message": "What code word did I give you earlier in this "
                                     "conversation? Reply with the word only.", "message_id": "live-msg-0002",
                                     "conversation_id": conv}, timeout=900)
    check("after a console restart, turn 2 recalls the fact from the server-rebuilt history", s == 200 and WORD in
          (r2.get("reply") or ""), (r2.get("reply") or "")[-200:])
    before = hermes_sessions()
    s, r3 = owner.post("/api/chat", {"profile": AGENT, "message": "What code word did I give you earlier in this "
                                     "conversation? Reply with the word only.", "message_id": "live-msg-0002",
                                     "conversation_id": conv}, timeout=900)
    check("a repeated delivery returns the stored reply and starts no new agent session",
          s == 200 and r3.get("repeat") and r3["reply"] == r2["reply"] and hermes_sessions() == before)
    s, r4 = owner.post("/api/chat", {"profile": AGENT, "message": "What code word did I give you earlier? If you were "
                                     "not given one, reply exactly: none.", "message_id": "live-msg-0003"}, timeout=900)
    check("a new conversation does not inherit the earlier one", s == 200 and r4.get("conversation_id") != conv
          and WORD not in (r4.get("reply") or ""), (r4.get("reply") or "")[-200:])
finally:
    con.stop()
    shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{sum(results)} passed, {len(results) - sum(results)} failed (LIVE, real agent {AGENT})")
sys.exit(0 if all(results) else 1)
