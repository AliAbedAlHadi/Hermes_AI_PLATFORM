"""Adversarial tests for Phase 3 (company isolation). Disposable fixtures only: a temporary Hermes home,
temporary company workspaces, temporary memory/workflow databases. The guard is executed as the real hook
script, exactly as Hermes runs it. A case passes only when the operation is actually refused.

Cases marked OPEN demonstrate a bypass that only an operating-system sandbox can close; they are reported,
not hidden, and do not count as passes.

    python -I test_security.py
"""
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "memory"))
sys.path.insert(0, str(HERE.parent / "workflow"))
from fedmem import Denied, Memory  # noqa: E402
from tasks import TaskError, Workflow  # noqa: E402

T = Path(tempfile.mkdtemp(prefix="sec-"))
HOME, WS = T / "home", T / "ws"
for c in ("isc", "auvia", "spectra"):
    (WS / c).mkdir(parents=True)
    (WS / c / "plan.md").write_text(f"{c} confidential plan\n")
    (HOME / "kanban" / "boards" / c / "workspaces").mkdir(parents=True)
for p in ("isc-executor", "auvia-project-manager", "fed-hermes-admin", "fed-audit"):
    (HOME / "profiles" / p).mkdir(parents=True)
    (HOME / "profiles" / p / ".env").write_text("SOME_KEY=not-a-real-key\n")
(HOME / "federation").mkdir(parents=True)
(WS / "isc" / "app.db").write_text("x")
LINK = WS / "isc" / "shortcut"  # a junction inside ISC's workspace pointing at AUVIA's (no admin rights needed)
if os.name == "nt":
    subprocess.run(["cmd", "/c", "mklink", "/J", str(LINK), str(WS / "auvia")], capture_output=True)
else:
    os.symlink(WS / "auvia", LINK)
DB = HOME / "federation" / "memory.db"
results = {"pass": 0, "fail": 0, "open": 0}


def check(name, ok, info=""):
    results["pass" if ok else "fail"] += 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {info}" if info and not ok else ""))


def open_risk(name, bypassed, info=""):
    """A bypass the guard cannot stop (needs an OS sandbox): reported as OPEN, never as PASS."""
    if bypassed:
        results["open"] += 1
        print(f"OPEN  {name}  -- {info}")
    else:
        check(name, True)


def raises(exc, fn, text=""):
    try:
        fn()
        return False
    except exc as e:
        return text.lower() in str(e).lower()


def company(agent):
    return "federation" if agent.startswith("fed-") else agent.split("-", 1)[0]


def guard(agent, tool, args, cwd=None, profile=None, raw=None):
    payload = raw if raw is not None else json.dumps({"hook_event_name": "pre_tool_call", "tool_name": tool,
                                                      "tool_input": args, "cwd": str(cwd or WS / company(agent)),
                                                      "profile": profile or agent})
    r = subprocess.run([sys.executable, "-I", str(HERE / "fed_guard.py"), "--agent", agent, "--home", str(HOME),
                        "--workspaces", str(WS), "--db", str(DB)], input=payload.encode(), capture_output=True)
    return r.returncode, r.stdout.decode()


def blocked(agent, tool, args, **kw):
    code, out = guard(agent, tool, args, **kw)
    return code == 2 and '"block"' in out


def allowed(agent, tool, args, **kw):
    return guard(agent, tool, args, **kw)[0] == 0


print("== 1. ISC agent reads AUVIA files")
check("absolute path into AUVIA's workspace refused", blocked("isc-executor", "read_file", {"path": str(WS / "auvia" / "plan.md")}))
check("search over AUVIA's workspace refused", blocked("isc-executor", "search_files", {"pattern": "plan", "path": str(WS / "auvia")}))
check("write into AUVIA's workspace refused", blocked("isc-executor", "write_file", {"path": str(WS / "auvia" / "x.txt")}))
check("ISC reads its own workspace (legitimate)", allowed("isc-executor", "read_file", {"path": str(WS / "isc" / "plan.md")}))
check("ISC writes in its own Kanban scratch space (legitimate)", allowed(
    "isc-executor", "write_file", {"path": str(HOME / "kanban" / "boards" / "isc" / "workspaces" / "t1" / "out.txt")}))
