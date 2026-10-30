"""Deployment validation: clone the committed repository and run the documented host procedure
(python console/server.py) in an isolated environment (tests/console_harness.py from the CLONE).
Checks start-up, non-destructive migration, authentication, core workflows, logs and restart behaviour.

    python -I tests/deploy_validate.py                 # the committed HEAD
    python -I tests/deploy_validate.py --working-tree  # HEAD plus the uncommitted changes on disk
"""
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
T = Path(tempfile.mkdtemp(prefix="fed-deploy-"))
subprocess.run(["git", "clone", "-q", str(REPO), str(T / "repo")], check=True)
head = subprocess.run(["git", "-C", str(T / "repo"), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
if "--working-tree" in sys.argv:  # overlay modified and new (non-ignored) files, so uncommitted work is validated too
    changed = subprocess.run(["git", "-C", str(REPO), "diff", "--name-only", "HEAD"], capture_output=True, text=True).stdout.split()
    changed += subprocess.run(["git", "-C", str(REPO), "ls-files", "--others", "--exclude-standard"], capture_output=True,
                              text=True).stdout.split()
    for rel in changed:
        if (REPO / rel).is_file():
            (T / "repo" / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, T / "repo" / rel)
    head += f"+{len(changed)} uncommitted"
sys.path.insert(0, str(T / "repo" / "tests"))
from console_harness import Console  # noqa: E402  (the clone's harness, run against the clone's code)

results = []


def check(name, ok, info=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {info}" if info and not ok else ""), flush=True)


def counts(db):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for (t,) in
           c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    c.close()
    return out


con = Console(port=8767, src=T / "repo").build()
fed = con.home / "federation"
before = {n: counts(fed / n) for n in ("memory.db", "workflow.db")}
try:
    con.start()
    check(f"fresh clone {head} starts and answers /login", True)
    check("first start prints a one-time setup code", "setup code" in con.output())
    netstat = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    check("listens on 127.0.0.1 only", f"127.0.0.1:{con.port}" in netstat and f"0.0.0.0:{con.port}" not in netstat)
    after = {n: counts(fed / n) for n in ("memory.db", "workflow.db")}
    check("start-up migration keeps every existing row", all(after[n].get(t) == c for n in before for t, c in before[n].items()))
    anon = con.client()
    check("admin API refuses anonymous calls", all(anon.get(p)[0] == 401 for p in (
        "/api/agents", "/api/ftasks", "/api/memory", "/api/workorders", "/api/memory/audit", "/api/admin/health")))
    owner = con.setup_owner()
    check("owner setup with the printed code", owner.get("/api/agents")[0] == 200)
    check("wrong password refused", con.client().post("/api/owner/login", {"password": "wrong-password-1"})[0] == 401)
    s, ag = owner.get("/api/agents")
    check("118 agents listed", s == 200 and len(ag) == 118)
    s, m = owner.get("/api/memory")
    active = sqlite3.connect(fed / "memory.db").execute(
        "SELECT COUNT(*) FROM records WHERE superseded_by IS NULL AND deleted=0").fetchone()[0]
    check("memory: the owner reads every active record", s == 200 and len(m["records"]) == min(active, 50))
    check("memory: owner write", owner.post("/api/memory/write", {"scope": "entity:isc", "kind": "fact", "title": "Deploy",
                                                                 "body": "deployment check"})[0] == 200)
    check("work orders list", owner.get("/api/workorders")[0] == 200)
    s, t = owner.post("/api/ftasks", {"company": "isc", "title": "Deploy check", "owner": "isc-junior-developer",
                                      "task_class": "code", "input_contract": "x", "output_contract": "y",
                                      "acceptance_criteria": "z", "test_command": "python -m unittest"})
    check("durable task create", s == 200 and t.get("state") == "NEW")
    owner.post(f"/api/ftasks/{t['id']}/transition", {"to": "TRIAGE", "expected_version": 1})
    s, t = owner.post(f"/api/ftasks/{t['id']}/transition", {"to": "READY", "expected_version": 2})
    check("READY probes the real model gateway, then dispatches (stub Kanban)", s == 200 and (
        t.get("kanban_task") or t.get("state") == "BLOCKED_EXTERNAL"), t.get("state"))
    check("owner cannot force SUCCEEDED", owner.post(f"/api/ftasks/{t['id']}/transition",
                                                    {"to": "SUCCEEDED", "expected_version": t["state_version"]})[0] == 400)
    check("usage view (owner only)", owner.get(f"/api/ftasks/{t['id']}/usage")[0] == 200 and
          anon.get(f"/api/ftasks/{t['id']}/usage")[0] == 401)
    s, h = owner.get("/api/admin/health")
    check("admin health: stores ok, 118 agents", s == 200 and h["agents"]["profiles"] == 118 and all(
        v == "ok" for v in h["stores"].values()))
    con.restart()
    check("restart: the owner session persists (bounded, revocable)", owner.get("/api/ftasks")[0] == 200)
    check("restart: no new setup code once the owner exists", "setup code" not in con.logs[-1].read_text(encoding="utf-8"))
    s, t2 = owner.get(f"/api/ftasks/{t['id']}")
    check("restart: task state and events persisted", s == 200 and t2["state"] == t["state"] and len(t2["events"]) >= 3)
    out = con.output()
    check("logs carry no password, token or key", con.password not in out and not re.search(r"gsk_[A-Za-z0-9]{10,}", out)
          and (owner.cookie("fed_owner") or "x") not in out)
finally:
    con.stop()
    shutil.rmtree(con.dir, ignore_errors=True)
    shutil.rmtree(T, ignore_errors=True)
print(f"\n{sum(results)} passed, {len(results) - sum(results)} failed (clone {head})")
sys.exit(0 if all(results) else 1)
