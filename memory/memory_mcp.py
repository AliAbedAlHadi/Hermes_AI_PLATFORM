"""Federation memory as an MCP stdio server (standard library only).

The caller's identity comes from --agent, written into each profile's config.yaml by
install_memory.py. The model cannot change it, so permissions are enforced outside the model (R10.05).
FED_CUSTOMER (passed through the profile's MCP env) binds the session to one authenticated customer.

    python -I memory_mcp.py --agent isc-junior-developer [--db PATH] [--roster PATH] [--workflow-db PATH]

task_get / task_review expose the durable-task review gate (workflow/tasks.py): a Reviewer or Verifier
records its verdict under the same fixed identity, so it cannot speak for another agent.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parent.parent / "workflow"))
from fedmem import Denied, KINDS, Memory  # noqa: E402

SCOPE_HELP = ("Scope: 'agent' (your own notes), 'department', 'entity' (your company), 'project:<slug>', "
              "'admin:<group>' (leadership), or 'global' (read only).")

TOOLS = [
    {"name": "memory_whoami",
     "description": "Show your company, department, authority level, projects and which memory scopes you can read.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "memory_search",
     "description": "Search the memory you are allowed to read (global, your company, your department, your projects). "
                    "Use before deciding something that may already be decided. " + SCOPE_HELP,
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Keywords"},
         "scope": {"type": "string", "description": "Optional: limit to one scope"},
         "kind": {"type": "string", "enum": list(KINDS)},
         "limit": {"type": "integer", "default": 10}}}},
    {"name": "memory_get",
     "description": "Read one memory record with its revision history (who changed it and why).",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}},
    {"name": "memory_write",
     "description": "Save a fact, decision, preference, procedure or lesson. Never store keys, tokens or passwords. "
                    "Use status 'verified' only with evidence, and put the evidence (task id, commit, file) in source. "
                    + SCOPE_HELP,
     "inputSchema": {"type": "object", "properties": {
         "scope": {"type": "string"},
         "kind": {"type": "string", "enum": list(KINDS)},
         "title": {"type": "string"},
         "body": {"type": "string"},
         "status": {"type": "string", "enum": ["claim", "verified"], "default": "claim"},
         "confidentiality": {"type": "string", "enum": ["internal", "confidential", "restricted"], "default": "internal"},
         "source": {"type": "string", "description": "Provenance: task id, commit hash, file, URL"},
         "tags": {"type": "string"}},
         "required": ["scope", "kind", "title", "body"]}},
    {"name": "memory_override",
     "description": "Replace a record with a new revision. Allowed for your own records, or for records written by "
                    "someone below your level in your company. The old revision is kept. A reason is required.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "integer"}, "body": {"type": "string"}, "reason": {"type": "string"},
         "title": {"type": "string"}, "status": {"type": "string", "enum": ["claim", "verified"]}},
         "required": ["id", "body", "reason"]}},
    {"name": "memory_project",
     "description": "List, add or remove members of a project memory (add/remove: managers level 50+).",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["list", "add", "remove"]},
         "project": {"type": "string"}, "agent": {"type": "string", "description": "Profile, e.g. isc-qa"}},
         "required": ["action", "project"]}},
    {"name": "memory_customer",
     "description": "Customers of your company. register a new customer (name, channel, handle, language, details); "
                    "find by exact channel handle; get or update one customer; list; link_project (managers). "
                    "Customer notes go in memory_write with scope 'customer:<id>'.",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["register", "find", "get", "list", "update", "link_project"]},
         "customer_id": {"type": "string"}, "name": {"type": "string"},
         "channel": {"type": "string", "description": "whatsapp, telegram, email, portal"},
         "handle": {"type": "string", "description": "phone, username or email on that channel"},
         "language": {"type": "string"}, "details": {"type": "string", "description": "Important facts about the customer"},
         "project": {"type": "string"}},
         "required": ["action"]}},
    {"name": "work_order",
     "description": "Cross-company work. Companies never read each other's memory: to get work or data from another "
                    "company, its managers create a work order (create), the lead CEO approves it, the supplier's "
                    "leadership accepts it with a team, the team starts, links its Kanban task and delivers, and the "
                    "lead closes it or asks for rework. revise changes requirements (pauses the work); cancel stops it. "
                    "While accepted, the supplier team reads only the granted records; both sides talk in scope "
                    "'workorder:<id>' with memory_write/memory_search.",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["create", "approve", "accept", "reject", "start", "link_task", "deliver",
                                               "close", "rework", "revise", "cancel", "get", "list"]},
         "wo_id": {"type": "string"},
         "supplier": {"type": "string", "description": "isc, auvia, tabbeqai, spectra or spectrasolutions"},
         "title": {"type": "string"}, "work_scope": {"type": "string"},
         "acceptance_criteria": {"type": "string"}, "permitted_sharing": {"type": "string"},
         "outputs": {"type": "string"}, "cost_allocation": {"type": "string"},
         "customer": {"type": "string"}, "project": {"type": "string"},
         "grant_records": {"type": "array", "items": {"type": "integer"}, "description": "Record ids the supplier may read"},
         "team": {"type": "array", "items": {"type": "string"}, "description": "Supplier agent profiles (accept)"},
         "task": {"type": "string", "description": "<board>:t_xxxxxxxx"},
         "delivery": {"type": "string"}, "reason": {"type": "string"}},
         "required": ["action"]}},
    {"name": "memory_lesson",
     "description": "Propose a lesson so agents in other companies can learn from it (write it without customer names "
                    "or private data). The knowledge steward uses list/approve/reject to publish it to global memory.",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["propose", "list", "approve", "reject"]},
         "text": {"type": "string"}, "department": {"type": "string"},
         "lesson_id": {"type": "integer"}, "sanitized_text": {"type": "string"},
         "source_record": {"type": "integer"}},
         "required": ["action"]}},
    {"name": "task_get",
     "description": "Read a durable task of your company: its contract, the commit under review, the automatic test "
                    "result and the verdicts so far. Use it before task_review.",
     "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    {"name": "task_review",
     "description": "Reviewer or Verifier only: record your verdict on the exact commit under review. findings must "
                    "name each acceptance criterion and what you saw. A Verifier passes checked_review = the id of the "
                    "Reviewer verdict it confirmed. A fail sends the task back to rework; the last pass completes it.",
     "inputSchema": {"type": "object", "properties": {
         "task_id": {"type": "string"}, "role": {"type": "string", "enum": ["reviewer", "verifier"]},
         "commit": {"type": "string", "description": "The commit hash you reviewed (at least 7 characters)"},
         "verdict": {"type": "string", "enum": ["pass", "fail"]}, "findings": {"type": "string"},
         "checked_review": {"type": "string"}},
         "required": ["task_id", "role", "commit", "verdict", "findings"]}},
]

_WF = {}


def workflow(path=None):
    """Durable-task store, opened on first use."""
    if "wf" not in _WF:
        from tasks import Workflow
        _WF["wf"] = Workflow(db_path=path)
    return _WF["wf"]


def task_view(mem, tid, path):
    wf = workflow(path)
    from tasks import TaskError
    try:
        t = wf.get(tid)
    except TaskError as e:
        raise Denied(str(e))
    me = mem.me
    if me.get("bound") or (me["company"] != t["company"] and me["profile"] not in ("fed-hermes-admin", "fed-audit")):
        raise Denied("you can only read tasks of your own company")
    keep = ("id", "title", "company", "state", "requirements_revision", "task_class", "input_contract",
            "output_contract", "acceptance_criteria", "test_command", "branch", "current_commit", "gate")
    return {k: t.get(k) for k in keep} | {"reviews": [
        {k: r[k] for k in ("id", "role", "actor", "commit_sha", "verdict", "findings", "checked_review")}
        for r in t["reviews"] if r["commit_sha"] == t["current_commit"] and r["requirements_revision"] == t["requirements_revision"]]}


def call(mem, name, a):
    if name == "memory_whoami":
        return mem.whoami()
    if name == "memory_search":
        return mem.search(a.get("query", ""), a.get("scope"), a.get("kind"), a.get("limit", 10))
    if name == "memory_get":
        return mem.get(int(a["id"]))
    if name == "memory_write":
        return mem.write(a["scope"], a["kind"], a["title"], a["body"], a.get("status", "claim"),
                         a.get("confidentiality", "internal"), a.get("source", ""), a.get("tags", ""))
    if name == "memory_override":
        return mem.override(int(a["id"]), a["body"], a["reason"], a.get("title"), a.get("status"))
    if name == "memory_project":
        return mem.project(a["action"], a["project"], a.get("agent"))
    if name == "memory_customer":
        return mem.customer(a["action"], a.get("customer_id"), a.get("name"), a.get("channel"), a.get("handle"),
                            a.get("language"), a.get("details"), a.get("project"))
    if name == "work_order":
        a = dict(a)
        return mem.work_order(a.pop("action"), a.pop("wo_id", None), **a)
    if name == "memory_lesson":
        return mem.lesson(a["action"], a.get("text"), a.get("department"), a.get("lesson_id"),
                          a.get("sanitized_text"), a.get("source_record"))
    if name in ("task_get", "task_review"):
        path = getattr(mem, "workflow_db", None)
        if name == "task_get":
            return task_view(mem, a["task_id"], path)
        if mem.me.get("bound"):
            raise Denied("reviews are not recorded in a customer conversation")
        from tasks import TaskError
        try:
            t = workflow(path).review(mem.me["profile"], a["task_id"], a["role"], a["verdict"], a["findings"],
                                      a["commit"], a.get("checked_review"))
        except TaskError as e:
            raise Denied(str(e))
        return {"task": t["id"], "state": t["state"], "gate_missing": t["gate"]}
    raise Denied(f"unknown tool {name}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True)
    ap.add_argument("--db")
    ap.add_argument("--roster")
    ap.add_argument("--workflow-db")
    opts = ap.parse_args()
    customer = os.environ.get("FED_CUSTOMER", "").strip()
    if customer.startswith("${"):  # placeholder left unresolved: no customer session
        customer = ""
    mem = Memory(opts.agent, opts.db, opts.roster, customer=customer or None)  # bad binding: fail closed
    mem.workflow_db = opts.workflow_db

    def send(msg):
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        rid, method = req.get("id"), req.get("method", "")
        if rid is None:  # notification (e.g. notifications/initialized)
            continue
        if method == "initialize":
            ver = (req.get("params") or {}).get("protocolVersion", "2025-06-18")
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": ver, "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "federation-memory", "version": "1.0"},
                "instructions": f"Federation memory for {opts.agent}. Permissions are enforced by the server."}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            p = req.get("params") or {}
            try:
                out, err = call(mem, p.get("name"), p.get("arguments") or {}), False
            except Denied as e:
                out, err = {"denied": str(e)}, True
            except (KeyError, ValueError, TypeError) as e:
                out, err = {"error": f"bad arguments: {e}"}, True
            except Exception as e:  # never let one bad call take the agent's memory tools down
                out, err = {"error": f"{type(e).__name__}: {e}"[:300]}, True
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, default=str)}],
                "isError": err}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}})


if __name__ == "__main__":
    main()