check("Hermes Admin may read another company's workspace (explicit privilege)",
      allowed("fed-hermes-admin", "read_file", {"path": str(WS / "auvia" / "plan.md")}))
check("...but not write it", blocked("fed-hermes-admin", "write_file", {"path": str(WS / "auvia" / "plan.md")}))

print("== 2. AUVIA agent modifies the Spectra board")
for tool in ("kanban_create", "kanban_complete", "kanban_block", "kanban_comment", "kanban_list"):
    check(f"{tool} on board 'spectra' refused", blocked("auvia-project-manager", tool,
                                                        {"board": "spectra", "title": "x", "task_id": "t_1"}))
check("own board allowed (legitimate)", allowed("auvia-project-manager", "kanban_create", {"board": "auvia", "title": "x"}))
check("no board given (the worker's pinned board) allowed", allowed("auvia-project-manager", "kanban_complete", {"task_id": "t_1"}))
check("board name case and space tricks refused", blocked("auvia-project-manager", "kanban_create", {"board": " Spectra ", "title": "x"}))
check("federation COO may coordinate across boards (explicit privilege)", allowed("fed-coo", "kanban_list", {"board": "spectra"}))
check("other federation officers may not", blocked("fed-audit", "kanban_create", {"board": "spectra", "title": "x"}))
check("terminal 'hermes kanban --board spectra' refused", blocked("auvia-project-manager", "terminal",
                                                                 {"command": "hermes kanban --board spectra create pwned"}))

print("== 2b. Kanban overrides inside the own board (Phase 4)")
check("assigning work to another company's agent refused", blocked(
    "isc-executor", "kanban_create", {"title": "x", "assignee": "spectra-ceo"}))
check("assigning work inside the own company allowed", allowed("isc-executor", "kanban_create", {"title": "x", "assignee": "isc-qa"}))
check("the '[tsk_' title prefix is reserved for the workflow", blocked(
    "isc-executor", "kanban_create", {"title": "[tsk_deadbeef] fake federation task", "assignee": "isc-qa"}))
check("federation COO may assign across companies (explicit privilege)", allowed(
    "fed-coo", "kanban_create", {"title": "x", "assignee": "spectra-qa", "board": "spectra"}))
os.environ["HERMES_KANBAN_TASK"] = "t_00000001"
check("a worker may link its own task", allowed("isc-executor", "kanban_link", {"parent_id": "t_9", "child_id": "t_00000001"}))
check("a worker may not link another task", blocked("isc-executor", "kanban_link", {"parent_id": "t_9", "child_id": "t_00000002"}))
del os.environ["HERMES_KANBAN_TASK"]
check("linking other tasks needs a manager", blocked("isc-executor", "kanban_link", {"parent_id": "t_1", "child_id": "t_2"}))
check("a team lead (level 50) may link tasks of its board", allowed("isc-team-lead", "kanban_link", {"parent_id": "t_1", "child_id": "t_2"}))
for cmd in ("hermes kanban complete t_1234", "hermes -p isc-executor kanban archive t_1", "hermes.exe kanban unblock t_9"):
    check(f"terminal '{cmd}' refused", blocked("isc-executor", "terminal", {"command": cmd}))
check("read-only 'hermes kanban list' still allowed", allowed("isc-executor", "terminal", {"command": "hermes kanban list"}))

print("== 3. Direct open of the shared memory database")
check("file tool read of memory.db refused", blocked("isc-executor", "read_file", {"path": str(DB)}))
check("terminal command naming memory.db refused", blocked(
    "isc-executor", "terminal", {"command": f'python -c "import sqlite3;sqlite3.connect(r\'{DB}\')"'}))
check("execute_code naming the federation folder refused", blocked(
    "isc-executor", "execute_code", {"code": f"open(r'{HOME / 'federation' / 'x'}')"}))
