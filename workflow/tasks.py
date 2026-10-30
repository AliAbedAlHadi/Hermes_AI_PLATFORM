"""Durable federation tasks (spec v1.1 R11.01-R11.06) on top of Hermes Kanban.

A federation task carries the full task contract and the 13 logical states. Hermes Kanban does the
execution (local worker in a preserved git worktree); `sync()` reads the Kanban board read-only and turns
what the worker did into state changes. Every change is an event with a unique id, timestamp, actor,
correlation id and state version; transitions check the expected version and accept an idempotency key,
so a repeated request never applies twice.

Review gate (R11.06, M1 item 6). REVIEWING -> SUCCEEDED is taken only by the gate, never by a person, and
only when all of these hold for the SAME commit and requirements revision:
  1. the contract's test command passed on a clean checkout of that commit (run by the workflow itself);
  2. an independent Reviewer of the company recorded `pass` for that commit;
  3. an independent Verifier recorded `pass`, naming that review;
  4. the task branch still points at that commit (otherwise the evidence is stale).
A failed test run or a `fail` verdict sends the task to REWORK with the findings.

Gateway recovery (M1 item 8). Work is dispatched only when the agent's model endpoint answers. A transient
outage parks the task in BLOCKED_EXTERNAL and retries with bounded exponential backoff, resuming the SAME
Kanban task (no duplicate execution); a permanent error (missing model, bad credentials) blocks at once.

Usage (M1 item 8). Each Hermes session that worked on the task (worker, reviewer, verifier) is recorded once,
with the tokens and cost Hermes measured. Local inference has no API bill: tokens and wall time only.

    from tasks import Workflow
    wf = Workflow()                       # HERMES_HOME/federation/workflow.db
"""
import json
import os
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from contextlib import closing
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gateway  # noqa: E402

SPEC = json.loads((HERE / "task_transitions.json").read_text(encoding="utf-8"))
STATES = SPEC["states"]
TERMINAL = set(SPEC["terminal"])
TRANSITIONS = SPEC["transitions"]
SYSTEM_ONLY = set(SPEC["system_only"])
TASK_CLASSES = ("code", "docs", "analysis", "ops", "design")
STALE_SECONDS = 600       # R11.02: RUNNING without a heartbeat for 10 minutes is flagged, not trusted
MAX_RECOVERY = 6          # automatic gateway recovery attempts before the owner must act
MAX_REVIEW_JOBS = 2       # reviewer/verifier dispatches per role, commit and requirements revision
VERDICT_GRACE = 120       # a reviewer may close its Kanban job just before recording the verdict: wait this long
TEST_TIMEOUT = 300
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL", re.I)


