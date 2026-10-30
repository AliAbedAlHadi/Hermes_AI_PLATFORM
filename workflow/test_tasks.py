"""Tests for durable tasks: state machine (R11.01-R11.03), review gate (R11.06), gateway recovery and usage.

Everything runs in a temporary directory: a fake Kanban board database, a stub `hermes` that simulates the
Kanban CLI, a real git repository as the company workspace, a controllable model probe and fake Hermes
session databases. No real task, board, model or customer data is touched.

    python -I test_tasks.py
"""
import http.server
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gateway  # noqa: E402
from tasks import MAX_RECOVERY, MAX_REVIEW_JOBS, VERDICT_GRACE, Conflict, TaskError, Workflow  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="wftest-"))
HOME, WS = TMP / "home", TMP / "ws"
BOARD_DB = HOME / "kanban" / "boards" / "isc" / "kanban.db"
LOG = TMP / "calls.log"
BOARD_DB.parent.mkdir(parents=True)
with sqlite3.connect(BOARD_DB) as k:
    k.executescript("""
    CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT, status TEXT, branch_name TEXT,
                        result TEXT, last_failure_error TEXT, workspace_kind TEXT);
    CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, profile TEXT, status TEXT, outcome TEXT,
                            summary TEXT, error TEXT, started_at REAL, ended_at REAL, metadata TEXT);
    CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER, kind TEXT,
                              payload TEXT, created_at REAL);""")

STUB = TMP / "hermes_stub.py"
STUB.write_text(f'''import json, sqlite3, sys, time, uuid
a = sys.argv[1:]
open({str(LOG)!r}, "a", encoding="utf-8").write(json.dumps(a) + "\\n")
db = sqlite3.connect({str(BOARD_DB)!r})
cmd = a[3]
def opt(name):
    return a[a.index(name) + 1] if name in a else None
if cmd == "create":
    kid = "t_" + uuid.uuid4().hex[:8]
    db.execute("INSERT INTO tasks (id, title, body, assignee, status, branch_name, workspace_kind) VALUES (?,?,?,?,?,?,?)",
               (kid, a[4], opt("--body"), opt("--assignee"), "ready",
                "wt/" + kid if opt("--workspace") == "worktree" else None, opt("--workspace") or "scratch"))
    print(json.dumps({{"id": kid}}))
elif cmd == "block":
    db.execute("UPDATE tasks SET status='blocked' WHERE id=?", (a[4],))
    db.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
               (a[4], "blocked", json.dumps({{"kind": opt("--kind"), "reason": a[-1]}}), time.time()))
elif cmd == "unblock":
    db.execute("UPDATE tasks SET status='ready' WHERE id=?", (a[4],))
elif cmd == "archive":
    import os
    if os.path.exists({str(TMP / "fail_archive")!r}):
        sys.exit(1)
    db.execute("UPDATE tasks SET status='archived' WHERE id=?", (a[4],))
db.commit()
''', encoding="utf-8")


def git(*args, cwd=None):
    return subprocess.run(["git", "-C", str(cwd or WS / "isc"), *args], capture_output=True, text=True, check=True).stdout


(WS / "isc").mkdir(parents=True)
git("init", "-q", "-b", "main")
git("config", "user.email", "t@example.invalid")
git("config", "user.name", "test")
(WS / "isc" / "README.md").write_text("isc\n")
git("add", "-A")
git("commit", "-qm", "init")

PROBE = {"isc-junior-developer": ("ok", "up"), "isc-reviewer": ("ok", "up"), "isc-verifier": ("ok", "up")}
SESSIONS = TMP / "sessions"
SESSIONS.mkdir()
wf = Workflow(db_path=TMP / "wf.db", home=HOME, hermes_cmd=[sys.executable, "-I", str(STUB)],
              probe=lambda p: PROBE.get(p, ("ok", "up")), workspaces=WS,
              sessions_db=lambda profile: SESSIONS / f"{profile}.db")
passed = failed = 0