check("a *.db inside the own workspace is refused through file tools", blocked("isc-executor", "read_file", {"path": str(WS / "isc" / "app.db")}))
# OPEN-1: the guard reads command text, so a path assembled at run time passes it. To show the real impact,
# the same technique is executed against a disposable canary "federation" database in this test's temp folder.
canary_db = HOME / "federation" / "memory.db"
import sqlite3 as _sq
with _sq.connect(canary_db) as _c:
    _c.execute("CREATE TABLE IF NOT EXISTS canary (v TEXT)")
    _c.execute("INSERT INTO canary VALUES ('CANARY-FEDERATION-SECRET')")
obf = ("import sqlite3,os;p=os.path.join(os.environ['FED_TEST_HOME'],'feder'+'ation','mem'+'ory'+'.db');"
       "print(sqlite3.connect(p).execute('select v from canary').fetchone()[0])")
passes_guard = allowed("isc-executor", "execute_code", {"code": obf})
ran = subprocess.run([sys.executable, "-I", "-c", obf], capture_output=True, text=True, env={**os.environ, "FED_TEST_HOME": str(HOME)})
open_risk("OPEN-1: a run-time-built path passes the guard AND reads the federation database",
          passes_guard and "CANARY-FEDERATION-SECRET" in ran.stdout,
          "proven on a canary database: the guard sees text only; needs an OS sandbox")

print("== 4. Another company's identity in a request")
check("hook bound to isc-executor refuses a call reported by another profile",
      blocked("isc-executor", "read_file", {"path": str(WS / "isc" / "plan.md")}, profile="auvia-project-manager"))
check("malformed hook payload refused (fail closed)", guard("isc-executor", "", {}, raw="not json")[0] == 2)
check("terminal spawning the memory server with another identity refused", blocked(
    "isc-executor", "terminal", {"command": "python memory_mcp.py --agent fed-hermes-admin"}))
mem_isc, mem_auvia = Memory("isc-ceo", DB), Memory("auvia-ceo", DB)
rid = mem_isc.write("entity", "decision", "ISC pricing", "ISC margin plan")["id"]
check("memory: AUVIA cannot read an ISC record by id", raises(Denied, lambda: mem_auvia.get(rid)))
check("memory: AUVIA cannot write ISC's scope by naming it", raises(Denied, lambda: mem_auvia.write("entity:isc", "fact", "x", "y")))
WFDB = T / "wf.db"
wf = Workflow(db_path=WFDB, home=HOME, hermes_cmd="no-dispatch", probe=lambda p: ("ok", ""), workspaces=WS)
task = wf.create("owner", "isc", "Sec task", "isc-junior-developer", "docs", "in", "out", "criteria")
check("workflow: an AUVIA reviewer cannot review an ISC task", raises(TaskError, lambda: wf.review(
    "auvia-reviewer", task["id"], "reviewer", "pass", "pretending to be ISC", "abcdef1"), "own company"))
r = subprocess.run([sys.executable, "-I", str(HERE.parent / "memory" / "memory_mcp.py"), "--agent", "auvia-reviewer",
                    "--db", str(DB), "--workflow-db", str(WFDB)], input=(json.dumps(
    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "task_get", "arguments": {"task_id": task["id"]}}})
                       + "\n").encode(), capture_output=True)
check("MCP: an AUVIA agent cannot read an ISC task", b'"isError": true' in r.stdout and b"own company" in r.stdout)

print("== 5. Path traversal and link escape")
check("'..' traversal from ISC to AUVIA refused", blocked("isc-executor", "read_file", {"path": "../auvia/plan.md"}))
check("mixed separators traversal refused", blocked("isc-executor", "read_file", {"path": "..\\auvia/plan.md"}))
check("junction/symlink inside ISC that points to AUVIA refused (realpath)", blocked(
    "isc-executor", "read_file", {"path": str(LINK / "plan.md")}) if LINK.exists() else False, "junction not created")
check("patch with a target file outside the workspace refused", blocked(
    "isc-executor", "patch", {"mode": "patch", "patch": "*** Begin Patch\n*** Update File: ../auvia/plan.md\n@@\n-a\n+b\n*** End Patch"}))
