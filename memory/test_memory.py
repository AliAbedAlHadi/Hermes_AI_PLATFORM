"""Isolation and override tests for federation memory (R16.05 non-leakage). Uses a temporary database.

    python -I test_memory.py
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fedmem import Denied, Memory  # noqa: E402

DB = Path(tempfile.mkdtemp()) / "memory.db"
M = lambda p: Memory(p, DB)  # noqa: E731
passed = failed = 0


def check(name, ok):
    global passed, failed
    passed, failed = passed + bool(ok), failed + (not ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}")


def denied(fn):
    try:
        fn()
        return False
    except Denied:
        return True


def ids(results):
    return {r["id"] for r in results}


# --- layers
dev, dev2 = M("isc-junior-developer"), M("isc-senior-developer")
spectra_dev = M("spectra-junior-developer")
isc_ceo, isc_pm, isc_lead = M("isc-ceo"), M("isc-project-manager"), M("isc-team-lead")
isc_fin, isc_dir = M("isc-finance"), M("isc-engineering-director")
admin, owner, audit, steward = M("fed-hermes-admin"), M("owner"), M("fed-audit"), M("fed-knowledge")

a_note = dev.write("agent", "fact", "My note", "I prefer pytest")["id"]
d_note = dev.write("department", "procedure", "Run tests", "Always run pytest before commit")["id"]
f_note = isc_fin.write("department", "fact", "Budget", "ISC finance internal figures")["id"]
e_note = isc_pm.write("entity", "decision", "ISC stack", "ISC uses Python 3.12")["id"]
x_note = M("isc-qa").write("department", "fact", "QA note", "QA check list v1")["id"]

check("agent reads own agent memory", a_note in ids(dev.search()))
check("peer cannot read another agent's memory", a_note not in ids(dev2.search()))
check("leader above reads a junior's agent memory", a_note in ids(isc_lead.search()))
check("same department reads department memory", d_note in ids(dev2.search()))
check("other department cannot read it (finance vs engineering)", f_note not in ids(dev.search()))
check("director (60) reads all departments of own company", f_note in ids(isc_dir.search()))
check("whole company reads entity memory", e_note in ids(M("isc-receptionist").search()))
check("junior cannot write entity memory", denied(lambda: dev.write("entity", "fact", "x", "y")))

# --- isolation between companies
spectra_ids = ids(spectra_dev.search())
check("other company sees none of ISC's records", not spectra_ids & {a_note, d_note, f_note, e_note, x_note})
check("other company cannot read an ISC record by id", denied(lambda: spectra_dev.get(e_note)))
check("other company cannot write ISC entity", denied(lambda: M("spectra-ceo").write("entity:isc", "fact", "x", "y")))
check("Spectra CEO cannot override ISC decision", denied(lambda: M("spectra-ceo").override(e_note, "z", "r")))
check("Hermes Admin reads everything", {a_note, d_note, f_note, e_note} <= ids(admin.search()))
check("Audit reads everything", {a_note, f_note} <= ids(audit.search()))
check("Audit cannot write company memory", denied(lambda: audit.write("entity:isc", "fact", "x", "y")))

# --- admin memory grouped by position
ex = isc_ceo.write("admin:executive", "decision", "Exec only", "Plan to open a new service")["id"]
mg = isc_pm.write("admin:managers", "decision", "Managers", "Weekly sync on Monday")["id"]
check("CEO reads executive admin memory", ex in ids(isc_ceo.search()))
check("director cannot read executive admin memory", ex not in ids(isc_dir.search()))
check("director reads managers admin memory", mg in ids(isc_dir.search()))
check("developer reads no admin memory", not {ex, mg} & ids(dev2.search()))
check("manager cannot write executive group", denied(lambda: isc_pm.write("admin:executive", "fact", "x", "y")))
check("other company CEO cannot read ISC executive", ex not in ids(M("auvia-ceo").search()))
fs = admin.write("admin:federation:super", "decision", "Super", "Federation-wide plan")["id"]
check("only Hermes Admin/owner read federation super admin", fs not in ids(M("fed-coo").search()) and fs in ids(owner.search()))

# --- projects
check("junior cannot add project members", denied(lambda: dev.project("add", "erp", "isc-junior-developer")))
isc_pm.project("add", "erp", "isc-junior-developer")
p_note = dev.write("project:erp", "fact", "ERP DB", "ERP uses PostgreSQL")["id"]
check("project member reads project memory", p_note in ids(dev.search("PostgreSQL")))
check("non-member cannot read project memory", p_note not in ids(M("isc-qa").search()))
check("cannot add another company's agent to a project",
      denied(lambda: isc_pm.project("add", "erp", "spectra-junior-developer")))

# --- override by position
r = isc_ceo.override(e_note, "ISC uses Python 3.13", "upgrade approved")
check("CEO overrides a manager's decision", r["revision"] == 2)
check("old revision kept and marked superseded", admin.get(e_note)["record"]["superseded_by"] == r["id"])
check("history shows the change", admin.get(r["id"])["history"][0]["body"] == "ISC uses Python 3.12")
check("manager cannot override the CEO's revision", denied(lambda: isc_pm.override(r["id"], "back", "no")))
check("cannot override an already superseded record", denied(lambda: isc_ceo.override(e_note, "x", "y")))
check("author corrects own record", dev.override(a_note, "I prefer pytest -q", "detail")["revision"] == 2)
check("override needs a reason", denied(lambda: isc_ceo.override(d_note, "x", " ")))
check("same level cannot override peer", denied(lambda: M("isc-team-lead").override(
    M("isc-solution-architect").write("department", "decision", "ADR-1", "Use REST")["id"], "Use GraphQL", "prefer")))
check("Hermes Admin overrides across companies",
      admin.override(M("spectra-ceo").write("entity", "decision", "S", "Spectra plan A")["id"], "plan B", "owner asked")["revision"] == 2)

# --- protected kinds
v = M("isc-reviewer").write("department", "verdict", "Task 12 review", "FAIL: tests missing")["id"]
check("CEO cannot flip a review verdict by rank", denied(lambda: isc_ceo.override(v, "PASS", "ship it")))
check("Hermes Admin cannot flip a review verdict", denied(lambda: admin.override(v, "PASS", "ship it")))
check("a new review (verifier) can replace the verdict", M("isc-verifier").override(v, "PASS after re-test", "re-review")["revision"] == 2)
check("only reviewers write verdicts", denied(lambda: dev.write("department", "verdict", "x", "PASS")))
g = owner.write("entity:federation", "owner_goal", "Goal", "Local first, cloud only when needed")["id"]
check("agents cannot write owner goals", denied(lambda: admin.write("entity:federation", "owner_goal", "x", "y")))
check("Hermes Admin cannot override owner goals", denied(lambda: admin.override(g, "cloud first", "faster")))

# --- secrets and lessons
check("secrets are refused", denied(lambda: dev.write("agent", "fact", "key", "GROQ key gsk_" + "a" * 40)))
check("password assignments are refused", denied(lambda: dev.write("agent", "fact", "login", "password: Hunter2024!")))
check("agents cannot write global directly", denied(lambda: isc_ceo.write("global", "lesson", "x", "y")))
lid = dev.lesson("propose", "Run the full test suite before every commit; partial runs hid failures.")["lesson_id"]
check("non-steward cannot approve lessons", denied(lambda: isc_ceo.lesson("approve", lesson_id=lid)))
gid = steward.lesson("approve", lesson_id=lid,
                     sanitized_text="Run the full test suite before every commit.")["global_record"]
check("approved lesson is visible to another company", gid in ids(spectra_dev.search("test suite")))
check("lesson carries its department tag", spectra_dev.get(gid)["record"]["tags"] == "department:engineering")

# --- audit trail
log = audit.audit_log(500)
check("denials are audited", any(e["result"] == "denied" for e in log))
check("overrides are audited", any(e["action"] == "override" for e in log))
check("developer cannot read the audit log", denied(lambda: dev.audit_log()))

# --- customers
rec_isc, cs_isc = M("isc-receptionist"), M("isc-customer-success")
c1 = rec_isc.customer("register", name="Ahmad Co", channel="whatsapp", handle="+96170000001", language="ar",
                      details="Prefers Arabic, wants an ERP for 3 branches")["id"]
c2 = rec_isc.customer("register", name="Beta LLC", channel="whatsapp", handle="+96170000002")["id"]
n1 = cs_isc.write(f"customer:{c1}", "preference", "Reports", "Wants weekly PDF reports on Sunday")["id"]
n2 = cs_isc.write(f"customer:{c2}", "fact", "Budget", "Beta budget is limited this quarter")["id"]
check("customer-facing role reads customer memory", {n1, n2} <= ids(cs_isc.search()))
check("developer outside the customer's projects cannot read it", not {n1, n2} & ids(dev2.search()))
check("other company cannot read ISC customers", not {n1, n2} & ids(M("spectra-customer-success").search()))
check("other company cannot get an ISC customer", denied(lambda: M("spectra-ceo").customer("get", c1)))
check("duplicate channel identity refused", denied(lambda: rec_isc.customer("register", name="X", channel="whatsapp",
                                                                          handle="+96170000001")))
check("find works by exact handle only", [c["id"] for c in rec_isc.customer("find", handle="+96170000001")] == [c1]
      and rec_isc.customer("find", handle="+9617") == [])
check("receptionist cannot browse the customer list", denied(lambda: rec_isc.customer("list")))
check("developer cannot register customers", denied(lambda: dev.customer("register", name="Y")))
isc_pm.customer("link_project", c1, project="erp")
check("project member reaches the linked customer's memory", n1 in ids(dev.search()) and n2 not in ids(dev.search()))
bound = Memory("isc-receptionist", DB, customer=c1)
check("customer-bound session sees its customer", n1 in ids(bound.search()))
check("customer-bound session cannot see another customer", n2 not in ids(bound.search())
      and denied(lambda: bound.get(n2)) and denied(lambda: bound.customer("get", c2)))
check("customer-bound session cannot look up other customers",
      [c["id"] for c in bound.customer("find", handle="+96170000002")] == [c1])
rx = M("isc-receptionist").write("agent", "fact", "Own note", "Receptionist working note")["id"]
w = bound.write("agent", "preference", "Contact day", "Prefers Sunday")
check("customer-bound write lands in that customer's memory", w["scope"] == f"customer:isc:{c1}")
check("customer-bound session cannot read the agent's own notes", rx not in ids(bound.search()) and denied(lambda: bound.get(rx)))
check("customer-bound session cannot register customers", denied(lambda: bound.customer("register", name="Z")))
check("cannot bind an agent to another company's customer", denied(lambda: Memory("spectra-receptionist", DB, customer=c1)))
check("only the owner issues login codes", denied(lambda: isc_ceo.customer_login_code(c1)))
code = owner.customer_login_code(c1)["code"]
check("login code resolves to its customer", owner.customer_by_code(code)["id"] == c1)
check("wrong login code is refused", owner.customer_by_code("wrong-code") is None)
check("login code is not stored in clear", code not in json.dumps(admin.customer("get", c1)))

# --- cross-company work orders (R03.05, R11.05), spec scenario AUVIA -> TabbeQ AI
au_pm, au_ceo, au_dir = M("auvia-project-manager"), M("auvia-ceo"), M("auvia-delivery-director")
tq_dir, tq_sr, tq_jr = M("tabbeqai-delivery-director"), M("tabbeqai-senior-developer"), M("tabbeqai-junior-developer")
spec_rec = au_dir.write("entity", "fact", "Vision API spec", "Endpoints: /detect, /classify; Arabic labels")["id"]
secret_rec = au_ceo.write("admin:executive", "decision", "Pricing", "Margin target 40%")["id"]
other_rec = au_dir.write("entity", "fact", "Unrelated", "AUVIA office move in May")["id"]
check("supplier cannot read lead data before any work order", denied(lambda: tq_sr.get(spec_rec)))
check("junior cannot create work orders", denied(lambda: M("auvia-junior-developer").work_order(
    "create", supplier="tabbeqai", title="x", work_scope="y", acceptance_criteria="z")))
check("cannot grant admin records", denied(lambda: au_ceo.work_order(
    "create", supplier="tabbeqai", title="x", work_scope="y", acceptance_criteria="z", grant_records=[secret_rec])))
check("work order to own company refused", denied(lambda: au_pm.work_order(
    "create", supplier="auvia", title="x", work_scope="y", acceptance_criteria="z")))
wo = au_pm.work_order("create", supplier="tabbeqai", title="Arabic OCR model", work_scope="Train and serve Arabic OCR",
                      acceptance_criteria="CER < 5% on the test set; /ocr endpoint returns JSON",
                      permitted_sharing="Only the Vision API spec", outputs="Model card, endpoint URL, test report",
                      cost_allocation="AUVIA pays TabbeQ AI per accepted output", grant_records=[spec_rec])
wid = wo["id"]
check("manager's work order waits for the lead CEO", wo["status"] == "draft")
check("supplier cannot accept a draft", denied(lambda: tq_dir.work_order("accept", wid, team=["tabbeqai-senior-developer"])))
check("only the lead CEO approves subcontracting", denied(lambda: au_dir.work_order("approve", wid)))
check("lead CEO approves", au_ceo.work_order("approve", wid)["status"] == "requested")
check("grants are not active before acceptance", denied(lambda: tq_sr.get(spec_rec)))
check("supplier developer cannot accept", denied(lambda: tq_sr.work_order("accept", wid, team=["tabbeqai-senior-developer"])))
check("team must be supplier agents", denied(lambda: tq_dir.work_order("accept", wid, team=["auvia-qa"])))
check("supplier leadership accepts with a team",
      tq_dir.work_order("accept", wid, team=["tabbeqai-senior-developer"])["status"] == "accepted")
check("supplier team reads exactly the granted record", tq_sr.get(spec_rec)["record"]["id"] == spec_rec)
check("granted record shows up in the team's search", spec_rec in ids(tq_sr.search("Vision")))
check("supplier team still cannot read ungranted lead data", denied(lambda: tq_sr.get(other_rec))
      and other_rec not in ids(tq_sr.search()))
check("supplier agent outside the team cannot read the grant", denied(lambda: tq_jr.get(spec_rec)))
check("supplier cannot override a granted lead record", denied(lambda: tq_sr.override(spec_rec, "changed", "r")))
check("third company sees nothing of the work order", denied(lambda: M("spectra-ceo").work_order("get", wid))
      and spec_rec not in ids(M("spectra-ceo").search()))
check("supplier team starts", tq_sr.work_order("start", wid)["status"] == "in_progress")
q_id = tq_sr.write(f"workorder:{wid}", "fact", "Question", "Which Arabic fonts are in scope?")["id"]
check("lead side reads the supplier's message in the shared scope", q_id in ids(au_pm.search(scope=f"workorder:{wid}")))
a_id = au_pm.write(f"workorder:{wid}", "decision", "Answer", "Naskh and Ruqaa only")["id"]
check("supplier reads the lead's answer", a_id in ids(tq_sr.search(scope=f"workorder:{wid}")))
check("non-team supplier agent cannot read the shared scope", a_id not in ids(tq_jr.search()))
check("lead cannot deliver for the supplier", denied(lambda: au_pm.work_order("deliver", wid, delivery="x")))
check("supplier links its Kanban task", tq_sr.work_order("link_task", wid, task="tabbeqai:t_0123abcd")["kanban_task"]
      == "tabbeqai:t_0123abcd")
tq_sr.work_order("deliver", wid, delivery="Model v1, CER 6.1%, endpoint /ocr, report in shared scope")
check("rework sends it back to the supplier", au_pm.work_order("rework", wid, reason="CER above 5%")["status"] == "in_progress")
tq_sr.work_order("deliver", wid, delivery="Model v2, CER 4.2%, report in shared scope")
rv = au_pm.work_order("revise", wid, acceptance_criteria="CER < 3%")
check("revised requirements pause the work (R11.05)", rv["status"] == "revision_pending" and rv["requirements_revision"] == 2)
check("grants are paused while the revision waits", denied(lambda: tq_sr.get(spec_rec)))
tq_dir.work_order("accept", wid, team=["tabbeqai-senior-developer"])
tq_sr.work_order("start", wid)
tq_sr.work_order("deliver", wid, delivery="Model v3, CER 2.7%")
check("lead closes after accepting the delivery", au_pm.work_order("close", wid, reason="accepted")["status"] == "closed")
check("grants end when the work order closes", denied(lambda: tq_sr.get(spec_rec)))
check("shared scope is read-only after close", denied(lambda: tq_sr.write(f"workorder:{wid}", "fact", "x", "y")))
check("history of the work order is kept", len(au_pm.work_order("get", wid)["events"]) >= 10)
check("Hermes Admin and Audit see all work orders", wid in [w["id"] for w in audit.work_order("list")])
check("customer-bound sessions cannot touch work orders", denied(lambda: bound.work_order("list")))

check("whoami is JSON-serialisable for every kind of caller (console Memory tab)",
      all(json.dumps(M(p).whoami()) for p in ("owner", "fed-audit", "isc-ceo", "tabbeqai-senior-developer")))
check("whoami lists the work orders a supplier agent takes part in", wid in M("tabbeqai-senior-developer").whoami()["wos"])

# --- MCP protocol smoke test
srv = subprocess.Popen([sys.executable, "-I", str(Path(__file__).parent / "memory_mcp.py"), "--agent", "spectra-qa",
                        "--db", str(DB)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8")
reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "memory_get", "arguments": {"id": e_note}}}]
out, _ = srv.communicate("\n".join(json.dumps(r) for r in reqs) + "\n", timeout=30)
resp = [json.loads(line) for line in out.splitlines()]
check("MCP lists 11 tools", len(resp[1]["result"]["tools"]) == 11)
check("MCP denies cross-company read", resp[2]["result"]["isError"] and "denied" in resp[2]["result"]["content"][0]["text"])

srv = subprocess.Popen([sys.executable, "-I", str(Path(__file__).parent / "memory_mcp.py"), "--agent", "isc-receptionist",
                        "--db", str(DB)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
                       env={**__import__("os").environ, "FED_CUSTOMER": c1})
reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "memory_customer",
                                                                       "arguments": {"action": "get", "customer_id": c2}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "memory_whoami", "arguments": {}}}]
out, _ = srv.communicate("\n".join(json.dumps(r) for r in reqs) + "\n", timeout=30)
resp = [json.loads(line) for line in out.splitlines()]
check("MCP with FED_CUSTOMER denies another customer", resp[1]["result"]["isError"])
check("MCP with FED_CUSTOMER knows whom it serves", c1 in resp[2]["result"]["content"][0]["text"])

# --- review tools through MCP: the verdict is recorded under the server's fixed identity
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workflow"))
from tasks import Workflow  # noqa: E402

WFDB = DB.parent / "workflow.db"
wft = Workflow(db_path=WFDB, home=DB.parent, hermes_cmd="does-not-run", probe=lambda p: ("ok", ""))
task = wft.create("owner", "isc", "Review tools", "isc-junior-developer", "docs", "in", "out", "criteria met")


def mcp(agent, calls_, env=None):
    p = subprocess.Popen([sys.executable, "-I", str(Path(__file__).parent / "memory_mcp.py"), "--agent", agent,
                          "--db", str(DB), "--workflow-db", str(WFDB)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         text=True, encoding="utf-8", env=env)
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}] + [
        {"jsonrpc": "2.0", "id": i + 2, "method": "tools/call", "params": {"name": n, "arguments": a}}
        for i, (n, a) in enumerate(calls_)]
    out, _ = p.communicate("\n".join(json.dumps(r) for r in reqs) + "\n", timeout=60)
    return [json.loads(line)["result"] for line in out.splitlines()][1:]


r = mcp("isc-reviewer", [("task_get", {"task_id": task["id"]})])
check("MCP task_get: an agent reads its own company's task", not r[0]["isError"] and "criteria met" in r[0]["content"][0]["text"])
r = mcp("spectra-reviewer", [("task_get", {"task_id": task["id"]}),
                             ("task_review", {"task_id": task["id"], "role": "reviewer", "commit": "abcdef1",
                                              "verdict": "pass", "findings": "pretending to be ISC"})])
check("MCP task_get: another company cannot read the task", r[0]["isError"] and "own company" in r[0]["content"][0]["text"])
check("MCP task_review: another company cannot review", r[1]["isError"] and "own company" in r[1]["content"][0]["text"])
r = mcp("isc-qa", [("task_review", {"task_id": task["id"], "role": "reviewer", "commit": "abcdef1", "verdict": "pass",
                                    "findings": "I am the reviewer, trust me"})])
check("MCP task_review: the role comes from the fixed identity, not the arguments",
      r[0]["isError"] and "only a reviewer" in r[0]["content"][0]["text"])
r = mcp("isc-reviewer", [("task_review", {"task_id": task["id"], "role": "reviewer", "commit": "abcdef1",
                                          "verdict": "pass", "findings": "reviewed before any work existed"})])
check("MCP task_review: a task that is not REVIEWING cannot be reviewed",
      r[0]["isError"] and "not reviewing" in r[0]["content"][0]["text"].lower())
check("MCP: no review row was written by any refused call",
      wft.db.execute("SELECT COUNT(*) FROM task_reviews").fetchone()[0] == 0)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