def check(name, ok, info=""):
    global passed, failed
    passed, failed = passed + bool(ok), failed + (not ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {info}" if (info and not ok) else ""))


def raises(exc, fn, text=""):
    try:
        fn()
        return False
    except exc as e:
        return text.lower() in str(e).lower()


def calls(name):
    return [json.loads(line) for line in LOG.read_text(encoding="utf-8").splitlines() if json.loads(line)[3] == name] \
        if LOG.exists() else []


def kdb(sql, *args):
    with sqlite3.connect(BOARD_DB) as k:
        r = k.execute(sql, args).fetchall()
        k.commit()
        return r


def claim(kid, profile="isc-junior-developer"):
    kdb("UPDATE tasks SET status='running' WHERE id=?", kid)
    kdb("INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?,?,?,?)", kid, profile, "running", time.time())
    return kdb("SELECT MAX(id) FROM task_runs")[0][0]


def finish(kid, files, summary="done", run_status="done"):
    """Simulate the worker: commit files on the task branch and complete the Kanban task."""
    branch = kdb("SELECT branch_name FROM tasks WHERE id=?", kid)[0][0]
    git("checkout", "-q", "-B", branch, "main")
    for name, text in files.items():
        (WS / "isc" / name).write_text(text)
    git("add", "-A")
    git("commit", "-qm", f"work for {kid}")
    git("checkout", "-q", "main")
    kdb("UPDATE task_runs SET status=?, outcome='completed', summary=?, ended_at=? WHERE task_id=? AND status='running'",
        run_status, summary, time.time(), kid)
    kdb("UPDATE tasks SET status='done' WHERE id=?", kid)
    return git("rev-parse", branch).strip()


GOOD = {"greet.py": "def greet(n):\n    return f'Hello, {n}!' if n else 'Hello, friend!'\n",
        "test_greet.py": "import unittest\nfrom greet import greet\nclass T(unittest.TestCase):\n"
                         "    def test(self):\n        self.assertEqual(greet('Ali'), 'Hello, Ali!')\n"
                         "        self.assertEqual(greet(''), 'Hello, friend!')\n"}
BAD = dict(GOOD, **{"greet.py": "def greet(n):\n    return 'hi'\n"})


def make(title="Add greet", **kw):
    base = dict(actor="owner", company="isc", title=title, owner="isc-junior-developer", task_class="code",
                input_contract="Write greet.py", output_contract="Commit greet.py and tests",
                acceptance_criteria="greet('Ali') == 'Hello, Ali!'", test_command="python -m unittest test_greet")
    return wf.create(**(base | kw))


def to_ready(tid):
    t = wf.transition(tid, "TRIAGE", "owner", 1)
    return wf.transition(tid, "READY", "owner", t["state_version"])


def to_reviewing(files=GOOD, title="Add greet"):
    t = to_ready(make(title)["id"])
    claim(t["kanban_task"])
    wf.sync()
    sha = finish(t["kanban_task"], files)
    wf.sync()
    return wf.get(t["id"]), sha


# ================================================================ 1. state machine (regression)
t = make()
check("new task starts in NEW with version 1", t["state"] == "NEW" and t["state_version"] == 1)
check("creation is an event with id, actor and correlation", t["events"][0]["id"].startswith("evt_")
      and t["events"][0]["actor"] == "owner" and t["events"][0]["correlation_id"] == t["correlation_id"])
check("incomplete contract refused", raises(TaskError, lambda: make(acceptance_criteria=" ")))
check("code task without a test command refused", raises(TaskError, lambda: make(test_command=""), "test command"))
check("docs task may omit the test command", make(task_class="docs", test_command="")["state"] == "NEW")
check("owner must belong to the company", raises(TaskError, lambda: make(owner="spectra-junior-developer")))
k1, k2 = make(idempotency_key="k-1"), make(idempotency_key="k-1")
check("same idempotency key creates one task", k1["id"] == k2["id"])
tid = t["id"]
check("skipping states refused (NEW -> RUNNING)", raises(TaskError, lambda: wf.transition(tid, "RUNNING", "owner", 1)))
check("wrong expected version is a conflict", raises(Conflict, lambda: wf.transition(tid, "TRIAGE", "owner", 7)))
a = wf.transition(tid, "TRIAGE", "owner", 1, idempotency_key="tri-1")
b = wf.transition(tid, "TRIAGE", "owner", 1, idempotency_key="tri-1")
check("repeated request with the same key applies once", b["state_version"] == 2
      and sum(e["to_state"] == "TRIAGE" for e in b["events"]) == 1)
r = wf.transition(tid, "READY", "owner", 2)
check("READY dispatched one worktree Kanban task", r["kanban_task"] and len(calls("create")) == 1
      and calls("create")[0][calls("create")[0].index("--workspace") + 1] == "worktree")
check("brief carries contract, test command and handoff rules", "## Test command" in calls("create")[0][6]
      and "R11.04" in calls("create")[0][6])
check("owner cannot fake worker progress", raises(TaskError, lambda: wf.transition(tid, "RUNNING", "owner", 3), "system"))
dep = make(title="Depends", dependencies=[tid])
wf.transition(dep["id"], "TRIAGE", "owner", 1)
check("READY blocked until dependencies SUCCEEDED", raises(TaskError, lambda: wf.transition(dep["id"], "READY", "owner", 2)))
check("cancel needs a reason", raises(TaskError, lambda: wf.transition(tid, "CANCELLED", "owner", 3), "reason"))
wf.transition(tid, "CANCELLED", "owner", 3, "superseded")
check("cancel archives the Kanban task", calls("archive")[-1][4] == r["kanban_task"])

# ---- consistency: a failed Kanban archive is retried until both stores agree (Phase 4)
t = to_ready(make("Archive retry")["id"])
(TMP / "fail_archive").write_text("1")
t = wf.transition(t["id"], "CANCELLED", "owner", t["state_version"], "no longer needed")
check("a failed archive is recorded, the task still ends", t["state"] == "CANCELLED"
      and any("archive" in (e["detail"] or "") and "failed" in e["detail"] for e in t["events"])
      and kdb("SELECT status FROM tasks WHERE id=?", t["kanban_task"])[0][0] != "archived")
(TMP / "fail_archive").unlink()
wf.sync()
t = wf.get(t["id"])
check("the next sync archives the worker, so task and board agree", kdb(
    "SELECT status FROM tasks WHERE id=?", t["kanban_task"])[0][0] == "archived"
      and any("archived on retry" in (e["detail"] or "") for e in t["events"]))
wf.sync()
check("the retry happens once (no repeated archive events)", sum("archived on retry" in (e["detail"] or "")
                                                                 for e in wf.get(t["id"])["events"]) == 1)

# ================================================================ 2. review gate: the happy path
t, sha = to_reviewing()
tid = t["id"]
check("worker completion -> REVIEWING bound to the branch head", t["state"] == "REVIEWING" and t["current_commit"] == sha)
wf.sync()
t = wf.get(tid)
tests = [x for x in t["reviews"] if x["role"] == "tests"]
check("the workflow ran the tests on the exact commit and they passed", len(tests) == 1 and tests[0]["verdict"] == "pass"
      and tests[0]["commit_sha"] == sha)
jobs = [j for j in t["jobs"] if j["role"] == "reviewer"]
check("a Reviewer job was dispatched to isc-reviewer for that commit", len(jobs) == 1 and jobs[0]["commit_sha"] == sha
      and calls("create")[-1][calls("create")[-1].index("--assignee") + 1] == "isc-reviewer")
wf.sync()
wf.sync()
t = wf.get(tid)
check("repeated syncs do not rerun tests or duplicate jobs", len([x for x in t["reviews"] if x["role"] == "tests"]) == 1
      and len(t["jobs"]) == 1)
check("owner cannot request SUCCEEDED (system-only)", raises(TaskError, lambda: wf.transition(
    tid, "SUCCEEDED", "owner", t["state_version"]), "system"))
wf.add_evidence(tid, "owner", "verification", "I looked at it, looks fine")
check("owner evidence does not open the gate", wf.get(tid)["state"] == "REVIEWING"
      and any("Reviewer" in g for g in wf.get(tid)["gate"]))
check("even the system cannot pass the gate without verdicts", raises(TaskError, lambda: wf.transition(
    tid, "SUCCEEDED", "system", wf.get(tid)["state_version"], system=True), "review gate"))
check("the worker cannot review its own work", raises(TaskError, lambda: wf.review(
    "isc-junior-developer", tid, "reviewer", "pass", "checked everything", sha), "only a reviewer"))
check("an agent of another company cannot review", raises(TaskError, lambda: wf.review(
    "spectra-reviewer", tid, "reviewer", "pass", "checked everything", sha), "own company"))
check("a non-reviewer role cannot review", raises(TaskError, lambda: wf.review(
    "isc-team-lead", tid, "reviewer", "pass", "checked everything", sha), "only a reviewer"))
check("a review of another commit is refused", raises(TaskError, lambda: wf.review(
    "isc-reviewer", tid, "reviewer", "pass", "checked everything", "deadbeefcafe"), "commit mismatch"))
check("a verdict without findings is refused", raises(TaskError, lambda: wf.review(
    "isc-reviewer", tid, "reviewer", "pass", "ok", sha), "findings"))
check("the Verifier cannot go first", raises(TaskError, lambda: wf.review(
    "isc-verifier", tid, "verifier", "pass", "checked everything", sha), "no passing reviewer"))
t = wf.review("isc-reviewer", tid, "reviewer", "pass", "greet('Ali') and greet('') match the criteria", sha[:10],
              idempotency_key="rv-1")
t2 = wf.review("isc-reviewer", tid, "reviewer", "pass", "greet('Ali') and greet('') match the criteria", sha[:10],
               idempotency_key="rv-1")
rv = [x for x in t2["reviews"] if x["role"] == "reviewer"]
check("Reviewer pass recorded once despite a repeated request", len(rv) == 1 and t["state"] == "REVIEWING")
wf.sync()
t = wf.get(tid)
check("after the review a Verifier job was dispatched", any(j["role"] == "verifier" for j in t["jobs"]))
check("the Verifier must name the review it checked", raises(TaskError, lambda: wf.review(
    "isc-verifier", tid, "verifier", "pass", "evidence matches the commit", sha, checked_review="rv_other"), "checked_review"))
t = wf.review("isc-verifier", tid, "verifier", "pass", "tests passed on this commit; review is right", sha,
              checked_review=rv[0]["id"])
check("Verifier pass -> the gate moves the task to SUCCEEDED", t["state"] == "SUCCEEDED"
      and t["events"][-1]["actor"] == "system:review-gate")
check("every review decision is an event", sum(e["kind"] == "review" for e in t["events"]) == 2
      and sum(e["kind"] == "tests" for e in t["events"]) == 1)
check("no review can be recorded after SUCCEEDED", raises(TaskError, lambda: wf.review(
    "isc-verifier", tid, "verifier", "fail", "changed my mind about it", sha, checked_review=rv[0]["id"]), "not reviewing"))

# ================================================================ 3. review gate: negative paths
t, sha = to_reviewing(BAD, "Failing tests")
wf.sync()
t = wf.get(t["id"])
check("failing tests on the commit -> REWORK, never SUCCEEDED", t["state"] in ("REWORK", "RUNNING")
      and any(x["role"] == "tests" and x["verdict"] == "fail" for x in t["reviews"]))
check("the rework attempt is a new Kanban task carrying the test output", "tests failed" in calls("create")[-1][6]
      and "git merge" in calls("create")[-1][6])

t = to_ready(make("No commit")["id"])
claim(t["kanban_task"])
wf.sync()
kdb("UPDATE task_runs SET status='done', outcome='completed' WHERE task_id=?", t["kanban_task"])
kdb("UPDATE tasks SET status='done' WHERE id=?", t["kanban_task"])  # branch never created: nothing committed
wf.sync()
wf.sync()
t = wf.get(t["id"])
check("missing evidence (no commit) -> REWORK", t["state"] in ("REWORK", "RUNNING")
      and any("no commit" in (e["detail"] or "") for e in t["events"]))

t, sha = to_reviewing(title="Rejected review")
wf.sync()
t = wf.review("isc-reviewer", t["id"], "reviewer", "fail", "greet('') returns the wrong text", sha)
check("a rejected review -> REWORK with the findings", t["state"] in ("REWORK", "RUNNING")
      and "wrong text" in [e for e in t["events"] if e["kind"] == "transition" and e["to_state"] == "REWORK"][-1]["detail"])

t, sha = to_reviewing(title="Stale evidence")
tid = t["id"]
wf.sync()
wf.review("isc-reviewer", tid, "reviewer", "pass", "criteria met on this commit", sha)
branch = wf.get(tid)["branch"]
git("checkout", "-q", branch)
(WS / "isc" / "extra.py").write_text("x = 1\n")
git("add", "-A")
git("commit", "-qm", "late change")
git("checkout", "-q", "main")
new = git("rev-parse", branch).strip()
rv_old = [x for x in wf.get(tid)["reviews"] if x["role"] == "reviewer"][0]
check("verifying an outdated commit is refused once the branch moved", (wf.sync() or True) and raises(TaskError, lambda: wf.review(
    "isc-verifier", tid, "verifier", "pass", "evidence matches the commit", sha, checked_review=rv_old["id"]), "commit mismatch"))
t = wf.get(tid)
check("the branch move makes earlier reviews stale; the new commit is reviewed again", t["current_commit"] == new
      and any(x["role"] == "tests" and x["commit_sha"] == new for x in t["reviews"])
      and any(j["commit_sha"] == new and j["role"] == "reviewer" for j in t["jobs"]) and t["state"] == "REVIEWING")

t, sha = to_reviewing(title="Revised requirements")
tid = t["id"]
wf.sync()
wf.review("isc-reviewer", tid, "reviewer", "pass", "criteria met on this commit", sha)
rv1 = [x for x in wf.get(tid)["reviews"] if x["role"] == "reviewer"][0]
wf.revise(tid, "owner", wf.get(tid)["state_version"], acceptance_criteria="greet('Ali') == 'Hello, Ali!' and logs it")
check("a requirements revision voids earlier reviews", raises(TaskError, lambda: wf.review(
    "isc-verifier", tid, "verifier", "pass", "evidence matches", sha, checked_review=rv1["id"]), "no passing reviewer"))

t, sha = to_reviewing(title="Silent reviewer")
tid = t["id"]
wf.sync()
for _ in range(MAX_REVIEW_JOBS + 1):
    for j in wf.get(tid)["jobs"]:
        kdb("UPDATE tasks SET status='done' WHERE id=?", j["kanban_task"])  # the job ends without a verdict
    wf.sync()
    wf.db.execute("UPDATE task_jobs SET ended_at=? WHERE task_id=?", (time.time() - VERDICT_GRACE - 1, tid))
    wf.db.commit()  # the grace period has passed
    wf.sync()
t = wf.get(tid)
check(f"a Reviewer that never answers is retried at most {MAX_REVIEW_JOBS} times", len(t["jobs"]) == MAX_REVIEW_JOBS
      and all(j["status"] == "incomplete" for j in t["jobs"]))
check("an incomplete review leaves the task REVIEWING with a clear reason", t["state"] == "REVIEWING"
      and t["state_reason"].startswith("review incomplete"))

t, sha = to_reviewing(title="Late verdict")
tid = t["id"]
wf.sync()
job = [j for j in wf.get(tid)["jobs"] if j["role"] == "reviewer"][0]
kdb("UPDATE tasks SET status='done' WHERE id=?", job["kanban_task"])  # reviewer closes its job first...
wf.sync()
check("a job closed without a verdict waits out the grace period (no second job yet)",
      len([j for j in wf.get(tid)["jobs"] if j["role"] == "reviewer"]) == 1)
wf.review("isc-reviewer", tid, "reviewer", "pass", "late but valid verdict on this commit", sha)  # ...then answers
wf.db.execute("UPDATE task_jobs SET ended_at=? WHERE task_id=?", (time.time() - VERDICT_GRACE - 1, tid))
wf.db.commit()
wf.sync()
jobs = wf.get(tid)["jobs"]
check("a late verdict is accepted and no duplicate reviewer is dispatched (live race found in Phase 4)",
      len([j for j in jobs if j["role"] == "reviewer"]) == 1 and any(j["role"] == "verifier" for j in jobs))

# ================================================================ 4. gateway recovery
n_create = len(calls("create"))
PROBE["isc-junior-developer"] = ("down", "connection refused")
t = to_ready(make("Gateway down at start")["id"])
check("model down at dispatch -> BLOCKED_EXTERNAL, nothing dispatched", t["state"] == "BLOCKED_EXTERNAL"
      and len(calls("create")) == n_create and t["kanban_task"] is None)
row = wf._row(t["id"])
check("a transient outage schedules a retry with backoff", row["block_cause"] == "gateway" and row["recovery_attempts"] == 1
      and 25 < row["next_retry_at"] - time.time() <= 31)
for i in range(2, MAX_RECOVERY + 1):
    wf.db.execute("UPDATE tasks SET next_retry_at=? WHERE id=?", (time.time() - 1, t["id"]))
    wf.db.commit()
    wf.sync()
row = wf._row(t["id"])
check(f"recovery stops after {MAX_RECOVERY} attempts (no infinite loop)", row["block_cause"] == "gateway-exhausted"
      and row["recovery_attempts"] == MAX_RECOVERY and row["next_retry_at"] is None
      and "owner action needed" in row["state_reason"])
before = len([e for e in wf.get(t["id"])["events"] if e["kind"] == "recovery"])
wf.sync()
check("an exhausted task is not probed again by the sync", len([e for e in wf.get(t["id"])["events"]
                                                                if e["kind"] == "recovery"]) == before)
PROBE["isc-junior-developer"] = ("ok", "up")
check("releasing a blocked task without a reason is refused (override needs a reason)", raises(
    TaskError, lambda: wf.transition(t["id"], "READY", "owner", wf._row(t["id"])["state_version"]), "override"))
t = wf.transition(t["id"], "READY", "owner", wf._row(t["id"])["state_version"], "gateway is back up")
check("the owner can retry once the gateway is back: one dispatch", t["state"] == "READY" and len(calls("create")) == n_create + 1)

PROBE["isc-junior-developer"] = ("model_missing", "qwen3.5:9b is not served")
t = to_ready(make("Missing model")["id"])
row = wf._row(t["id"])
check("an unavailable model is permanent: blocked at once, no retry scheduled", row["block_cause"] == "gateway-permanent"
      and row["next_retry_at"] is None and "permanent" in row["state_reason"])

PROBE["isc-junior-developer"] = ("ok", "up")
t = to_ready(make("Crash mid-run")["id"])
kid = t["kanban_task"]
run = claim(kid)
wf.sync()
kdb("UPDATE task_runs SET status='crashed', error=?, ended_at=? WHERE id=?",
    "httpx.ConnectError: [WinError 10061] the target machine actively refused it", time.time(), run)
kdb("UPDATE tasks SET status='ready' WHERE id=?", kid)  # Kanban would retry the worker
PROBE["isc-junior-developer"] = ("down", "connection refused")
wf.sync()
t = wf.get(t["id"])
check("a worker killed by a gateway failure -> Kanban retries stopped, task BLOCKED_EXTERNAL",
      t["state"] == "BLOCKED_EXTERNAL" and kdb("SELECT status FROM tasks WHERE id=?", kid)[0][0] == "blocked"
      and calls("block")[-1][4] == kid)
n_create = len(calls("create"))
PROBE["isc-junior-developer"] = ("ok", "up")
wf.db.execute("UPDATE tasks SET next_retry_at=? WHERE id=?", (time.time() - 1, t["id"]))
wf.db.commit()
wf.sync()
t = wf.get(t["id"])
check("on recovery the SAME Kanban task resumes (no duplicate execution)", t["state"] == "READY"
      and t["kanban_task"] == kid and calls("unblock")[-1][4] == kid and len(calls("create")) == n_create)
check("recovery is audited", any(e["kind"] == "recovery" for e in t["events"])
      and any("gateway recovered" in (e["detail"] or "") for e in t["events"]))
run = claim(kid)
wf.sync()
check("the resumed worker moves the task to RUNNING again", wf.get(t["id"])["state"] == "RUNNING")
kdb("UPDATE task_runs SET status='crashed', error='AssertionError: wrong output', ended_at=? WHERE id=?", time.time(), run)
kdb("UPDATE tasks SET status='ready' WHERE id=?", kid)
wf.sync()
check("a crash that is not a gateway failure is left to Kanban's bounded retries", wf.get(t["id"])["state"] == "RUNNING")


# ---- the probe itself against an isolated local HTTP server
class H(http.server.BaseHTTPRequestHandler):
    mode = "ok"

    def log_message(self, *a):
        pass

    def do_GET(self):
        m = H.mode
        if m == "slow":
            time.sleep(3)
        code, body = {"ok": (200, b'{"data":[{"id":"qwen3.5:9b"}]}'), "nomodel": (200, b'{"data":[{"id":"llama3"}]}'),
                      "garbage": (200, b"<html>oops"), "auth": (401, b"{}"), "busy": (503, b"{}"),
                      "slow": (200, b'{"data":[{"id":"qwen3.5:9b"}]}')}[m]
        try:
            self.send_response(code)
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            pass  # the probe gave up first (timeout case)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_address[1]}/v1"
for mode, expect in (("ok", "ok"), ("nomodel", "model_missing"), ("garbage", "malformed"), ("auth", "auth"),
                     ("busy", "unavailable"), ("slow", "timeout")):
    H.mode = mode
    got = gateway.probe(url, "qwen3.5:9b", timeout=1)[0]
    check(f"probe classifies {mode!r} as {expect}", got == expect, got)
srv.shutdown()
srv.server_close()
check("probe classifies a stopped server as down (Windows needs ~2s to refuse)", gateway.probe(url, "qwen3.5:9b", timeout=5)[0] == "down")
check("transient vs permanent classes are disjoint", not (gateway.TRANSIENT & gateway.PERMANENT)
      and {"down", "timeout", "malformed", "unavailable"} <= gateway.TRANSIENT and {"model_missing", "auth"} <= gateway.PERMANENT)
check("backoff is exponential and capped", [gateway.backoff(i) for i in (1, 2, 3, 6, 9)] == [30, 60, 120, 900, 900])


# ================================================================ 5. usage and cost
def session_db(profile, rows):
    p = SESSIONS / f"{profile}.db"
    with sqlite3.connect(p) as s:
        s.executescript("""CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, title TEXT, model TEXT, started_at REAL,
            ended_at REAL, billing_base_url TEXT, billing_provider TEXT, api_call_count INTEGER, input_tokens INTEGER,
            output_tokens INTEGER, cache_read_tokens INTEGER, reasoning_tokens INTEGER, estimated_cost_usd REAL,
            actual_cost_usd REAL, cost_status TEXT, cost_source TEXT, pricing_version TEXT);
            CREATE TABLE IF NOT EXISTS session_model_usage (session_id TEXT, model TEXT, billing_provider TEXT,
            billing_base_url TEXT, api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER,
            cache_read_tokens INTEGER, reasoning_tokens INTEGER, estimated_cost_usd REAL, actual_cost_usd REAL,
            cost_status TEXT, cost_source TEXT);""")
        for r in rows:
            s.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", r["s"])
            if r.get("u"):
                s.execute("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", r["u"])


t = to_ready(make("Usage")["id"])
kid, title = t["kanban_task"], kdb("SELECT title FROM tasks WHERE id=?", t["kanban_task"])[0][0]
now = time.time()
kdb("INSERT INTO task_runs (task_id, profile, status, outcome, error, started_at, ended_at) VALUES (?,?,?,?,?,?,?)",
    kid, "isc-junior-developer", "crashed", None, "Connection refused", now - 300, now - 250)
kdb("INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at, metadata) VALUES (?,?,?,?,?,?,?)",
    kid, "isc-junior-developer", "done", "completed", now - 200, now - 100, json.dumps({"worker_session_id": "S2"}))
local = "http://localhost:11434/v1"
session_db("isc-junior-developer", [
    {"s": ("S1", title, "qwen3.5:9b", now - 299, now - 251, local, "custom", None, None, None, None, None, 0.0, 0.0,
           "unknown", "none", None)},  # crashed early: Hermes recorded no usage row and no tokens
    {"s": ("S2", "renamed by the agent", "qwen3.5:9b", now - 199, now - 101, local, "custom", 5, 29000, 600, 1000, 0,
           0.0, 0.0, "unknown", "none", None),
     "u": ("S2", "qwen3.5:9b", "custom", local, 5, 29000, 600, 1000, 0, 0.0, 0.0, "unknown", "none")}])
wf.sync()
wf.sync()
u = wf.usage(t["id"])
tot = u["totals"]
check("each session is recorded once, however often sync runs", len(u["rows"]) == 2 and tot["sessions"] == 2)
check("the session is found by run metadata even if its title changed", any(r["session_id"] == "S2" for r in u["rows"]))
check("retries are counted, not double-billed", tot["worker_runs"] == 2 and tot["retries"] == 1
      and tot["input_tokens"] == 29000 and tot["output_tokens"] == 600)
check("missing usage metadata stays missing (no invented tokens)", tot["sessions_without_token_data"] == 1
      and [r for r in u["rows"] if r["session_id"] == "S1"][0]["input_tokens"] is None)
check("the failed attempt is categorised as a gateway failure", tot["failures"] == {"gateway": 1})
check("local inference: no money invented, wall time recorded", tot["provider_cost_usd"] is None
      and tot["estimated_cost_usd"] is None and tot["local_sessions"] == 2 and tot["local_wall_seconds"] > 140
      and tot["local_operational_cost"] is None)
os.environ["FED_LOCAL_COST_PER_HOUR"] = "0.36"
check("an owner-supplied local rate is applied and labelled", abs(wf.usage(t["id"])["totals"]["local_operational_cost"]
                                                                   - round(tot["local_wall_seconds"] / 3600 * 0.36, 4)) < 1e-9
      and "owner-supplied" in wf.usage(t["id"])["totals"]["local_operational_cost_source"])
del os.environ["FED_LOCAL_COST_PER_HOUR"]

# A session first seen while running, before Hermes filled in its endpoint, is corrected by later collections.
t = to_ready(make("Late endpoint")["id"])
kid, title = t["kanban_task"], kdb("SELECT title FROM tasks WHERE id=?", t["kanban_task"])[0][0]
kdb("INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?,?,?,?)", kid, "isc-executor", "running", now - 60)
session_db("isc-executor", [{"s": ("L1", title, "qwen3.5:9b", now - 59, None, None, None, None, None, None, None, None,
                                   None, None, None, None, None)}])
wf.sync()
early = wf.usage(t["id"])["rows"][0]
with sqlite3.connect(SESSIONS / "isc-executor.db") as s:
    s.execute("UPDATE sessions SET billing_base_url=?, billing_provider='custom', ended_at=?, input_tokens=500, "
              "output_tokens=20 WHERE id='L1'", (local, now - 5))
wf.collect_usage(t["id"])
late = wf.usage(t["id"])["rows"][0]
check("a session recorded before its endpoint was known is corrected later (local, tokens, end time)",
      early["is_local"] == 0 and late["is_local"] == 1 and late["base_url"] == local and late["input_tokens"] == 500
      and late["wall_seconds"] and len(wf.usage(t["id"])["rows"]) == 1)

t, sha = to_reviewing(title="Paid review")
tid = t["id"]
wf.sync()
job = [j for j in wf.get(tid)["jobs"] if j["role"] == "reviewer"][0]
jt = kdb("SELECT title FROM tasks WHERE id=?", job["kanban_task"])[0][0]
kdb("INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) VALUES (?,?,?,?,?,?)",
    job["kanban_task"], "isc-reviewer", "done", "completed", now - 50, now - 10)
paid = "https://api.groq.com/openai/v1"
session_db("isc-reviewer", [{"s": ("R1", jt, "qwen/qwen3.8-27b", now - 49, now - 11, paid, "groq", 3, 9000, 300, 0, 0,
                                   0.0021, 0.0019, "actual", "provider", "groq-2026-10"),
                             "u": ("R1", "qwen/qwen3.8-27b", "groq", paid, 3, 9000, 300, 0, 0, 0.0021, 0.0019,
                                   "actual", "provider")}])
wf.sync()
r1 = [r for r in wf.usage(tid)["rows"] if r["session_id"] == "R1"]
check("reviewer sessions are counted under their role", r1 and r1[0]["role"] == "reviewer")
check("provider-reported cost and the Hermes estimate are kept apart", r1 and r1[0]["provider_cost_usd"] == 0.0019
      and r1[0]["estimated_cost_usd"] == 0.0021 and r1[0]["is_local"] == 0 and r1[0]["cost_source"] == "provider")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