check("another agent's profile folder refused", blocked("isc-executor", "read_file", {"path": str(HOME / "profiles" / "fed-hermes-admin" / "x")}))
check("own profile .env refused (secrets)", blocked("isc-executor", "read_file", {"path": str(HOME / "profiles" / "isc-executor" / ".env")}))

print("== 6. Customer record outside authorisation")
cus = mem_isc.customer("register", name="Test Customer", channel="portal", handle="t-1", details="test only")["id"]
check("AUVIA cannot read an ISC customer", raises(Denied, lambda: Memory("auvia-customer-success", DB).customer("get", cus)))
check("an ISC developer outside the customer's projects cannot read it", raises(Denied, lambda: Memory("isc-junior-developer", DB).customer("get", cus)))
bound = Memory("isc-receptionist", DB, customer=cus)
other = mem_isc.customer("register", name="Other Customer", channel="portal", handle="t-2")["id"]
check("a customer-bound session cannot read another customer", raises(Denied, lambda: bound.customer("get", other)))

print("== 7 & 8. Work-order scope and access after close")
spec = Memory("auvia-delivery-director", DB).write("entity", "fact", "Spec", "granted spec")["id"]
secret = Memory("auvia-delivery-director", DB).write("entity", "fact", "Internal", "not granted")["id"]
au_pm, au_ceo, tq_dir, tq_sr = (Memory(p, DB) for p in ("auvia-project-manager", "auvia-ceo", "tabbeqai-delivery-director",
                                                         "tabbeqai-senior-developer"))
wo = au_pm.work_order("create", supplier="tabbeqai", title="OCR", work_scope="s", acceptance_criteria="a", grant_records=[spec])
au_ceo.work_order("approve", wo["id"])
tq_dir.work_order("accept", wo["id"], team=["tabbeqai-senior-developer"])
check("participant reads the granted record (legitimate)", tq_sr.get(spec)["record"]["id"] == spec)
check("participant cannot read a record outside the grant", raises(Denied, lambda: Memory("tabbeqai-senior-developer", DB).get(secret)))
tq_sr.work_order("start", wo["id"])
tq_sr.work_order("deliver", wo["id"], delivery="done")
au_pm.work_order("close", wo["id"], reason="ok")
check("access to the grant ends when the work order closes", raises(Denied, lambda: Memory("tabbeqai-senior-developer", DB).get(spec)))
check("the shared scope becomes read-only after close", raises(Denied, lambda: Memory("tabbeqai-senior-developer", DB).write(
    f"workorder:{wo['id']}", "fact", "late", "after close")))

print("== 9. Lower role override; high level in another company")
dec = Memory("isc-team-lead", DB).write("department:isc:engineering", "decision", "Stack", "Python")["id"]
check("ISC junior cannot override the team lead", raises(Denied, lambda: Memory("isc-junior-developer", DB).override(dec, "Go", "prefer")))
check("AUVIA CEO (level 70) cannot override an ISC decision", raises(Denied, lambda: mem_auvia.override(dec, "Go", "rank")))

print("== 10. Forcing SUCCEEDED or faking review evidence")
check("owner cannot request SUCCEEDED", raises(TaskError, lambda: wf.transition(task["id"], "SUCCEEDED", "owner", 1)))
check("the system cannot pass the gate without verdicts", raises(TaskError, lambda: (
    wf.db.execute("UPDATE tasks SET state='REVIEWING', current_commit='abc1234', branch='wt/x' WHERE id=?", (task["id"],)),
    wf.db.commit(), wf.transition(task["id"], "SUCCEEDED", "system", wf._row(task["id"])["state_version"], system=True))))
check("a non-reviewer role cannot record a verdict", raises(TaskError, lambda: wf.review(
    "isc-qa", task["id"], "reviewer", "pass", "I am the reviewer, trust me", "abc1234"), "only a reviewer"))
check("file tools cannot reach the workflow database", blocked("isc-reviewer", "write_file", {"path": str(WFDB)}))

print("== 11. Subprocess bypass of file/tool restrictions")
check("terminal reading another company's workspace by path refused", blocked(
    "isc-executor", "terminal", {"command": f'type "{WS / "auvia" / "plan.md"}"'}))
