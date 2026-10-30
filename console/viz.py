"""Federation map data (Phase 5.1): the 118 agents, their companies, roles, escalation lines and a status
derived only from recorded facts. Read-only; served to the signed-in owner by /api/viz.

Status, first match wins (nothing is guessed; "unknown" means there is no record at all):
  live     a Kanban run of this agent is running and its last heartbeat is under 10 minutes old
  stale    a run is marked running but its heartbeat is older (or missing)
  blocked  a Kanban task assigned to it is blocked, or a federation task it owns is BLOCKED_EXTERNAL/ESCALATING
  failed   its latest finished run crashed, failed or timed out in the last 24 hours
  idle     it has worked before (runs or federation tasks) and nothing above applies
  unknown  no run and no federation task has ever been recorded for it
No conversation text, summaries, credentials or customer data are included.
"""
import importlib.util
import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

STATUSES = ("live", "stale", "blocked", "failed", "idle", "unknown")
STALE_SECONDS = 600
FAIL_WINDOW = 86400
# Federation escalation lines (roster/create_profiles.py states them in prose for these roles).
FED_ESCALATE = {"hermes-admin": None, "coo": "hermes-admin", "modelops": "hermes-admin", "finops": "hermes-admin",
                "infra-recovery": "hermes-admin", "knowledge": "security-policy", "security-policy": "hermes-admin",
                "audit": None}
COMPANY_NAMES = {"federation": "Hermes Federation", "isc": "ISC", "auvia": "AUVIA", "tabbeqai": "TabbeQ AI",
                 "spectra": "Spectra", "spectrasolutions": "Spectra Solutions"}


def _escalate_map(src):
    spec = importlib.util.spec_from_file_location("fed_create_profiles", src / "roster" / "create_profiles.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # defines tables only; main() is not called
    return mod.ESCALATE


def _board_facts(home, boards):
    """Per profile: runs (status, heartbeat, ended) and assigned tasks (status), read-only from every board."""
    facts = {}
    for b in boards:
        path = (Path(home(b)) if callable(home) else Path(home)) / "kanban" / "boards" / b / "kanban.db"
        if not path.exists():
            continue
        try:
            with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)) as k:
                k.row_factory = sqlite3.Row
                rcols = {r[1] for r in k.execute("PRAGMA table_info(task_runs)")}
                tcols = {r[1] for r in k.execute("PRAGMA table_info(tasks)")}
                beat = "last_heartbeat_at" if "last_heartbeat_at" in rcols else "started_at"
                for r in k.execute(f"SELECT r.profile, r.status, r.started_at, r.ended_at, r.{beat} AS beat, t.title, "
                                   f"t.id AS task FROM task_runs r LEFT JOIN tasks t ON t.id = r.task_id"):
                    f = facts.setdefault(r["profile"] or "", {"runs": [], "tasks": []})
                    f["runs"].append({"status": r["status"], "started": r["started_at"], "ended": r["ended_at"],
                                      "beat": r["beat"], "title": r["title"] or "", "task": r["task"], "board": b})
                if "assignee" in tcols:
                    for t in k.execute("SELECT id, title, status, assignee FROM tasks WHERE status != 'archived'"):
                        f = facts.setdefault(t["assignee"] or "", {"runs": [], "tasks": []})
                        f["tasks"].append({"id": t["id"], "title": t["title"] or "", "status": t["status"], "board": b})
        except sqlite3.Error:
            continue  # an unreadable board contributes nothing rather than wrong data
    return facts


def derive_status(runs, tasks, fed_tasks, now=None):
    """(status, reason) from recorded facts only."""
    now = now or time.time()
    running = [r for r in runs if r["status"] == "running"]
    if running:
        last = max((r["beat"] or r["started"] or 0) for r in running)
        if now - last < STALE_SECONDS:
            return "live", f"running {running[-1]['title'][:60]!r}"
        return "stale", f"marked running, last heartbeat {int((now - last) / 60)} min ago"
    blocked = [t for t in tasks if t["status"] == "blocked"]
    fed_blocked = [t for t in fed_tasks if t["state"] in ("BLOCKED_EXTERNAL", "ESCALATING")]
    if blocked or fed_blocked:
        what = blocked[0]["title"] if blocked else f"{fed_blocked[0]['id']} {fed_blocked[0]['state']}"
        return "blocked", f"blocked: {what[:60]!r}"
    ended = sorted((r for r in runs if r["ended"]), key=lambda r: r["ended"])
    if ended and ended[-1]["status"] in ("crashed", "failed", "timed_out") and now - ended[-1]["ended"] < FAIL_WINDOW:
        return "failed", f"last run {ended[-1]['status']} {int((now - ended[-1]['ended']) / 60)} min ago"
    if runs or fed_tasks:
        return "idle", "no active work"
    return "unknown", "no run or federation task recorded"


def federation_map(home, src, agents, workflow_tasks, org):
    """agents: the console's agent list (id, title, model, provider, board); org: fedmem.load_org()."""
    started = time.perf_counter()
    escalate = _escalate_map(src)
    facts = _board_facts(home, COMPANY_NAMES)
    by_owner = {}
    for t in workflow_tasks:
        by_owner.setdefault(t["owner"], []).append(t)
    ids = {a["id"] for a in agents}
    nodes, edges = [], []
    for a in agents:
        pid = a["id"]
        company = "federation" if pid.startswith("fed-") else pid.split("-", 1)[0]
        role = pid.split("-", 1)[1]
        o = org.get(pid, {})
        f = facts.get(pid, {"runs": [], "tasks": []})
        status, reason = derive_status(f["runs"], f["tasks"], by_owner.get(pid, []))
        recent = sorted(f["runs"], key=lambda r: r["ended"] or r["started"] or 0, reverse=True)[:3]
        nodes.append({
            "id": pid, "title": a.get("title") or role, "company": company, "role": role,
            "department": o.get("department"), "level": o.get("level"),
            "model": a.get("model"), "provider": a.get("provider"), "status": status, "reason": reason,
            "open_tasks": sum(1 for t in f["tasks"] if t["status"] not in ("done", "archived")),
            "runs": len(f["runs"]), "federation_tasks": len(by_owner.get(pid, [])),
            "last_activity": max([r["ended"] or r["started"] or 0 for r in f["runs"]] +
                                 [t["updated_at"] for t in by_owner.get(pid, [])] + [0]) or None,
            "recent": [{"at": r["ended"] or r["started"], "status": r["status"], "board": r["board"],
                        "task": r["task"], "title": r["title"][:80]} for r in recent],
        })
        target = FED_ESCALATE.get(role) if company == "federation" else escalate.get(role)
        target_id = (f"fed-{target}" if company == "federation" else f"{company}-{target}") if target else None
        if company != "federation" and not target:
            target_id = "fed-coo"  # company CEOs escalate to the federation COO / Hermes Admin
        if target_id in ids:
            edges.append({"from": pid, "to": target_id, "kind": "escalates_to"})
    companies = []
    for cid, name in COMPANY_NAMES.items():
        members = [n for n in nodes if n["company"] == cid]
        companies.append({"id": cid, "name": name, "agents": len(members),
                          "status": {s: sum(1 for n in members if n["status"] == s) for s in STATUSES}})
    return {"generated_at": time.time(), "statuses": list(STATUSES), "companies": companies, "agents": nodes,
            "edges": edges, "build_ms": round((time.perf_counter() - started) * 1000, 1),
            "source": "profiles (agents), roster.json + fedmem (company, role, department, level), "
                      "create_profiles.ESCALATE (escalation), Kanban boards + workflow.db (status, read-only)"}
