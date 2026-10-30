"""OPEN-1/2/3 isolation probe (investigation only; nothing is fixed here).

Builds a synthetic federation layout in a fresh temporary folder (fake Hermes home, workspaces, profiles, Hermes
source and guard script, all holding canary text), then:

  A. asks the REAL guard (security/fed_guard.py) what it decides for each access path  -> application-level layer
  B. performs the same access as an ordinary child / grandchild process of this user     -> what the OS allows
  C. records read-only facts about this host (user groups, ACLs of the real folders, listening ports, Docker/WSL
     state, one worktree .git pointer). Nothing on the host is changed, no real file content is read, no real
     database is opened, no service is called (a TCP connect to a local port is opened and closed at once).

    python -I security/isolation_probe.py [--json out.json]

Every canary and file it writes is under the temporary folder, which is removed at the end.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from fed_guard import Guard  # noqa: E402

PY = sys.executable
T = Path(tempfile.mkdtemp(prefix="fed-isoprobe-"))
HOME, WS = T / "hermes", T / "workspaces"
CANARY = "CANARY-" + os.urandom(6).hex()
rows, facts = [], {}


def put(p, text=CANARY):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------- synthetic layout (canaries only)
for c in ("isc", "auvia", "tabbeqai", "spectra", "spectrasolutions"):
    put(WS / c / "README.md", f"{c} workspace")
put(WS / "isc" / "src" / "app.py", "print('isc app')")
OTHER = put(WS / "auvia" / "private" / "customer-notes.txt")              # another company's file
MEMDB = put(HOME / "federation" / "memory.db")                            # federation database (canary text)
OWNER = put(HOME / "federation" / "owner.json")
AUTH = put(HOME / "auth.json")                                            # Hermes provider auth store
ENVF = put(HOME / ".env")
BOARD = put(HOME / "kanban" / "boards" / "auvia" / "kanban.db")           # another company's Kanban board
OWNCFG = put(HOME / "profiles" / "isc-executor" / "config.yaml", "hooks: fed_guard")
OTHERCFG = put(HOME / "profiles" / "auvia-ceo" / "config.yaml", "hooks: fed_guard")
SRCF = put(HOME / "hermes-agent" / "model_tools.py", "def handle_function_call(): pass  # dispatches hooks")
GUARD = put(T / "Documents" / "Hermes" / "security" / "fed_guard.py", "# guard source")
BACKUP = put(T / "hermes-backups" / "phase5" / "memory.db")
SHARED_TMP = put(T / "shared-temp" / "auvia-build.log")                   # a shared temp folder
g = Guard("isc-executor", HOME, WS, db=T / "audit.db")                    # refusals are audited into a temp DB
ISC = WS / "isc"


def guard(tool, args, cwd=ISC):
    why = g.decide({"tool_name": tool, "tool_input": args, "cwd": str(cwd)})
    return "denied" if why else "allowed"


def child(code, grandchild=False):
    """Run Python code as a child (or grandchild) process of this user; returns stdout."""
    if grandchild:
        code = f"import subprocess,sys; print(subprocess.run([sys.executable,'-I','-c',{code!r}],capture_output=True,text=True).stdout,end='')"
    r = subprocess.run([PY, "-I", "-c", code], capture_output=True, text=True, timeout=60, cwd=ISC)
    return (r.stdout + r.stderr).strip()


def read_via_os(path, grandchild=False):
    out = child(f"print(open(r'{path}',encoding='utf-8').read())", grandchild)
    return "read" if CANARY in out else "blocked"


def write_via_os(path):
    mark = "probe-" + os.urandom(4).hex()
    child(f"open(r'{path}','a',encoding='utf-8').write('\\n# {mark}')")
    return "written" if mark in Path(path).read_text(encoding="utf-8") else "blocked"


def row(open_id, case, layer_guard, layer_os, note=""):
    rows.append({"open": open_id, "case": case, "guard": layer_guard, "os": layer_os, "note": note})


fwd = lambda p: str(p).replace("\\", "/")

# ---------------------------------------------------------------- OPEN-1: filesystem
row("OPEN-1", "file tool, own workspace (relative)", guard("read_file", {"path": "src/app.py"}), "read", "authorised path keeps working")
row("OPEN-1", "file tool, absolute path to another company", guard("read_file", {"path": str(OTHER)}), read_via_os(OTHER))
row("OPEN-1", "file tool, ../ traversal", guard("read_file", {"path": "../auvia/private/customer-notes.txt"}), read_via_os(OTHER))
row("OPEN-1", "file tool, federation DB", guard("read_file", {"path": str(MEMDB)}), read_via_os(MEMDB))
row("OPEN-1", "file tool, backups", guard("read_file", {"path": str(BACKUP)}), read_via_os(BACKUP))
row("OPEN-1", "file tool, write own config.yaml", guard("write_file", {"path": str(OWNCFG), "content": "x"}), "n/a (see OPEN-3)")
# junction inside the workspace pointing at another company (no admin right needed on Windows)
J = ISC / "vendor"
jr = subprocess.run(["cmd", "/c", "mklink", "/J", str(J), str(OTHER.parent)], capture_output=True, text=True)
facts["junction_created_without_admin"] = jr.returncode == 0
if jr.returncode == 0:
    row("OPEN-1", "file tool, junction escape (workspace/vendor -> auvia)", guard("read_file", {"path": "vendor/customer-notes.txt"}),
        read_via_os(J / "customer-notes.txt"), "realpath catches the junction for file tools; the OS follows it")
try:
    os.symlink(str(OTHER), str(ISC / "link.txt"))
    facts["symlink_created_without_admin"] = True
except OSError as e:
    facts["symlink_created_without_admin"] = f"no ({type(e).__name__}: needs Developer Mode or admin)"
# time-of-check / time-of-use: the guard resolves the path, then the tool runs later
B = ISC / "build"
B.mkdir()
decision = guard("read_file", {"path": "build/customer-notes.txt"})
B.rmdir()
subprocess.run(["cmd", "/c", "mklink", "/J", str(B), str(OTHER.parent)], capture_output=True)
row("OPEN-1", "TOCTOU: plain dir at check, junction at use", decision, read_via_os(B / "customer-notes.txt"),
    "a background process can swap the directory between the hook and the tool")
row("OPEN-1", "shared temp folder (same user)", "not covered", read_via_os(SHARED_TMP))

# ---------------------------------------------------------------- OPEN-2: commands, code, subprocesses
cmd = lambda text: guard("terminal", {"command": text})
row("OPEN-2", "terminal, literal path to another company", cmd(f"type {OTHER}"), read_via_os(OTHER))
row("OPEN-2", "terminal, literal path to federation DB", cmd(f"sqlite3 {MEMDB} .dump"), read_via_os(MEMDB))
row("OPEN-2", "terminal, run-time built path (string join)", cmd(
    "python -c \"import os;p=os.environ['LOCALAPPDATA']+'/her'+'mes/fede'+'ration/mem'+'ory.d'+'b';print(open(p).read())\""),
    read_via_os(MEMDB), "the guard only sees text")
row("OPEN-2", "terminal, wildcard (fed*/*.d?)", cmd(f"type {fwd(HOME)}/fed*/mem*.d?"), read_via_os(MEMDB))
row("OPEN-2", "terminal, PowerShell -EncodedCommand", cmd("powershell -NoProfile -EncodedCommand JABwAD0A..."), read_via_os(MEMDB))
row("OPEN-2", "execute_code, run-time built path", guard("execute_code", {"code": "p='../au'+'via/private/customer-notes.txt'\nprint(open(p).read())"}),
    read_via_os(OTHER))
row("OPEN-2", "grandchild process reads another company", "n/a (no hook for child processes)", read_via_os(OTHER, grandchild=True))
row("OPEN-2", "terminal, another company's Kanban board DB (literal)", cmd(f"sqlite3 {fwd(BOARD)} \"select * from tasks\""), read_via_os(BOARD))
row("OPEN-2", "terminal, Hermes auth.json (literal)", cmd(f"type {fwd(AUTH)}"), read_via_os(AUTH))
row("OPEN-2", "terminal, Hermes .env (literal)", cmd(f"type {fwd(ENVF)}"), read_via_os(ENVF))
row("OPEN-2", "terminal, owner.json (literal)", cmd(f"type {fwd(OWNER)}"), read_via_os(OWNER))
# environment: Hermes' own execute_code scrubber (names) vs. files on disk
try:
    sys.path.insert(0, str(Path(os.environ["LOCALAPPDATA"]) / "hermes" / "hermes-agent"))
    from tools.code_execution_env import _scrub_child_env  # pure function: no I/O
    env = _scrub_child_env({"GROQ_API_KEY": "x", "FED_MODEL_KEY_A": "x", "TELEGRAM_BOT_TOKEN": "x", "HERMES_HOME": str(HOME),
                            "PATH": "x", "LOCALAPPDATA": "x", "FED_CUSTOMER": "cus_1"}, is_passthrough=lambda n: False, is_windows=True)
    facts["execute_code_child_env_keeps"] = sorted(env)
except Exception as e:  # different Hermes version: record, do not fail
    facts["execute_code_child_env_keeps"] = f"not checked ({type(e).__name__})"

# ---------------------------------------------------------------- OPEN-3: cross-agent, control files, guard integrity
row("OPEN-3", "file tool, write own config.yaml (remove guard)", guard("write_file", {"path": str(OWNCFG), "content": "x"}), write_via_os(OWNCFG))
row("OPEN-3", "terminal, literal write to own config.yaml", cmd(f"echo x >> {fwd(OWNCFG)}"), write_via_os(OWNCFG))
row("OPEN-3", "terminal, built path to own config.yaml", cmd("python -c \"open('con'+'fig.yaml','a').write('x')\""), write_via_os(OWNCFG))
row("OPEN-3", "file tool, another agent's config", guard("write_file", {"path": str(OTHERCFG), "content": "x"}), write_via_os(OTHERCFG))
row("OPEN-3", "terminal, overwrite the guard script (literal name)", cmd(f"echo pass > {fwd(GUARD)}"), write_via_os(GUARD))
row("OPEN-3", "terminal, overwrite the guard script (wildcard name)", cmd(f"copy NUL {fwd(GUARD.parent)}/fed_g*.py"), write_via_os(GUARD),
    "one change disables the guard for all 118 agents")
row("OPEN-3", "terminal, patch Hermes source (hook dispatcher)", cmd(f"echo x >> {fwd(SRCF)}"), write_via_os(SRCF))
row("OPEN-3", "terminal, modify backups", cmd(f"echo x >> {fwd(BACKUP)}"), write_via_os(BACKUP))

# ---------------------------------------------------------------- C. read-only host facts
def run(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"unavailable: {type(e).__name__}"


groups = run(["whoami", "/groups"])
facts["user_is_local_admin_member"] = "S-1-5-32-544" in groups
facts["integrity_level"] = next((ln.split("\\", 1)[1].split("  ")[0] for ln in groups.splitlines()
                                 if "Mandatory Label\\" in ln), "unknown")
user = os.environ.get("USERNAME", "")
real = {"hermes_home": Path(os.environ["LOCALAPPDATA"]) / "hermes", "workspaces": Path.home() / "hermes-federation" / "workspaces",
        "guard_source": Path.home() / "OneDrive" / "Documents" / "Hermes" / "security",
        "backups": Path.home() / "hermes-backups", "console_settings_dir": Path.home() / "hermes-federation"}
facts["acl_user_full_control"] = {}
for k, p in real.items():
    acl = run(["icacls", str(p)])
    facts["acl_user_full_control"][k] = any(user.lower() in ln.lower() and "(F)" in ln for ln in acl.splitlines())
connect = lambda port: child(f"import socket;s=socket.socket();s.settimeout(2);"
                             f"print('open' if s.connect_ex(('127.0.0.1',{port}))==0 else 'closed');s.close()", grandchild=True)
lst = socket.socket()
lst.bind(("127.0.0.1", 0))
lst.listen(1)  # a throwaway local service standing in for the Console / Ollama
facts["loopback_service_reachable_from_grandchild"] = connect(lst.getsockname()[1])
lst.close()
facts["real_ports_now (console 8765, ollama 11434)"] = {name: connect(port) for name, port in (("console", 8765), ("ollama", 11434))}
facts["docker"] = run(["docker", "version", "--format", "{{.Client.Version}}"]).strip() or "client only / engine stopped"
facts["wsl"] = " ".join(run(["wsl", "-l", "-v"]).replace("\x00", "").split())
wt = next((p for p in (Path.home() / "hermes-federation" / "workspaces").glob("*/.worktrees/*/.git") if p.is_file()), None)
facts["worktree_git_pointer"] = wt.read_text(encoding="utf-8").strip()[:200] if wt else "no worktree found"

shutil.rmtree(T, ignore_errors=True)
facts["temp_removed"] = not T.exists()

# ---------------------------------------------------------------- report
w = max(len(r["case"]) for r in rows)
print(f"{'OPEN':7} {'case':{w}}  guard     OS")
for r in rows:
    print(f"{r['open']:7} {r['case']:{w}}  {r['guard']:9} {r['os']}" + (f"   ({r['note']})" if r["note"] else ""))
print("\nhost facts:")
for k, v in facts.items():
    print(f"  {k}: {v}")
gaps = [r for r in rows if r["guard"] == "allowed" and r["os"] in ("read", "written") and "authorised" not in r["note"]]
print(f"\n{len(rows)} cases; {len(gaps)} reach a canary with the guard allowing the call; "
      f"{sum(1 for r in rows if r['guard'] == 'denied' and r['os'] in ('read', 'written'))} are refused by the guard "
      f"but allowed by the OS (application-level only)")
if "--json" in sys.argv:
    Path(sys.argv[sys.argv.index("--json") + 1]).write_text(json.dumps({"rows": rows, "facts": facts}, indent=1), encoding="utf-8")