check("process_manage starting a reader of AUVIA's files refused", blocked(
    "isc-executor", "process_manage", {"action": "start", "command": f"cat {WS / 'auvia'}/plan.md"}))
# OPEN-2: a child process with an assembled path; executed for real against the disposable AUVIA workspace.
code = ("import subprocess,sys,os;p=os.path.join(os.environ['FED_TEST_WS'],'au'+'via','plan.md');"
        "subprocess.run([sys.executable,'-c','print(open(r\"'+p+'\").read())'])")
passes_guard = allowed("isc-executor", "execute_code", {"code": code})
ran = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, env={**os.environ, "FED_TEST_WS": str(WS)})
open_risk("OPEN-2: a subprocess with an assembled path passes the guard AND reads another company's file",
          passes_guard and "auvia confidential" in ran.stdout, "proven on a disposable workspace; needs an OS sandbox")
r = subprocess.run([sys.executable, "-c", f"print(open(r'{WS / 'auvia' / 'plan.md'}').read())"], capture_output=True, text=True)
open_risk("OPEN-3: the OS itself lets the shared Windows user read every company's files", "auvia confidential" in r.stdout,
          "every agent runs as the same Windows user; NTFS grants it full control of all workspaces")

print("== 11b. The guard cannot be switched off through an agent's own authorised folders")
for f in ("config.yaml", "SOUL.md", "profile.yaml", "config.yaml.bak-guard", "shell-hooks-allowlist.json", ".env"):
    check(f"write_file own profile {f} refused", blocked("isc-executor", "write_file",
                                                          {"path": str(HOME / "profiles" / "isc-executor" / f)}))
check("patch on the own config.yaml refused", blocked("isc-executor", "patch", {
    "mode": "patch", "patch": f"*** Begin Patch\n*** Update File: {HOME / 'profiles' / 'isc-executor' / 'config.yaml'}\n@@\n-a\n+b\n*** End Patch"}))
check("reading the own config.yaml still allowed", allowed("isc-executor", "read_file",
                                                          {"path": str(HOME / "profiles" / "isc-executor" / "config.yaml")}))
check("ordinary notes in the own profile folder still writable", allowed("isc-executor", "write_file",
                                                                         {"path": str(HOME / "profiles" / "isc-executor" / "notes.md")}))
check("the guard's own source is outside every agent root", blocked("isc-executor", "write_file", {"path": str(HERE / "fed_guard.py")}))
check("terminal editing the own config.yaml refused (defence in depth)", blocked("isc-executor", "terminal", {
    "command": f"echo x >> {HOME / 'profiles' / 'isc-executor' / 'config.yaml'}"}))

print("== 12. Secrets through logs or configuration endpoints")
sys.path.insert(0, str(HERE.parent / "tests"))
from console_harness import Console  # noqa: E402

with Console(port=8769) as con:  # an isolated console: never the real one
    anon = con.client()
    for path in ("/api/boards/isc/tasks/t_00000000/log", "/api/agents", "/api/memory/audit",
                 "/api/ftasks/tsk_00000000/usage", "/api/admin/health", "/api/admin/sessions", "/api/conversations"):
        code = anon.get(path)[0]
        check(f"anonymous GET {path} refused", code == 401, f"got {code}")
    out = con.output()
    check("console output carries no API key", "gsk_" not in out and "API_KEY=" not in out)
check("terminal printing a .env file refused", blocked("isc-executor", "terminal", {"command": "type ..\\..\\.env"}))
check("terminal reading the owner hash refused", blocked("isc-executor", "terminal", {"command": "type owner.json"}))
audit_rows = Memory("owner", DB).audit_log(500)
guard_rows = [a for a in audit_rows if a["action"].startswith("guard.")]
check("guard refusals are audited with actor, action and outcome", guard_rows and all(
    a["result"] == "denied" and a["actor"] for a in guard_rows))
check("audit rows carry no secret content", not any("not-a-real-key" in (a["detail"] or "") for a in audit_rows))

print(f"\n{results['pass']} passed, {results['fail']} failed, {results['open']} OPEN (need an OS sandbox)")
sys.exit(1 if results["fail"] else 0)