def hermes_home():
    return Path(os.environ.get("HERMES_HOME") or (
        Path(os.environ["LOCALAPPDATA"]) / "hermes" if os.name == "nt" else Path.home() / ".hermes"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,                       -- tsk_xxxxxxxx, immutable
  company TEXT NOT NULL, project TEXT, customer TEXT,
  title TEXT NOT NULL,
  owner TEXT NOT NULL,                       -- accountable agent profile
  stage TEXT NOT NULL DEFAULT 'intake',      -- analysis, build, review, release, delivery ...
  task_class TEXT NOT NULL,
  input_contract TEXT NOT NULL,              -- what the worker receives
  output_contract TEXT NOT NULL,             -- what it must hand back
  acceptance_criteria TEXT NOT NULL,
  dependencies TEXT NOT NULL DEFAULT '[]',   -- JSON list of task ids
  requirements_revision INTEGER NOT NULL DEFAULT 1,
  data_restrictions TEXT NOT NULL DEFAULT '',
  allowed_tools TEXT NOT NULL DEFAULT '',
  evidence TEXT NOT NULL DEFAULT '[]',       -- JSON list of {kind, ref, at}
  state TEXT NOT NULL,
  state_version INTEGER NOT NULL DEFAULT 1,
  state_reason TEXT NOT NULL DEFAULT '',
  board TEXT NOT NULL,                       -- Kanban board of the executing company
  kanban_task TEXT,                          -- current Kanban attempt
  last_heartbeat REAL,
  correlation_id TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS task_events (
  id TEXT PRIMARY KEY,                       -- evt_ uuid
  task_id TEXT NOT NULL REFERENCES tasks(id),
  ts REAL NOT NULL, actor TEXT NOT NULL, correlation_id TEXT NOT NULL,
  state_version INTEGER NOT NULL,            -- version AFTER this event
  kind TEXT NOT NULL,                        -- created, transition, attempt, evidence, contract, tests, review, job, recovery
  from_state TEXT, to_state TEXT, detail TEXT NOT NULL DEFAULT '',
  idempotency_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS task_events_task ON task_events(task_id, ts);
CREATE TABLE IF NOT EXISTS task_attempts (
  task_id TEXT NOT NULL REFERENCES tasks(id),
  kanban_task TEXT NOT NULL, run_id INTEGER NOT NULL,
  profile TEXT, model TEXT, node TEXT, status TEXT, outcome TEXT, summary TEXT, error TEXT,
  started_at REAL, ended_at REAL,
  PRIMARY KEY (kanban_task, run_id)
);
CREATE TABLE IF NOT EXISTS task_reviews (
  id TEXT PRIMARY KEY,                       -- rv_xxxxxxxx
  task_id TEXT NOT NULL REFERENCES tasks(id),
  role TEXT NOT NULL,                        -- tests (run by the workflow), reviewer, verifier
  actor TEXT NOT NULL,
  commit_sha TEXT NOT NULL, requirements_revision INTEGER NOT NULL,
  verdict TEXT NOT NULL,                     -- pass, fail
  findings TEXT NOT NULL DEFAULT '',
  checked_review TEXT,                       -- verifier: the reviewer verdict it confirmed
  ts REAL NOT NULL,
  idempotency_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS task_reviews_task ON task_reviews(task_id, commit_sha, requirements_revision, role);
CREATE TABLE IF NOT EXISTS task_jobs (
  kanban_task TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  role TEXT NOT NULL,                        -- reviewer, verifier
  commit_sha TEXT NOT NULL, requirements_revision INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',       -- open, answered, incomplete
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS task_usage (
  session_id TEXT PRIMARY KEY,               -- one Hermes session counted once, however often sync runs
  task_id TEXT NOT NULL REFERENCES tasks(id),
  kanban_task TEXT NOT NULL, run_id INTEGER,
  role TEXT NOT NULL,                        -- worker, reviewer, verifier
  profile TEXT, provider TEXT, model TEXT, base_url TEXT, is_local INTEGER NOT NULL,
  started_at REAL, ended_at REAL, outcome TEXT, failure_category TEXT,
  api_calls INTEGER, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, reasoning_tokens INTEGER,
  provider_cost_usd REAL,                    -- billed amount the provider reported; NULL when none
  estimated_cost_usd REAL,                   -- Hermes estimate from its pricing table; NULL when unknown
  cost_status TEXT, cost_source TEXT, pricing_version TEXT,
  wall_seconds REAL,                         -- local usage: how long the session ran
  recorded_at REAL NOT NULL
);
"""
# Columns added after the first release; added in place, existing rows keep their data.
MIGRATIONS = {
    "tasks": [("test_command", "TEXT NOT NULL DEFAULT ''"), ("current_commit", "TEXT"), ("branch", "TEXT"),
              ("recovery_attempts", "INTEGER NOT NULL DEFAULT 0"), ("next_retry_at", "REAL"),
              ("block_cause", "TEXT NOT NULL DEFAULT ''")],
    "task_jobs": [("ended_at", "REAL")],
}


class TaskError(Exception):
    pass


class Conflict(TaskError):
    """The task changed since the caller read it (state version mismatch)."""


class Workflow:
    def __init__(self, db_path=None, home=None, hermes_cmd=None, probe=None, workspaces=None, sessions_db=None,
                 route=None):
        self.home = Path(home) if home else hermes_home()
        # route (console/routing.py): per-company homes, workspaces and identities of migrated groups; None = one home
        self.route = route
        path = Path(db_path or os.environ.get("FED_WORKFLOW_DB") or self.home / "federation" / "workflow.db")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        self.db.commit()
        cmd = hermes_cmd or os.environ.get("FED_HERMES_CMD") or "hermes"
        if isinstance(cmd, str) and " " in cmd:  # e.g. FED_HERMES_CMD='"C:\py\python.exe" -I "stub.py"'
            cmd = [part.strip('"') for part in shlex.split(cmd, posix=os.name != "nt")]
        self.hermes_cmd = cmd
        self.probe = probe or (lambda profile: gateway.probe_profile(self._home(profile), profile))
        self.workspaces = Path(workspaces or os.environ.get("FED_WORKSPACES") or
                               Path.home() / "hermes-federation" / "workspaces")
        self.sessions_db = sessions_db  # tests: callable(profile) -> path of a state.db

    def _home(self, key):
        """Hermes home of a board or profile."""
        return self.route.home(key) if self.route else self.home

    def _ws(self, board):
        return self.route.workspace(board) if self.route else self.workspaces / board
        self.node = socket.gethostname()

    # ------------------------------------------------------------ helpers
    def _event(self, task, actor, kind, from_state=None, to_state=None, detail="", idem=None, version=None):
        eid = "evt_" + uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO task_events (id, task_id, ts, actor, correlation_id, state_version, kind, from_state, "
            "to_state, detail, idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (eid, task["id"], time.time(), actor, task["correlation_id"],
             version if version is not None else task["state_version"], kind, from_state, to_state,
             detail[:2000], idem))
        return eid

    def _row(self, tid):
        r = self.db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if r is None:
            raise TaskError(f"task {tid} not found")
        return dict(r)

    def _workers(self, tid):
        return {r[0] for r in self.db.execute("SELECT DISTINCT profile FROM task_attempts WHERE task_id=? AND profile "
                                              "IS NOT NULL", (tid,))}

    def get(self, tid):
        t = self._row(tid)
        for k in ("dependencies", "evidence"):
            t[k] = json.loads(t[k])
        t["allowed_next"] = [s for s in TRANSITIONS[t["state"]] if f"{t['state']}->{s}" not in SYSTEM_ONLY]
        t["stale"] = bool(t["state"] == "RUNNING" and (time.time() - (t["last_heartbeat"] or t["updated_at"])) > STALE_SECONDS)
        t["events"] = [dict(e) for e in self.db.execute(
            "SELECT id, ts, actor, correlation_id, state_version, kind, from_state, to_state, detail FROM task_events "
            "WHERE task_id=? ORDER BY ts, rowid", (tid,))]
        t["attempts"] = [dict(a) for a in self.db.execute(
            "SELECT * FROM task_attempts WHERE task_id=? ORDER BY started_at, run_id", (tid,))]
        t["reviews"] = [dict(r) for r in self.db.execute(
            "SELECT id, role, actor, commit_sha, requirements_revision, verdict, findings, checked_review, ts "
            "FROM task_reviews WHERE task_id=? ORDER BY ts, rowid", (tid,))]
        t["jobs"] = [dict(j) for j in self.db.execute(
            "SELECT * FROM task_jobs WHERE task_id=? ORDER BY created_at", (tid,))]
        t["gate"] = self._gate(t) if t["state"] == "REVIEWING" else []
        return t

    def list(self, company=None):
        sql, args = "SELECT * FROM tasks", ()
        if company:
            sql, args = sql + " WHERE company=?", (company,)
        out = []
        for r in self.db.execute(sql + " ORDER BY updated_at DESC", args):
            r = dict(r)
            r["stale"] = bool(r["state"] == "RUNNING" and (time.time() - (r["last_heartbeat"] or r["updated_at"])) > STALE_SECONDS)
            out.append({k: r[k] for k in ("id", "title", "company", "owner", "state", "state_version", "task_class",
                                          "kanban_task", "requirements_revision", "stale", "updated_at", "block_cause",
                                          "current_commit")})
        return out

    # ------------------------------------------------------------ contract
    def create(self, actor, company, title, owner, task_class, input_contract, output_contract, acceptance_criteria,
               project=None, customer=None, dependencies=None, data_restrictions="", allowed_tools="", stage="intake",
               board=None, idempotency_key=None, test_command=""):
        if idempotency_key:
            hit = self.db.execute("SELECT task_id FROM task_events WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if hit:
                return self.get(hit[0])
        fields = {"title": title, "owner": owner, "input_contract": input_contract,
                  "output_contract": output_contract, "acceptance_criteria": acceptance_criteria}
        missing = [k for k, v in fields.items() if not (v or "").strip()]
        if missing:
            raise TaskError("task contract incomplete: " + ", ".join(missing))
        if task_class not in TASK_CLASSES:
            raise TaskError(f"task_class must be one of {', '.join(TASK_CLASSES)}")
        if task_class == "code" and not (test_command or "").strip():
            raise TaskError("a code task needs a test command (the review gate runs it on the exact commit)")
        if not owner.startswith(f"{company}-"):
            raise TaskError(f"owner {owner} is not an agent of {company}")
        deps = list(dependencies or [])
        for d in deps:
            self._row(d)
        tid, now = "tsk_" + uuid.uuid4().hex[:8], time.time()
        t = {"id": tid, "correlation_id": "cor_" + uuid.uuid4().hex[:12], "state_version": 1}
        self.db.execute(
            "INSERT INTO tasks (id, company, project, customer, title, owner, stage, task_class, input_contract, "
            "output_contract, acceptance_criteria, dependencies, data_restrictions, allowed_tools, state, board, "
            "correlation_id, created_by, created_at, updated_at, test_command) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, company, project, customer, title.strip(), owner, stage, task_class, input_contract.strip(),
             output_contract.strip(), acceptance_criteria.strip(), json.dumps(deps), data_restrictions,
             allowed_tools, "NEW", board or company, t["correlation_id"], actor, now, now, (test_command or "").strip()))
        self._event(t, actor, "created", None, "NEW", f"owner {owner}, class {task_class}", idempotency_key, 1)
        self.db.commit()
        return self.get(tid)

    def revise(self, tid, actor, expected_version, **changes):
        """Change the contract. R11.05: a changed material input invalidates evidence and needs a new run."""
        t = self._row(tid)
        if t["state"] in TERMINAL:
            raise TaskError(f"task is {t['state']}")
        if expected_version != t["state_version"]:
            raise Conflict(f"task is at version {t['state_version']}, not {expected_version}")
        allowed = ("input_contract", "output_contract", "acceptance_criteria", "data_restrictions", "allowed_tools",
                   "test_command")
        changes = {k: v.strip() for k, v in changes.items() if k in allowed and (v or "").strip()}
        if not changes:
            raise TaskError("nothing to change")
        for k, v in changes.items():
            self.db.execute(f"UPDATE tasks SET {k}=? WHERE id=?", (v, tid))
        # Reviews and test runs are bound to (commit, revision): bumping the revision makes them all stale.
        self.db.execute("UPDATE tasks SET requirements_revision=requirements_revision+1, evidence='[]', "
                        "state_version=state_version+1, updated_at=? WHERE id=?", (time.time(), tid))
        self._event(t, actor, "contract", t["state"], t["state"], "revised: " + ", ".join(changes) +
                    "; earlier evidence and reviews invalidated", version=t["state_version"] + 1)
        self.db.commit()
        return self.get(tid)

    # ------------------------------------------------------------ state machine
    def transition(self, tid, to_state, actor, expected_version, reason="", idempotency_key=None, system=False):
        if idempotency_key:
            hit = self.db.execute("SELECT task_id FROM task_events WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if hit:
                return self.get(hit[0])  # the same request already applied
        t = self._row(tid)
        frm = t["state"]
        if to_state not in STATES:
            raise TaskError(f"unknown state {to_state}")
        if to_state not in TRANSITIONS[frm]:
            raise TaskError(f"{frm} -> {to_state} is not allowed")
        if f"{frm}->{to_state}" in SYSTEM_ONLY and not system:
            raise TaskError(f"{frm} -> {to_state} is taken only by the system ({SPEC['system_only'][f'{frm}->{to_state}']})")
        if expected_version != t["state_version"]:
            raise Conflict(f"task is at version {t['state_version']}, not {expected_version}; reload and retry")
        if to_state in ("CANCELLED", "FAILED", "WAITING_CUSTOMER", "BLOCKED_EXTERNAL", "REWORK") and not reason.strip():
            raise TaskError(f"a reason is required to move to {to_state}")
        if to_state == "READY" and frm in ("ESCALATING", "BLOCKED_EXTERNAL") and not system and not reason.strip():
            raise TaskError(f"releasing a task from {frm} is an override: give a reason")
        if to_state == "READY":
            open_deps = [d for d in json.loads(t["dependencies"]) if self._row(d)["state"] != "SUCCEEDED"]
            if open_deps:
                raise TaskError("dependencies not SUCCEEDED yet: " + ", ".join(open_deps))
        if to_state == "SUCCEEDED":  # the gate holds for every caller, the system included
            missing = self._gate(t)
            if missing:
                raise TaskError("review gate: " + "; ".join(missing))
        v = t["state_version"] + 1
        cur = self.db.execute("UPDATE tasks SET state=?, state_version=?, state_reason=?, updated_at=? "
                              "WHERE id=? AND state_version=?", (to_state, v, reason, time.time(), tid, t["state_version"]))
        if cur.rowcount != 1:
            raise Conflict("task changed concurrently; reload and retry")
        self._event(t, actor, "transition", frm, to_state, reason, idempotency_key, v)
        if to_state == "RUNNING":
            self.db.execute("UPDATE tasks SET block_cause='', next_retry_at=NULL WHERE id=?", (tid,))
        if to_state == "REVIEWING":  # work finished: a later outage starts a fresh recovery budget
            self.db.execute("UPDATE tasks SET recovery_attempts=0 WHERE id=?", (tid,))
        self.db.commit()
        if to_state == "READY" or (to_state == "REWORK" and frm == "REVIEWING"):
            self._start_work(tid, actor, rework=(to_state == "REWORK"))
        if to_state in ("CANCELLED", "FAILED") and t["kanban_task"]:
            self._archive_worker(self._row(tid))
        return self.get(tid)

    def _archive_worker(self, t):
        """Stop the Kanban worker of a task that ended. A failure is recorded and retried by sync(), so the
        task database and the board never stay inconsistent."""
        try:
            rc, out, err = self._kanban("archive", t["kanban_task"], board=t["board"])
        except (OSError, subprocess.TimeoutExpired) as e:
            rc, err = 1, type(e).__name__
        if rc != 0:
            self._event(t, "system", "attempt", t["state"], t["state"],
                        f"Kanban archive of {t['kanban_task']} failed ({(err or '').strip()[-120:]}); sync will retry")
            self.db.commit()
            return False
        return True

    # ------------------------------------------------------------ review gate (R11.06)
    def _latest(self, tid, role, commit, rev):
        r = self.db.execute("SELECT * FROM task_reviews WHERE task_id=? AND role=? AND commit_sha=? AND "
                            "requirements_revision=? ORDER BY ts DESC, rowid DESC LIMIT 1", (tid, role, commit, rev)).fetchone()
        return dict(r) if r else None

    def _gate(self, t):
        """What still stops REVIEWING -> SUCCEEDED (empty list = the gate is open)."""
        tid, commit, rev = t["id"], t.get("current_commit"), t["requirements_revision"]
        if not commit:
            return ["no commit under review"]
        missing = []
        head = self._git_head(t["board"], t["branch"]) if t.get("branch") else ""
        if head != commit:
            missing.append(f"evidence is stale: branch {t.get('branch')} is at {head[:10] or '?'}, reviews are for {commit[:10]}")
        if t["test_command"] or t["task_class"] == "code":
            tests = self._latest(tid, "tests", commit, rev)
            if not tests or tests["verdict"] != "pass":
                missing.append("tests have not passed on this commit")
        rv = self._latest(tid, "reviewer", commit, rev)
        vf = self._latest(tid, "verifier", commit, rev)
        if not rv or rv["verdict"] != "pass":
            missing.append("no passing Reviewer verdict for this commit")
        if not vf or vf["verdict"] != "pass":
            missing.append("no passing Verifier verdict for this commit")
        elif rv and vf["checked_review"] != rv["id"]:
            missing.append("the Verifier confirmed a different review than the latest one")
        workers = self._workers(tid)
        if (rv and rv["actor"] in workers) or (vf and (vf["actor"] in workers or (rv and vf["actor"] == rv["actor"]))):
            missing.append("reviews are not independent of the worker or of each other")
        return missing

    def review(self, actor, tid, role, verdict, findings, commit, checked_review=None, idempotency_key=None):
        """A Reviewer or Verifier records its verdict. `actor` is the agent identity fixed by the caller
        (the MCP server's --agent), never a name the model typed."""
        if idempotency_key:
            hit = self.db.execute("SELECT task_id FROM task_reviews WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if hit:
                return self.get(hit[0])
        t = self._row(tid)
        company, _, actor_role = actor.partition("-")
        if role not in ("reviewer", "verifier") or actor_role != role:
            raise TaskError(f"only a {role} agent records a {role} verdict")
        if company != t["company"]:
            raise TaskError("a task is reviewed by its own company")
        if t["state"] != "REVIEWING":
            raise TaskError(f"task is {t['state']}, not REVIEWING")
        if verdict not in ("pass", "fail"):
            raise TaskError("verdict must be pass or fail")
        if len((findings or "").strip()) < 10:
            raise TaskError("say what you checked and what you found (findings)")
        full = t["current_commit"] or ""
        if not commit or len(commit) < 7 or not full.startswith(commit.strip()):
            raise TaskError(f"commit mismatch: the task is under review at {full[:12]}, you reviewed {commit}")
        if actor in self._workers(tid):
            raise TaskError("the worker cannot review its own work")
        rev = t["requirements_revision"]
        if role == "verifier":
            rv = self._latest(tid, "reviewer", full, rev)
            if not rv or rv["verdict"] != "pass":
                raise TaskError("nothing to verify: no passing Reviewer verdict for this commit")
            if rv["actor"] == actor:
                raise TaskError("the Verifier must be a different agent from the Reviewer")
            if checked_review != rv["id"]:
                raise TaskError(f"name the review you verified (checked_review = {rv['id']})")
        rid = "rv_" + uuid.uuid4().hex[:8]
        self.db.execute("INSERT INTO task_reviews (id, task_id, role, actor, commit_sha, requirements_revision, verdict, "
                        "findings, checked_review, ts, idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (rid, tid, role, actor, full, rev, verdict, findings.strip(), checked_review, time.time(),
                         idempotency_key))
        self.db.execute("UPDATE task_jobs SET status='answered' WHERE task_id=? AND role=? AND commit_sha=? AND "
                        "requirements_revision=? AND status='open'", (tid, role, full, rev))
        self._event(t, actor, "review", "REVIEWING", "REVIEWING", f"{role} {verdict} on {full[:10]} ({rid}): {findings[:300]}")
        self.db.commit()
        if verdict == "fail":
            self.transition(tid, "REWORK", actor, t["state_version"], f"{role} verdict fail ({rid}): {findings[:800]}")
        elif role == "verifier":
            self._try_finalize(tid)
        return self.get(tid)

    def _try_finalize(self, tid):
        t = self._row(tid)
        if t["state"] == "REVIEWING" and not self._gate(t):
            vf = self._latest(tid, "verifier", t["current_commit"], t["requirements_revision"])
            self.transition(tid, "SUCCEEDED", "system:review-gate", t["state_version"],
                            f"gate passed on {t['current_commit'][:10]}: tests, review and verification ({vf['id']})",
                            system=True)
            return True
        return False

    def _run_tests(self, t, commit):
        """Run the contract's test command on a clean export of exactly `commit`. Returns (passed, output)."""
        repo = self._ws(t["board"])
        tmp = Path(tempfile.mkdtemp(prefix="fedtest-"))
        try:
            arch = tmp / "src.tar"
            r = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", "-o", str(arch), commit],
                               capture_output=True, text=True)
            if r.returncode != 0:
                return False, f"cannot export {commit[:10]}: {r.stderr.strip()[:300]}"
            work = tmp / "src"
            work.mkdir()
            with tarfile.open(arch) as tar:
                tar.extractall(work, filter="data")
            argv = shlex.split(t["test_command"], posix=True)
            if argv and argv[0] in ("python", "python3", "py"):
                argv[0] = sys.executable
            env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            try:
                p = subprocess.run(argv, cwd=work, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   timeout=TEST_TIMEOUT, env=env)
            except subprocess.TimeoutExpired:
                return False, f"tests timed out after {TEST_TIMEOUT}s"
            except OSError as e:
                return False, f"cannot run the test command: {e}"
            out = (p.stdout + p.stderr).strip()
            return p.returncode == 0, f"exit {p.returncode}\n{out[-1500:]}"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ------------------------------------------------------------ Kanban execution
    def _kanban(self, *args, board, timeout=180):
        if self.route:
            return self.route.kanban(board, *args, timeout=timeout)
        cmd = list(self.hermes_cmd) if isinstance(self.hermes_cmd, (list, tuple)) else [self.hermes_cmd]
        r = subprocess.run([*cmd, "kanban", "--board", board, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1"})
        return r.returncode, ANSI.sub("", r.stdout), ANSI.sub("", r.stderr)

    def _brief(self, t, rework_note=""):
        lines = [f"Federation task {t['id']} (requirements revision {t['requirements_revision']}, "
                 f"correlation {t['correlation_id']}).", "",
                 "## Input", t["input_contract"], "", "## Output you must hand back", t["output_contract"], "",
                 "## Acceptance criteria", t["acceptance_criteria"]]
        if t["test_command"]:
            lines += ["", "## Test command (the review gate runs it on your commit)", t["test_command"]]
        if t["data_restrictions"]:
            lines += ["", "## Data restrictions", t["data_restrictions"]]
        if t["allowed_tools"]:
            lines += ["", "## Allowed tools", t["allowed_tools"]]
        if rework_note:
            lines += ["", "## Rework requested", rework_note]
            if t.get("branch"):
                lines += [f"Your previous attempt is on branch {t['branch']}: start with `git merge {t['branch']}` "
                          "and fix only what the findings name."]
        lines += ["", "## Handoff (R11.04)", "In your completion summary give: verified findings, assumptions, "
                  "attempted and failed approaches, artifact locations, test results with the commit hash, open questions."]
        return "\n".join(lines)

    def _block_for_gateway(self, t, status, detail, actor="system"):
        """Park the task in BLOCKED_EXTERNAL because its model endpoint failed; schedule a retry if transient."""
        t = self._row(t["id"])
        permanent = status in gateway.PERMANENT
        attempts = t["recovery_attempts"] + (0 if permanent else 1)
        cause = "gateway-permanent" if permanent else ("gateway-exhausted" if attempts >= MAX_RECOVERY else "gateway")
        nxt = None if cause != "gateway" else time.time() + gateway.backoff(attempts)
        self.db.execute("UPDATE tasks SET block_cause=?, recovery_attempts=?, next_retry_at=? WHERE id=?",
                        (cause, attempts, nxt, t["id"]))
        self.db.commit()
        why = f"model gateway {status}: {detail}"
        why += (" (permanent: fix the configuration, then move to READY)" if permanent else
                f" (recovery attempt {attempts}/{MAX_RECOVERY}" +
                (", next try at " + time.strftime("%H:%M:%S", time.localtime(nxt)) + ")" if nxt else
                 "; automatic recovery exhausted, owner action needed)"))
        self.transition(t["id"], "BLOCKED_EXTERNAL", actor, t["state_version"], why, system=True)

    def _start_work(self, tid, actor, rework=False):
        """Dispatch (or resume) the worker, but only if its model endpoint answers right now."""
        t = self._row(tid)
        status, detail = self.probe(t["owner"])
        if status != "ok":
            self._event(t, "system", "recovery", t["state"], t["state"], f"probe {status}: {detail}")
            self.db.commit()
            self._block_for_gateway(t, status, detail)
            return None
        if t["block_cause"].startswith("gateway") and t["kanban_task"] and self._kanban_status(t) == "blocked":
            # The worker's Kanban task was parked by us: resume the same task, never create a second one.
            self._kanban("unblock", t["kanban_task"], board=t["board"])
            self._event(t, actor, "attempt", t["state"], t["state"], f"resumed Kanban task {t['board']}:{t['kanban_task']}")
            self.db.commit()
            self._kanban("dispatch", board=t["board"])
            return t["kanban_task"]
        return self._dispatch(tid, actor, rework)

    def _dispatch(self, tid, actor, rework=False):
        t = self._row(tid)
        note = t["state_reason"] if rework else ""
        rc, out, err = self._kanban(
            "create", f"[{t['id']}] {t['title']}", "--body", self._brief(t, note), "--assignee", t["owner"],
            "--workspace", "worktree", "--max-runtime", "1800", "--max-retries", "2", "--json", board=t["board"])
        m = re.search(r'"id":\s*"(t_[0-9a-f]+)"', out)
        if rc != 0 or not m:
            self._event(t, "system", "attempt", t["state"], t["state"], f"Kanban create failed: {(err or out)[-300:]}")
            self.db.commit()
            raise TaskError("could not create the Kanban task: " + (err or out)[-300:])
        kid = m.group(1)
        self.db.execute("UPDATE tasks SET kanban_task=?, last_heartbeat=NULL WHERE id=?", (kid, tid))
        self._event(t, actor, "attempt", t["state"], t["state"], f"Kanban task {t['board']}:{kid} for {t['owner']}")
        self.db.commit()
        self._kanban("dispatch", board=t["board"])
        return kid

    def _dispatch_job(self, t, role, commit):
        """Ask the company's Reviewer or Verifier to judge one commit; it answers with the task_review tool."""
        agent = f"{t['company']}-{role}"
        status, detail = self.probe(agent)
        if status != "ok":
            self._event(t, "system", "recovery", "REVIEWING", "REVIEWING", f"{role} not dispatched, probe {status}: {detail}")
            self.db.commit()
            return None
        rev = t["requirements_revision"]
        tests = self._latest(t["id"], "tests", commit, rev)
        lines = [f"Independent {role} for federation task {t['id']} (requirements revision {rev}).", "",
                 f"Commit under review: {commit}", f"Branch: {t['branch']}", f"Repository: {self._ws(t['board'])}",
                 f"Read it with: git -C \"{self._ws(t['board'])}\" show {commit} (and git show {commit}:<file>).",
                 "", "## Acceptance criteria", t["acceptance_criteria"], "", "## Output contract", t["output_contract"]]
        if tests:
            lines += ["", "## Automatic test run on this commit", f"{tests['verdict']}: {tests['findings'][:800]}"]
        if role == "reviewer":
            lines += ["", "## Your job", "Check the commit against every acceptance criterion. Do not change any file.",
                      f"Then call task_review with task_id {t['id']}, role reviewer, commit {commit}, verdict pass or "
                      "fail, and findings naming each criterion and what you saw."]
        else:
            rv = self._latest(t["id"], "reviewer", commit, rev)
            lines += ["", "## Reviewer verdict to verify", f"{rv['id']} by {rv['actor']}: {rv['findings'][:800]}",
                      "", "## Your job", "Confirm independently that the evidence matches this exact commit, the tests "
                      "passed on it and the review is right. Do not change any file.",
                      f"Then call task_review with task_id {t['id']}, role verifier, commit {commit}, checked_review "
                      f"{rv['id']}, verdict pass or fail, and findings."]
        rc, out, err = self._kanban("create", f"[{t['id']}] {role} {commit[:10]}", "--body", "\n".join(lines),
                                    "--assignee", agent, "--max-runtime", "900", "--max-retries", "1", "--json",
                                    board=t["board"])
        m = re.search(r'"id":\s*"(t_[0-9a-f]+)"', out)
        if rc != 0 or not m:
            self._event(t, "system", "job", "REVIEWING", "REVIEWING", f"{role} job create failed: {(err or out)[-200:]}")
            self.db.commit()
            return None
        self.db.execute("INSERT INTO task_jobs (kanban_task, task_id, role, commit_sha, requirements_revision, created_at) "
                        "VALUES (?,?,?,?,?,?)", (m.group(1), t["id"], role, commit, rev, time.time()))
        self._event(t, "system", "job", "REVIEWING", "REVIEWING", f"{role} asked to judge {commit[:10]}: "
                                                                    f"Kanban {t['board']}:{m.group(1)} for {agent}")
        self.db.commit()
        self._kanban("dispatch", board=t["board"])
        return m.group(1)

    def _model_of(self, profile):
        ep = gateway.endpoint_of(self._home(profile), profile)
        return f"{ep[0]}/{ep[2]}" if ep else ""

    def _git_head(self, board, branch):
        r = subprocess.run(["git", "-C", str(self._ws(board)), "rev-parse", branch], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""

    def _kanban_db(self, board):
        path = self._home(board) / "kanban" / "boards" / board / "kanban.db"
        return closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)) if path.exists() else None

    def _kanban_status(self, t):
        db = self._kanban_db(t["board"])
        if db is None:
            return None
        with db as k:
            r = k.execute("SELECT status FROM tasks WHERE id=?", (t["kanban_task"],)).fetchone()
        return r[0] if r else None

    # ------------------------------------------------------------ sync
    def sync(self):
        """Turn Kanban activity into task states, run the review pipeline and gateway recovery, record usage.
        Safe to run any time, any number of times: everything is keyed (attempts by run, reviews and jobs by
        commit + revision, usage by session) and transitions use the current state version."""
        changed = []
        for (tid,) in self.db.execute("SELECT id FROM tasks WHERE kanban_task IS NOT NULL AND state IN "
                                      "('READY','RUNNING','REWORK')").fetchall():
            try:
                if self._sync_worker(tid):
                    changed.append(tid)
            except Conflict:
                continue  # someone else moved it; the next sync sees the new version
        for (tid,) in self.db.execute("SELECT id FROM tasks WHERE state='REVIEWING'").fetchall():
            try:
                if self._sync_review(tid):
                    changed.append(tid)
            except Conflict:
                continue
        now = time.time()
        for (tid,) in self.db.execute("SELECT id FROM tasks WHERE state='BLOCKED_EXTERNAL' AND block_cause='gateway' "
                                      "AND next_retry_at IS NOT NULL AND next_retry_at <= ?", (now,)).fetchall():
            try:
                if self._recover(tid):
                    changed.append(tid)
            except Conflict:
                continue
        # Ended tasks whose Kanban worker is still live (an archive failed): retry, so both stores agree.
        for (tid,) in self.db.execute("SELECT id FROM tasks WHERE state IN ('CANCELLED','FAILED') AND kanban_task IS NOT "
                                      "NULL AND updated_at > ?", (now - 7 * 86400,)).fetchall():
            t = self._row(tid)
            if self._kanban_status(t) in ("ready", "running", "blocked", "todo", "triage", "scheduled"):
                if self._archive_worker(t):
                    self._event(t, "system", "attempt", t["state"], t["state"], f"Kanban task {t['kanban_task']} archived on retry")
                    self.db.commit()
        for (tid,) in self.db.execute("SELECT id FROM tasks WHERE updated_at > ? OR state NOT IN "
                                      "('SUCCEEDED','FAILED','CANCELLED')", (now - 86400,)).fetchall():
            self.collect_usage(tid)
        return changed

    def _sync_worker(self, tid):
        t = self._row(tid)
        db = self._kanban_db(t["board"])
        if db is None:
            return False
        with db as k:
            k.row_factory = sqlite3.Row
            kt = k.execute("SELECT * FROM tasks WHERE id=?", (t["kanban_task"],)).fetchone()
            if kt is None:
                return False
            runs = k.execute("SELECT * FROM task_runs WHERE task_id=? ORDER BY id", (kt["id"],)).fetchall()
            beat = k.execute("SELECT MAX(created_at) FROM task_events WHERE task_id=? AND kind IN "
                             "('heartbeat','claimed','spawned','completed')", (kt["id"],)).fetchone()[0]
            last_block = k.execute("SELECT payload FROM task_events WHERE task_id=? AND kind IN ('blocked','gave_up') "
                                   "ORDER BY id DESC LIMIT 1", (kt["id"],)).fetchone()
        for r in runs:
            self.db.execute(
                "INSERT INTO task_attempts (task_id, kanban_task, run_id, profile, model, node, status, outcome, "
                "summary, error, started_at, ended_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(kanban_task, run_id) DO UPDATE SET status=excluded.status, outcome=excluded.outcome, "
                "summary=excluded.summary, error=excluded.error, ended_at=excluded.ended_at",
                (tid, kt["id"], r["id"], r["profile"], self._model_of(r["profile"] or t["owner"]), self.node,
                 r["status"], r["outcome"], (r["summary"] or "")[:2000], (r["error"] or "")[:1000],
                 r["started_at"], r["ended_at"]))
        if beat and beat != t["last_heartbeat"]:
            self.db.execute("UPDATE tasks SET last_heartbeat=? WHERE id=?", (beat, tid))
        if kt["branch_name"] and kt["branch_name"] != t["branch"]:
            self.db.execute("UPDATE tasks SET branch=? WHERE id=?", (kt["branch_name"], tid))
        self.db.commit()
        ks, moved = kt["status"], False
        if t["state"] in ("READY", "REWORK") and (ks in ("running", "done", "review", "blocked") or runs):
            self.transition(tid, "RUNNING", "system", t["state_version"],
                            f"worker {runs[-1]['profile'] if runs else t['owner']} claimed {kt['id']}", system=True)
            t, moved = self._row(tid), True
        if t["state"] != "RUNNING":
            return moved
        # A worker that died because its model endpoint failed: stop Kanban from burning retries, park, recover.
        last = runs[-1] if runs else None
        gw_text = " ".join(filter(None, [last["error"] if last else "", (last_block[0] if last_block else "") or "",
                                         kt["last_failure_error"] or ""]))
        if ks not in ("done", "review") and (last and last["status"] in ("crashed", "failed", "timed_out")
                                            or ks == "blocked") and gateway.is_gateway_error(gw_text):
            status, detail = self.probe(t["owner"])
            if status == "ok" and ks == "ready":
                # A blip that is already over: let Kanban spend one of its bounded retries now.
                self._kanban("dispatch", board=t["board"])
            if status != "ok":
                if ks != "blocked":
                    self._kanban("block", kt["id"], "--kind", "transient", f"model gateway {status}: {detail}"[:200],
                                 board=t["board"])
                self._block_for_gateway(t, status, detail)
                return True
        if ks in ("done", "review"):
            head = self._git_head(t["board"], kt["branch_name"]) if kt["branch_name"] else ""
            ev = json.loads(t["evidence"])
            if head:
                ev.append({"kind": "commit", "ref": f"{kt['branch_name']}@{head}", "at": time.time()})
            summary = (runs[-1]["summary"] if runs else "") or (kt["result"] or "")
            if summary:
                ev.append({"kind": "handoff", "ref": summary[:1500], "at": time.time()})
            self.db.execute("UPDATE tasks SET evidence=?, stage='review', current_commit=? WHERE id=?",
                            (json.dumps(ev), head or None, tid))
            self.db.commit()
            self.transition(tid, "REVIEWING", "system", t["state_version"],
                            f"worker completed {kt['id']}" + (f", commit {head[:10]}" if head else ", no commit found"),
                            system=True)
            return True
        if ks == "blocked":
            payload = json.loads(last_block[0]) if last_block and last_block[0] else {}
            kind = payload.get("kind") or ("retries" if "failures" in payload else "")
            to = "ESCALATING" if kind in ("capability", "retries") or "failures" in payload else "BLOCKED_EXTERNAL"
            why = payload.get("reason") or payload.get("error") or "worker blocked"
            self.transition(tid, to, "system", t["state_version"], f"{kind or 'blocked'}: {why[:400]}", system=True)
            return True
        if ks == "archived":
            self.transition(tid, "FAILED", "system", t["state_version"], "Kanban task archived outside the workflow")
            return True
        return moved

    def _sync_review(self, tid):
        """REVIEWING pipeline: freshness -> tests on the exact commit -> Reviewer -> Verifier -> gate."""
        t = self._row(tid)
        rev = t["requirements_revision"]
        head = self._git_head(t["board"], t["branch"]) if t["branch"] else ""
        if head and head != t["current_commit"]:
            self.db.execute("UPDATE tasks SET current_commit=? WHERE id=?", (head, tid))
            self._event(t, "system", "evidence", "REVIEWING", "REVIEWING",
                        f"branch moved to {head[:10]}: earlier reviews are stale, reviewing the new commit")
            self.db.commit()
            t = self._row(tid)
        commit = t["current_commit"]
        if not commit:
            self.transition(tid, "REWORK", "system", t["state_version"], "no commit to review: commit the work on the task branch")
            return True
        if (t["test_command"] or t["task_class"] == "code") and not self._latest(tid, "tests", commit, rev):
            ok, output = self._run_tests(t, commit) if t["test_command"] else (False, "no test command in the contract")
            rid = "rv_" + uuid.uuid4().hex[:8]
            self.db.execute("INSERT INTO task_reviews (id, task_id, role, actor, commit_sha, requirements_revision, "
                            "verdict, findings, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                            (rid, tid, "tests", "system:test-runner", commit, rev, "pass" if ok else "fail",
                             f"$ {t['test_command']}\n{output}", time.time()))
            self._event(t, "system:test-runner", "tests", "REVIEWING", "REVIEWING",
                        f"{'pass' if ok else 'fail'} on {commit[:10]} ({rid}): {output[:300]}")
            self.db.commit()
            if not ok:
                self.transition(tid, "REWORK", "system:test-runner", t["state_version"],
                                f"tests failed on {commit[:10]} ({rid}):\n{output[-800:]}")
                return True
        # Jobs that ended without a verdict are incomplete; the review stays open (never a success).
        for j in self.db.execute("SELECT * FROM task_jobs WHERE task_id=? AND status='open'", (tid,)).fetchall():
            db = self._kanban_db(t["board"])
            if db is None:
                break
            with db as k:
                r = k.execute("SELECT status FROM tasks WHERE id=?", (j["kanban_task"],)).fetchone()
            if r and r[0] in ("done", "review", "blocked", "archived"):
                self.db.execute("UPDATE task_jobs SET status='incomplete', ended_at=? WHERE kanban_task=?",
                                (time.time(), j["kanban_task"]))
                self._event(t, "system", "job", "REVIEWING", "REVIEWING",
                            f"{j['role']} job {j['kanban_task']} ended ({r[0]}) without recording a verdict")
                self.db.commit()
        for role in ("reviewer", "verifier"):
            verdict = self._latest(tid, role, commit, rev)
            if verdict:
                if verdict["verdict"] != "pass":
                    return False  # a fail verdict already moved the task to REWORK
                continue
            if role == "verifier" and not self._latest(tid, "reviewer", commit, rev):
                break
            jobs = self.db.execute("SELECT status, ended_at FROM task_jobs WHERE task_id=? AND role=? AND commit_sha=? AND "
                                   "requirements_revision=?", (tid, role, commit, rev)).fetchall()
            if any(j[0] == "open" for j in jobs):
                break
            # A late verdict is normal (the agent closes its job, then calls task_review): wait before asking again.
            if any(j[0] == "incomplete" and (j[1] or 0) > time.time() - VERDICT_GRACE for j in jobs):
                break
            if len(jobs) >= MAX_REVIEW_JOBS:
                if not t["state_reason"].startswith("review incomplete"):
                    self.db.execute("UPDATE tasks SET state_reason=? WHERE id=?",
                                    (f"review incomplete: {role} gave no verdict after {len(jobs)} requests; "
                                     "owner action needed (REWORK or FAILED)", tid))
                    self.db.commit()
                break
            if self._latest(tid, role, commit, rev):  # re-check: a verdict may have landed while we looked
                continue
            self._dispatch_job(t, role, commit)
            break
        return self._try_finalize(tid)

    def _recover(self, tid):
        """A gateway-blocked task whose retry time has come: probe, resume if healthy, else back off."""
        t = self._row(tid)
        status, detail = self.probe(t["owner"])
        self._event(t, "system", "recovery", "BLOCKED_EXTERNAL", "BLOCKED_EXTERNAL",
                    f"recovery probe {t['recovery_attempts']}/{MAX_RECOVERY}: {status} ({detail})")
        self.db.commit()
        if status == "ok":
            self.transition(tid, "READY", "system", t["state_version"],
                            f"model gateway recovered after {t['recovery_attempts']} attempt(s)", system=True)
            return True
        permanent = status in gateway.PERMANENT
        attempts = t["recovery_attempts"] + (0 if permanent else 1)
        cause = "gateway-permanent" if permanent else ("gateway-exhausted" if attempts >= MAX_RECOVERY else "gateway")
        nxt = time.time() + gateway.backoff(attempts) if cause == "gateway" else None
        self.db.execute("UPDATE tasks SET recovery_attempts=?, next_retry_at=?, block_cause=?, state_reason=? WHERE id=?",
                        (attempts, nxt, cause, f"model gateway {status}: {detail} (recovery attempt {attempts}/{MAX_RECOVERY}"
                         + ("; automatic recovery exhausted, owner action needed)" if cause == "gateway-exhausted" else
                            "; permanent, fix the configuration)" if permanent else ")"), tid))
        self.db.commit()
        return False

    def add_evidence(self, tid, actor, kind, ref):
        """Informational evidence (notes, links). It never opens the review gate on its own."""
        t = self._row(tid)
        ev = json.loads(t["evidence"]) + [{"kind": kind, "ref": ref, "at": time.time()}]
        self.db.execute("UPDATE tasks SET evidence=?, updated_at=? WHERE id=?", (json.dumps(ev), time.time(), tid))
        self._event(t, actor, "evidence", t["state"], t["state"], f"{kind}: {ref[:300]}")
        self.db.commit()
        return self.get(tid)

    # ------------------------------------------------------------ usage and cost
    def _state_db(self, profile):
        if self.sessions_db:
            return self.sessions_db(profile)
        return self._home(profile) / "profiles" / profile / "state.db"

    def collect_usage(self, tid):
        """Record each Hermes session that worked on the task, once. Sessions are found by the Kanban run's
        worker_session_id, else by the Kanban task title inside the run's time window."""
        t = self._row(tid)
        runs = [(a["kanban_task"], a["run_id"], a["profile"], "worker", a["started_at"], a["ended_at"], a["outcome"],
                 a["status"], a["error"]) for a in self.db.execute("SELECT * FROM task_attempts WHERE task_id=?", (tid,))]
        db = self._kanban_db(t["board"])
        if db is None:
            return 0
        added = 0
        with db as k:
            k.row_factory = sqlite3.Row
            jobs = {j["kanban_task"]: j["role"] for j in self.db.execute("SELECT * FROM task_jobs WHERE task_id=?", (tid,))}
            for kid, role in jobs.items():
                for r in k.execute("SELECT * FROM task_runs WHERE task_id=?", (kid,)):
                    runs.append((kid, r["id"], r["profile"], role, r["started_at"], r["ended_at"], r["outcome"],
                                 r["status"], r["error"]))
            titles = {r["id"]: r["title"] for r in k.execute(
                f"SELECT id, title FROM tasks WHERE id IN ({','.join('?' * len({x[0] for x in runs}))})",
                tuple({x[0] for x in runs}))} if runs else {}
            metas = {(r["task_id"], r["id"]): r["metadata"] for r in k.execute(
                "SELECT task_id, id, metadata FROM task_runs WHERE task_id IN "
                f"({','.join('?' * len(titles))})", tuple(titles))} if titles else {}
        for kid, run_id, profile, role, started, ended, outcome, status, error in runs:
            if not profile or not started:
                continue
            path = self._state_db(profile)
            if not Path(path).exists():
                continue
            hint = None
            try:
                hint = json.loads(metas.get((kid, run_id)) or "{}").get("worker_session_id")
            except ValueError:
                pass
            with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)) as s:
                s.row_factory = sqlite3.Row
                rows = []
                if hint:
                    rows = s.execute("SELECT * FROM sessions WHERE id=?", (hint,)).fetchall()
                if not rows:
                    rows = s.execute("SELECT * FROM sessions WHERE title=? AND started_at BETWEEN ? AND ?",
                                     (titles.get(kid, ""), started - 120, (ended or time.time()) + 120)).fetchall()
                for sess in rows:
                    u = s.execute("SELECT * FROM session_model_usage WHERE session_id=?", (sess["id"],)).fetchone()
                    self._record_usage(t, kid, run_id, role, profile, sess, u, outcome, status, error)
                    added += 1
        self.db.commit()
        return added

    def _record_usage(self, t, kid, run_id, role, profile, sess, u, outcome, status, error):
        src = u if u is not None else sess
        base = (src["billing_base_url"] or "") if "billing_base_url" in src.keys() else ""
        local = bool(re.search(r"//(localhost|127\.0\.0\.1|\[::1\])[:/]", base))
        tokens = {k: (src[k] if k in src.keys() else None) for k in
                  ("api_call_count", "input_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens")}
        cost_status = src["cost_status"] if "cost_status" in src.keys() else None
        cost_source = src["cost_source"] if "cost_source" in src.keys() else None
        actual = src["actual_cost_usd"] if "actual_cost_usd" in src.keys() else None
        est = src["estimated_cost_usd"] if "estimated_cost_usd" in src.keys() else None
        # Never invent money: a provider amount counts only when Hermes recorded a real source for it, and an
        # estimate only when Hermes knew the price. Local inference has no API bill at all.
        provider_cost = actual if (actual is not None and not local and cost_source not in (None, "", "none")) else None
        estimated = est if (est is not None and not local and cost_status not in (None, "", "unknown")) else None
        if outcome == "completed" or status == "done":
            failure = None
        elif gateway.is_gateway_error(error or ""):
            failure = "gateway"
        else:
            failure = status or outcome or "unknown"
        started, ended = sess["started_at"], sess["ended_at"]
        self.db.execute(
            "INSERT INTO task_usage (session_id, task_id, kanban_task, run_id, role, profile, provider, model, base_url, "
            "is_local, started_at, ended_at, outcome, failure_category, api_calls, input_tokens, output_tokens, "
            "cache_read_tokens, reasoning_tokens, provider_cost_usd, estimated_cost_usd, cost_status, cost_source, "
            "pricing_version, wall_seconds, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET provider=COALESCE(excluded.provider, provider), "
            "model=COALESCE(excluded.model, model), base_url=CASE WHEN excluded.base_url != '' THEN excluded.base_url "
            "ELSE base_url END, is_local=CASE WHEN excluded.base_url != '' THEN excluded.is_local ELSE is_local END, "
            "ended_at=excluded.ended_at, outcome=excluded.outcome, "
            "failure_category=excluded.failure_category, api_calls=excluded.api_calls, input_tokens=excluded.input_tokens, "
            "output_tokens=excluded.output_tokens, cache_read_tokens=excluded.cache_read_tokens, "
            "reasoning_tokens=excluded.reasoning_tokens, provider_cost_usd=excluded.provider_cost_usd, "
            "estimated_cost_usd=excluded.estimated_cost_usd, cost_status=excluded.cost_status, "
            "cost_source=excluded.cost_source, wall_seconds=excluded.wall_seconds",
            (sess["id"], t["id"], kid, run_id, role, profile, (src["billing_provider"] if "billing_provider" in src.keys() else None),
             sess["model"], base, int(local), started, ended, outcome or status, failure, tokens["api_call_count"],
             tokens["input_tokens"], tokens["output_tokens"], tokens["cache_read_tokens"], tokens["reasoning_tokens"],
             provider_cost, estimated, cost_status, cost_source,
             (sess["pricing_version"] if "pricing_version" in sess.keys() else None),
             (ended - started) if (started and ended) else None, time.time()))

    def usage(self, tid):
        """Per-session usage rows and task totals. Each session counts once; retries are separate sessions."""
        self._row(tid)
        rows = [dict(r) for r in self.db.execute("SELECT * FROM task_usage WHERE task_id=? ORDER BY started_at", (tid,))]
        runs = self.db.execute("SELECT COUNT(*) FROM task_attempts WHERE task_id=?", (tid,)).fetchone()[0]

        def total(key):
            vals = [r[key] for r in rows if r[key] is not None]
            return sum(vals) if vals else None

        rate = os.environ.get("FED_LOCAL_COST_PER_HOUR")
        local_secs = sum(r["wall_seconds"] or 0 for r in rows if r["is_local"])
        totals = {
            "sessions": len(rows), "worker_runs": runs, "retries": max(0, runs - 1),
            "by_role": {role: sum(1 for r in rows if r["role"] == role) for role in ("worker", "reviewer", "verifier")},
            "api_calls": total("api_calls"), "input_tokens": total("input_tokens"), "output_tokens": total("output_tokens"),
            "sessions_without_token_data": sum(1 for r in rows if r["input_tokens"] is None),
            "provider_cost_usd": total("provider_cost_usd"),
            "estimated_cost_usd": total("estimated_cost_usd"),
            "local_sessions": sum(1 for r in rows if r["is_local"]), "local_wall_seconds": round(local_secs, 1),
            "local_operational_cost": (round(local_secs / 3600 * float(rate), 4) if rate else None),
            "local_operational_cost_source": (f"FED_LOCAL_COST_PER_HOUR={rate} (owner-supplied rate)" if rate else
                                              "not estimated: set FED_LOCAL_COST_PER_HOUR to the machine's hourly cost"),
            "failures": {c: sum(1 for r in rows if r["failure_category"] == c)
                         for c in {r["failure_category"] for r in rows if r["failure_category"]}},
            "note": "Local Ollama inference has no API bill; provider cost is shown only when the provider reported it.",
        }
        return {"task": tid, "rows": rows, "totals": totals}
