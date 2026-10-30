"""Federation Console: a local test UI over the Hermes CLI.

Binds to 127.0.0.1 only (R05.05). Every action is a fixed `hermes` command with validated
arguments; no shell is used. Run:  python server.py   then open http://127.0.0.1:8765
The owner signs in (owner_auth.py); the customer portal (/portal) has its own customer sessions.
"""
import json
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
import subprocess
import sys
import urllib.request
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

def load_env_file(path):
    """Load KEY=VALUE lines from a .env file next to this script. Real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


# Settings file outside OneDrive first (holds the API key), then a .env next to this script.
load_env_file(Path.home() / "hermes-federation" / ".env")
load_env_file(Path(__file__).parent / ".env")

HOST = os.environ.get("CONSOLE_HOST", "127.0.0.1")
PORT = int(os.environ.get("CONSOLE_PORT", "8765"))
HERE = Path(__file__).parent
# Hermes home: HERMES_HOME if set, else the platform default (Windows %LOCALAPPDATA%\hermes, Linux/macOS ~/.hermes).
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (
    Path(os.environ["LOCALAPPDATA"]) / "hermes" if os.name == "nt" else Path.home() / ".hermes"))
PROFILES = HERMES_HOME / "profiles"
OURS = re.compile(r"^(fed|isc|auvia|tabbeqai|spectra|spectrasolutions)-[a-z-]+$")
BOARDS = {"federation": "Hermes Federation", "isc": "ISC", "auvia": "AUVIA", "tabbeqai": "TabbeQ AI",
          "spectra": "Spectra", "spectrasolutions": "Spectra Solutions"}
TASK_ID = re.compile(r"^t_[0-9a-f]{6,16}$")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1"}

# Federation memory (../memory). The console acts as the owner; "view as" shows what one agent may read.
sys.path.insert(0, str(HERE.parent / "memory"))
from fedmem import OWNER, Denied, Memory, load_org  # noqa: E402
sys.path.insert(0, str(HERE.parent / "workflow"))
from tasks import STATES, Conflict, TaskError, Workflow  # noqa: E402
sys.path.insert(0, str(HERE))
from owner_auth import COOKIE, AuthError, OwnerAuth  # noqa: E402
from sessions import Locked, SessionStoreError  # noqa: E402
from conversations import Busy, ConversationError, Conversations  # noqa: E402
import gateway  # noqa: E402  (workflow/gateway.py)
import models  # noqa: E402  (provider-independent model management, Phase 5.2)
import agent_runner  # noqa: E402  (G2-2 pilot: one synthetic agent run as the separate Windows account)
import routing  # noqa: E402  (per-company identities: where each group lives and as whom it runs)
import viz  # noqa: E402  (federation map, Phase 5.1)
sys.path.insert(0, str(HERE.parent / "channels"))
import telegram  # noqa: E402  (Telegram adapter, Phase 5.3: prepared, not connected)

# Per-company layout (deploy/migration): migrated groups live in C:\HermesFederation\homes\<group> and run as their
# own Windows account. FED_LAYOUT_ROOT points elsewhere (isolated test consoles: an empty folder = nothing migrated).
LAYOUT_ROOT = Path(os.environ.get("FED_LAYOUT_ROOT") or agent_runner.ROOT)
# Once migrate.py --shared ran, the memory and workflow databases live in C:\HermesFederation\shared (explicit
# FED_MEMORY_DB / FED_WORKFLOW_DB still win). Set before anything opens them.
_SHARED = routing.Routing(HERMES_HOME, HERMES_HOME, "hermes", {}, root=LAYOUT_ROOT).shared() or {}
for _key, _var in (("memory", "FED_MEMORY_DB"), ("workflow", "FED_WORKFLOW_DB")):
    if _SHARED.get(_key) and not os.environ.get(_var):
        os.environ[_var] = ENV[_var] = _SHARED[_key]
AUTH = OwnerAuth(os.environ.get("FED_OWNER_FILE") or HERMES_HOME / "federation" / "owner.json")
WF, WF_LOCK = Workflow(home=HERMES_HOME), threading.Lock()
CONV = Conversations(os.environ.get("FED_CONVERSATIONS_DB") or HERMES_HOME / "federation" / "conversations.db")
PORTAL_COOKIE = "fed_portal"
# Saved model configurations (no secrets) and their keys in a separate file in the federation folder; the
# console settings file and its GROQ_API_KEY are never written by model management.
MODELS = models.Registry(os.environ.get("FED_MODELS_FILE") or HERMES_HOME / "federation" / "models.json",
                         os.environ.get("FED_MODEL_KEYS_FILE") or HERMES_HOME / "federation" / "model-keys.env")
PUBLIC = {"/login", "/login.html", "/portal", "/portal.html", "/api/owner/state", "/api/owner/setup",
          "/api/owner/login", "/api/portal/me", "/api/portal/login", "/api/portal/logout", "/api/portal/chat",
          "/api/portal/conversation"}
# Owner endpoints that read or change federation state outside the owner's own session: each is audited
# before it runs, and refused (503) if the audit trail cannot be written.
AUDITED = re.compile(r"^/api/(ftasks|tasks|memory/(write|override|customers|lessons)|boards/[a-z]+/dispatch|admin/|models/|pilot/)")
SYNC_STATE = {"last_ok": None, "last_error": None, "errors": 0}


def sync_loop():
    """Turn what Kanban workers did into task states, every 15 seconds (durable: state lives in workflow.db)."""
    while True:
        try:
            with WF_LOCK:
                WF.sync()
            SYNC_STATE.update(last_ok=time.time(), last_error=None)
        except Exception as e:  # keep syncing; the next round retries
            SYNC_STATE.update(last_error=f"{type(e).__name__} at {time.strftime('%H:%M:%S')}", errors=SYNC_STATE["errors"] + 1)
            print("sync error:", type(e).__name__, flush=True)
        time.sleep(15)


class AuditUnavailable(Exception):
    pass


def admin_audit(action, target, result, detail=""):
    """Write one audit row as the owner; raise AuditUnavailable if it cannot be written."""
    try:
        Memory(OWNER)._audit(action, target, result, detail[:300])
    except Exception:
        raise AuditUnavailable("the audit trail is unavailable; the action was not performed") from None


ID_IN_PATH = re.compile(r"(tsk_[0-9a-f]{8}|cus_[0-9a-f]{8}|wo_[0-9a-f]{8}|t_[0-9a-f]{6,16})")


def audit_target(path, data):
    """What an admin action touches, for the audit row: an id from the path or body, never message content."""
    m = ID_IN_PATH.search(path)
    if m:
        return m.group(1)
    for key in ("id", "customer", "board", "profile", "scope"):
        v = data.get(key) if isinstance(data, dict) else None
        if isinstance(v, str) and v and len(v) <= 64:
            return f"{key}:{v}"
    return ""


_TG = []


def telegram_bridge():
    """The Telegram adapter, created on first use. Phase 5: link codes only; the transport is Disconnected and
    no bot token is read, so nothing reaches Telegram."""
    if not _TG:
        def lookup(cid):
            try:
                return Memory(OWNER).customer("get", cid)
            except Denied:
                return None

        def run(agent, prompt, cid):
            rc, out, _ = hermes("-p", agent, "-z", prompt, timeout=900, env={**ENV, "FED_CUSTOMER": cid})
            return rc == 0, out.strip()
        _TG.append(telegram.TelegramBridge(
            os.environ.get("FED_CHANNELS_DB") or HERMES_HOME / "federation" / "channels.db", lookup, CONV, run,
            telegram.Disconnected(), lambda *a: Memory(OWNER)._audit(*a), BOARDS))
    return _TG[0]


def customer_version(cid):
    try:
        return Memory(OWNER).customer_session_version(cid)
    except Exception:
        return None


def memory_as(profile):
    """Owner's memory handle, or a read-only look through one agent's permissions (audited as 'owner as X')."""
    if not profile or profile == OWNER:
        return Memory(OWNER)
    return Memory(profile, viewer=OWNER)


def portal_token(handler):
    m = re.search(r"(?:^|;\s*)" + PORTAL_COOKIE + r"=([A-Za-z0-9_-]+)", handler.headers.get("Cookie", ""))
    return m.group(1) if m else None


def portal_customer(handler):
    """Customer of a live portal session, re-checked on every request: an inactive customer or a newer login
    code ends the session. Test portal only; real customers arrive through the channel gateway."""
    return AUTH.store.validate(portal_token(handler), "customer", customer_version)


def health():
    """Read-only federation health for the owner: model gateway, stores, boards, workers, guard coverage."""
    out = {"checked_at": time.time()}
    probes = {}
    for profile in ("isc-junior-developer", "fed-hermes-admin"):
        st, detail = gateway.probe_profile(ROUTE.home(profile), profile, timeout=4)
        probes[profile] = {"status": st, "class": ("ok" if st == "ok" else "temporary" if st in gateway.TRANSIENT
                                                   else "permanent"), "detail": detail[:160]}
    out["model_gateway"] = probes
    stores = {"sessions": AUTH.store.check(), "conversations": CONV.check()}
    for name, path in (("memory", Memory(OWNER).db), ("workflow", WF.db)):
        try:
            stores[name] = path.execute("PRAGMA quick_check").fetchone()[0]
        except sqlite3.Error as e:
            stores[name] = f"error: {type(e).__name__}"
    for b in BOARDS:
        try:
            with closing(board_db(b)) as c:
                stores[f"kanban:{b}"] = c.execute("PRAGMA quick_check").fetchone()[0]
        except sqlite3.Error as e:
            stores[f"kanban:{b}"] = f"error: {type(e).__name__}"
    out["stores"] = stores
    with WF_LOCK:
        tasks = WF.list()
    out["tasks"] = {
        "by_state": {s: sum(1 for t in tasks if t["state"] == s) for s in STATES if any(t["state"] == s for t in tasks)},
        "running_without_heartbeat": [t["id"] for t in tasks if t["stale"]],
        "blocked_temporary": [t["id"] for t in tasks if t["state"] == "BLOCKED_EXTERNAL" and t["block_cause"] == "gateway"],
        "blocked_permanent": [t["id"] for t in tasks if t["block_cause"] in ("gateway-permanent", "gateway-exhausted")
                              or t["state"] == "ESCALATING"],
        "failed": [t["id"] for t in tasks if t["state"] == "FAILED"]}
    out["sync"] = dict(SYNC_STATE)
    profiles = ROUTE.profile_dirs()
    texts = [(d / "config.yaml").read_text(encoding="utf-8") for d in profiles if (d / "config.yaml").exists()]
    out["agents"] = {"profiles": len(profiles), "guard_installed": sum("fed_guard.py" in t for t in texts),
                     "memory_tool_installed": sum("federation-memory:" in t for t in texts)}
    try:
        day = [a for a in Memory(OWNER).audit_log(2000) if a["ts"] > time.time() - 86400]
        out["denials_24h"] = sum(1 for a in day if a["result"] == "denied")
    except Exception:
        out["denials_24h"] = None
    return out


# FED_HERMES_CMD replaces the hermes executable (isolated test instances use a stub); same rule as workflow.
HERMES_CMD = WF.hermes_cmd if isinstance(WF.hermes_cmd, list) else [WF.hermes_cmd]
ROUTE = routing.Routing(HERMES_HOME, WF.workspaces, HERMES_CMD, ENV, root=LAYOUT_ROOT)
WF.route = ROUTE


def hermes(*args, timeout=120, env=None):
    """Every Hermes command of the Console: agents of a migrated group run as that group's Windows account."""
    return ROUTE.run(*args, timeout=timeout, env=env)


def hermes_json(*args, timeout=120):
    rc, out, err = hermes(*args, "--json", timeout=timeout)
    start = min([i for i in (out.find("{"), out.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise RuntimeError((err or out).strip()[-400:] or f"hermes exited {rc}")
    return json.loads(out[start:])


SYNCED_KEYS = ("GROQ_API_KEY", "DEEPSEEK_API_KEY")


def federation_env_files():
    files = [HERMES_HOME / ".env"]
    files += [d / ".env" for d in ROUTE.profile_dirs()]
    return files


def sync_api_keys():
    """Copy API keys from the console settings into the Hermes .env of the default home and every agent.

    Hermes reads provider keys per profile (key_env), so one key has to reach all 118 profiles.
    Only the key the configured AGENT_PROVIDER needs is copied (local models need none): every copy is a
    file an agent's terminal can read, so unused keys are never spread. The value is never printed.
    """
    need = PROVIDER_KEY.get(os.environ.get("AGENT_PROVIDER", "").strip())
    keys = {k: os.environ[k] for k in SYNCED_KEYS if k == need and os.environ.get(k)}
    if not keys:
        print("API keys: none copied to agents (the configured model needs no API key)")
        return 0
    changed = 0
    for f in federation_env_files():
        lines = f.read_text(encoding="utf-8").splitlines() if f.exists() else []
        new = [ln for ln in lines if ln.split("=", 1)[0].strip() not in keys]
        new += [f"{k}={v}" for k, v in keys.items()]
        if new != lines:
            f.write_text("\n".join(new) + "\n", encoding="utf-8")
            changed += 1
    print(f"API keys {', '.join(keys)} synced to {changed} of {len(federation_env_files())} Hermes .env files")
    return changed


PROVIDER_KEY = {"custom:groq": "GROQ_API_KEY", "deepseek": "DEEPSEEK_API_KEY"}


def apply_agent_model():
    """Set every agent's model from AGENT_PROVIDER / AGENT_MODEL (same edit as roster/set_local_model.py)."""
    provider, model = os.environ.get("AGENT_PROVIDER", "").strip(), os.environ.get("AGENT_MODEL", "").strip()
    if not (provider and model):
        return
    need = PROVIDER_KEY.get(provider)
    if need and not os.environ.get(need):
        print(f"Not switching agents to {provider}: {need} is empty in the settings file")
        return
    done = 0
    for d in ROUTE.profile_dirs():
        cfg = d / "config.yaml"
        if not (d.is_dir() and OURS.match(d.name) and cfg.exists()):
            continue
        text = cfg.read_text(encoding="utf-8")
        m = re.search(r"^model:\n((?:[ \t].*\n|\n)*)", text, re.M)
        if not m or re.search(r'^\s+provider:\s*"?fed-', m.group(1), re.M):
            continue  # no model block, or the owner assigned this agent a model in the Models tab: keep it
        block = re.sub(r'^(\s+default:\s*).*$', rf'\g<1>"{model}"', m.group(1), count=1, flags=re.M)
        block = re.sub(r'^(\s+provider:\s*).*$', rf'\g<1>"{provider}"', block, count=1, flags=re.M)
        block = re.sub(r"^\s+ollama_num_ctx:.*\n", "", block, flags=re.M)  # Ollama-only; hosted APIs reject it
        if provider.startswith("local"):
            block = re.sub(r'^(\s+)(provider:.*\n)', r'\g<1>\g<2>\g<1>ollama_num_ctx: 65536\n', block, count=1, flags=re.M)
        if block != m.group(1):
            cfg.write_text(text[:m.start(1)] + block + text[m.end(1):], encoding="utf-8")
            done += 1
    print(f"Agents on {provider}/{model} ({done} updated)")


def board_db(board):
    """Read-only connection to a board's kanban.db. Reads skip the slow CLI; writes still use it."""
    path = ROUTE.home(board) / "kanban" / "boards" / board / "kanban.db"
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def list_tasks(board):
    with closing(board_db(board)) as c:
        return [dict(r) for r in c.execute(
            "SELECT id, title, assignee, status, created_at, started_at, completed_at FROM tasks "
            "WHERE status != 'archived' ORDER BY created_at DESC")]


def show_task(board, task_id):
    with closing(board_db(board)) as c:
        row = c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise LookupError(f"{task_id} not found on {board}")
        runs = [dict(r) for r in c.execute(
            "SELECT id, profile, status, outcome, summary, error, started_at, ended_at FROM task_runs "
            "WHERE task_id = ? ORDER BY id", (task_id,))]
        comments = [dict(r) for r in c.execute(
            "SELECT author, body, created_at FROM task_comments WHERE task_id = ? ORDER BY id", (task_id,))]
        parents = [r[0] for r in c.execute("SELECT parent_id FROM task_links WHERE child_id = ?", (task_id,))]
    return {"task": dict(row), "runs": runs, "comments": comments, "parents": parents}


def yaml_value(text, key):
    m = re.search(rf"^\s*{re.escape(key)}:\s*([^\n#]+)", text, re.M)
    return m.group(1).strip().strip("'\"") if m else ""


def agents():
    rows = []
    for d in ROUTE.profile_dirs():
        if not (d.is_dir() and OURS.match(d.name)):
            continue
        meta = (d / "profile.yaml").read_text(encoding="utf-8") if (d / "profile.yaml").exists() else ""
        cfg = (d / "config.yaml").read_text(encoding="utf-8") if (d / "config.yaml").exists() else ""
        ui = re.search(r"^ui_meta:\s*(\{.*\})\s*$", meta, re.M)
        bots = json.loads(ui.group(1)).get("hermes-bots", {}) if ui else {}
        model_block = cfg.split("\nmodel:", 1)[1][:2500] if "\nmodel:" in cfg else ""
        rows.append({
            "id": d.name,
            "title": bots.get("title") or d.name,
            "section": bots.get("sectionName") or "Unassigned",
            "board": "federation" if d.name.startswith("fed-") else d.name.split("-", 1)[0],
            "description": yaml_value(meta, "description"),
            "model": yaml_value(model_block, "default"),
            "provider": yaml_value(model_block, "provider"),
        })
    return rows


OLLAMA_PS = (os.environ.get("FED_OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/") + "/api/ps"
OLLAMA_WAIT = float(os.environ.get("FED_OLLAMA_STATUS_TIMEOUT") or 15)  # Ollama answers slowly while loading a model
STATUS_TTL = 5


class OllamaStatus:
    """Ollama's state, checked in the background. The check waits for Ollama's real answer (no short cut-off that
    reports a busy Ollama as down); /api/status returns the latest answer at once, so the browser never waits on
    Ollama and never drops a request half-way. Only the first call after start-up waits for the first answer."""

    def __init__(self):
        self.value, self.at, self.running = None, 0.0, False
        self.lock, self.first = threading.Lock(), threading.Event()
        self.loading = {}  # model -> {"since": ts} while a load runs; {"error"/"seconds": ...} once done

    def _check(self):
        out = {"ollama": False, "loaded": [], "installed": []}
        try:
            with urllib.request.urlopen(OLLAMA_PS, timeout=OLLAMA_WAIT) as r:
                out = {"ollama": True, "loaded": [m["name"] for m in json.load(r).get("models", [])], "installed": []}
            with urllib.request.urlopen(OLLAMA_PS[:-len("/api/ps")] + "/api/tags", timeout=OLLAMA_WAIT) as r:
                out["installed"] = sorted(m["name"] for m in json.load(r).get("models", []))
        except Exception:
            pass
        with self.lock:
            self.value, self.at, self.running = out, time.time(), False
        self.first.set()

    def load(self, model):
        """Load `model` in the background (owner action); the status shows it as loading until Ollama is ready."""
        with self.lock:
            if "since" in self.loading.get(model, {}):
                return False
            self.loading[model] = {"since": time.time()}

        def run():
            try:
                ctx = agent_num_ctx(model)
                done = {"seconds": models.load_ollama_model(model, OLLAMA_PS[:-len("/api/ps")], num_ctx=ctx),
                        "num_ctx": ctx}
            except models.ModelError as e:
                done = {"error": str(e)}
            with self.lock:
                self.loading[model], self.at = done, 0.0  # force a fresh check: the model should now be listed
            admin_audit("admin/models/ollama-load", model, "ok" if "seconds" in done else "failed",
                        f"{done.get('seconds', '')}s" if "seconds" in done else done["error"][:200])
        threading.Thread(target=run, daemon=True).start()
        return True

    def get(self):
        with self.lock:
            start = not self.running and time.time() - self.at > STATUS_TTL
            self.running = self.running or start
        if start:
            threading.Thread(target=self._check, daemon=True).start()
        if self.value is None:
            self.first.wait(OLLAMA_WAIT + 1)
        with self.lock:
            out = dict(self.value or {"ollama": False, "loaded": [], "installed": []})
            out["checked_s_ago"] = round(time.time() - self.at, 1) if self.at else None
            now = time.time()
            out["loading"] = {m: ({"seconds_so_far": round(now - s["since"])} if "since" in s else s)
                              for m, s in self.loading.items()}
        return out


OLLAMA = OllamaStatus()
# FED_PILOT_ROOT points the pilot elsewhere (isolated test consoles point it at an empty folder: pilot unavailable).
PILOT = agent_runner.Pilot(os.environ.get("FED_PILOT_ROOT") or agent_runner.ROOT)


def agent_num_ctx(model):
    """The num_ctx the federation agents send for `model` (model.ollama_num_ctx of the profiles using it, most common
    value), so a pre-load matches what Ollama will be asked for. None when no profile uses the model."""
    counts = {}
    for p in ROUTE.profile_dirs():
        cfg = p / "config.yaml"
        if not cfg.is_file():
            continue
        block = re.search(r"^model:\n((?:[ \t]+.*\n|\n)*)", cfg.read_text(encoding="utf-8", errors="replace"), re.M)
        if not block:
            continue
        default = re.search(r"^\s+default:\s*\"?([^\"\n#]+?)\"?\s*(?:#.*)?$", block.group(1), re.M)
        ctx = re.search(r"^\s+ollama_num_ctx:\s*(\d+)", block.group(1), re.M)
        if default and ctx and default.group(1).strip() == model:
            counts[int(ctx.group(1))] = counts.get(int(ctx.group(1)), 0) + 1
    return max(counts, key=counts.get) if counts else None


def status():
    return OLLAMA.get()


# The browser closed the connection before the answer was written (page reload, navigation to /login, closed tab).
CLIENT_GONE = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, code, payload, ctype="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except CLIENT_GONE:
            self.close_connection = True  # nobody is listening any more: nothing to report, nothing to retry

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def guard(self):
        # Reject cross-site requests: only same-origin pages may call the API.
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        host = self.headers.get("Host", "")
        return urlparse(origin).netloc in (host, f"{HOST}:{PORT}", f"localhost:{PORT}", f"127.0.0.1:{PORT}")

    def owner_token(self):
        m = re.search(rf"(?:^|;\s*){COOKIE}=([A-Za-z0-9_-]+)", self.headers.get("Cookie", ""))
        return m.group(1) if m else None

    def authorized(self):
        """Everything except the sign-in page and the customer portal needs a live owner session, checked
        against the session store on every request (revocation and password changes apply immediately)."""
        return urlparse(self.path).path in PUBLIC or AUTH.valid(self.owner_token())

    def fail(self, e):
        """Unexpected error: a reference id for the operator, no internal detail for the client."""
        if isinstance(e, CLIENT_GONE):  # the client left (e.g. while its request body was read): not a server error
            self.close_connection = True
            return None
        ref = secrets.token_hex(4)
        print(f"error {ref}: {type(e).__name__} on {urlparse(self.path).path}", flush=True)
        return self.send(500, {"error": "internal error", "ref": ref})

    def deny(self):
        if urlparse(self.path).path.startswith("/api/"):
            return self.send(401, {"error": "sign in as the owner"})
        self.send_response(302)
        self.send_header("Location", "/login")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def set_cookie(self, name, token, ttl):
        self.send_header("Set-Cookie", f"{name}={token or ''}; HttpOnly; SameSite=Strict; Path=/; "
                                       f"Max-Age={int(ttl) if token else 0}")

    def send_session(self, token, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.set_cookie(COOKIE, token, AUTH.store.policy("owner")[0])
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            if not self.authorized():
                return self.deny()
        except SessionStoreError:
            return self.send(503, {"error": "session store unavailable; try again shortly"})
        url = urlparse(self.path)
        path, query = url.path, {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            if path in ("/login", "/login.html"):
                return self.send(200, (HERE / "login.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/owner/state":
                return self.send(200, {"exists": AUTH.exists(), "signed_in": AUTH.valid(self.owner_token())})
            if path == "/api/ftasks":
                with WF_LOCK:
                    return self.send(200, {"states": STATES, "tasks": WF.list(query.get("company") or None)})
            m = re.fullmatch(r"/api/ftasks/(tsk_[0-9a-f]{8})(/usage)?", path)
            if m:
                with WF_LOCK:
                    if m.group(2):  # usage and cost: owner only, and every look is audited
                        Memory(OWNER)._audit("usage.view", m.group(1), "ok")
                        return self.send(200, WF.usage(m.group(1)))
                    return self.send(200, WF.get(m.group(1)))
            if path == "/api/memory":
                mem = memory_as(query.get("as"))
                return self.send(200, {"whoami": mem.whoami(),
                                       "records": mem.search(query.get("q", ""), query.get("scope") or None,
                                                             query.get("kind") or None, 50)})
            m = re.fullmatch(r"/api/memory/records/(\d+)", path)
            if m:
                return self.send(200, memory_as(query.get("as")).get(int(m.group(1))))
            if path == "/api/memory/customers":
                return self.send(200, Memory(OWNER).customer("list"))
            if path == "/api/workorders":
                return self.send(200, Memory(OWNER).work_order("list"))
            m = re.fullmatch(r"/api/workorders/(wo_[0-9a-f]{8})", path)
            if m:
                return self.send(200, Memory(OWNER).work_order("get", m.group(1)))
            if path in ("/portal", "/portal.html"):
                return self.send(200, (HERE / "portal.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/conversations":
                agent = query.get("agent") or ""
                cid = query.get("id") or CONV.latest("owner", "owner", agent)
                return self.send(200, CONV.transcript("owner", "owner", cid) if cid else {"id": None, "messages": []})
            if path == "/api/portal/conversation":
                cus = portal_customer(self)
                if not cus:
                    return self.send(401, {"error": "not logged in"})
                c = Memory(OWNER).customer("get", cus)
                cid = CONV.latest("customer", cus, f"{c['company']}-receptionist")
                return self.send(200, CONV.transcript("customer", cus, cid) if cid else {"id": None, "messages": []})
            if path == "/api/admin/health":
                return self.send(200, health())
            if path == "/api/admin/sessions":
                return self.send(200, AUTH.store.list(self.owner_token()))
            if path == "/api/portal/me":
                cid = portal_customer(self)
                if not cid:
                    return self.send(401, {"error": "not logged in"})
                c = Memory(OWNER).customer("get", cid)
                return self.send(200, {k: c[k] for k in ("id", "name", "company", "language", "channel", "projects")}
                                 | {"company_name": BOARDS.get(c["company"], c["company"])})
            if path == "/api/memory/audit":
                return self.send(200, Memory(OWNER).audit_log(200))
            if path == "/api/memory/lessons":
                return self.send(200, Memory(OWNER).lesson("list"))
            if path in ("/", "/index.html"):
                return self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/agents":
                return self.send(200, agents())
            if path == "/api/viz":
                try:
                    with WF_LOCK:
                        ftasks = WF.list()
                    return self.send(200, viz.federation_map(ROUTE.home, HERE.parent, agents(), ftasks, load_org()))
                except Exception as e:  # the map is optional: report it, the rest of the console keeps working
                    print(f"map unavailable: {type(e).__name__}", flush=True)
                    return self.send(503, {"error": "the federation map is unavailable right now"})
            if path == "/api/models":
                return self.send(200, {"providers": {k: {"label": v["label"], "base": v["base"], "needs_key": v["key"]}
                                                     for k, v in models.PROVIDERS.items()},
                                       "purposes": list(models.PURPOSES), "companies": list(BOARDS),
                                       "configs": [MODELS.public(n) for n in sorted(MODELS.list())]})
            if path == "/api/boards":
                return self.send(200, BOARDS)
            if path == "/api/status":
                return self.send(200, status())
            if path == "/api/pilot":
                return self.send(200, PILOT.status())
            m = re.fullmatch(r"/api/boards/([a-z]+)/tasks", path)
            if m and m.group(1) in BOARDS:
                return self.send(200, list_tasks(m.group(1)))
            m = re.fullmatch(r"/api/boards/([a-z]+)/tasks/(t_[0-9a-f]+)(/log)?", path)
            if m and m.group(1) in BOARDS and TASK_ID.match(m.group(2)):
                if m.group(3):
                    _, out, err = hermes("kanban", "--board", m.group(1), "log", m.group(2))
                    lines = (out or err).splitlines()
                    return self.send(200, {"log": "\n".join(lines[-400:])})
                return self.send(200, show_task(m.group(1), m.group(2)))
            return self.send(404, {"error": "not found"})
        except Denied as e:
            return self.send(403, {"error": str(e)})
        except TaskError as e:
            return self.send(400, {"error": str(e)})
        except ConversationError as e:
            return self.send(404, {"error": str(e)})
        except SessionStoreError:
            return self.send(503, {"error": "session store unavailable; try again shortly"})
        except Exception as e:
            return self.fail(e)

    def do_POST(self):
        try:
            if not self.authorized():
                return self.deny()
        except SessionStoreError:
            return self.send(503, {"error": "session store unavailable; try again shortly"})
        if not self.guard():
            return self.send(403, {"error": "cross-origin request refused"})
        path = urlparse(self.path).path
        audited = bool(AUDITED.match(path))
        try:
            data = self.body()
        except ValueError:
            return self.send(400, {"error": "the request body is not valid JSON"})
        target = audit_target(path, data)
        reason = (data.get("reason") or "")[:200] if isinstance(data, dict) and isinstance(data.get("reason"), str) else ""
        try:
            if audited:
                admin_audit(f"admin{path[4:]}", target, "attempt", reason)
            result = self.post(path, data)
            if audited:
                admin_audit(f"admin{path[4:]}", target, "ok", reason)
            return result
        except AuditUnavailable as e:
            return self.send(503, {"error": str(e)})
        except Exception as e:
            if audited:
                try:
                    admin_audit(f"admin{path[4:]}", target, "denied" if isinstance(e, (Denied, TaskError, AuthError))
                                else "error", f"{type(e).__name__}: {e}"[:200])
                except AuditUnavailable:
                    pass
            if isinstance(e, Locked):
                return self.send(429, {"error": str(e)})
            if isinstance(e, Denied):
                return self.send(403, {"error": str(e)})
            if isinstance(e, AuthError):
                return self.send(401, {"error": str(e)})
            if isinstance(e, Conflict):
                return self.send(409, {"error": str(e)})
            if isinstance(e, TaskError):
                return self.send(400, {"error": str(e)})
            if isinstance(e, Busy):
                return self.send(409, {"error": str(e)})
            if isinstance(e, ConversationError):
                return self.send(404, {"error": str(e)})
            if isinstance(e, SessionStoreError):
                return self.send(503, {"error": "session store unavailable; try again shortly"})
            if isinstance(e, subprocess.TimeoutExpired):
                return self.send(504, {"error": "Hermes took too long to answer"})
            return self.fail(e)

    def post(self, path, data):
        if True:
            if path == "/api/owner/setup":
                return self.send_session(AUTH.setup(data.get("code"), data.get("password") or ""), {"ok": True})
            if path == "/api/owner/login":
                return self.send_session(AUTH.login(data.get("password") or ""), {"ok": True})
            if path == "/api/owner/logout":
                AUTH.logout(self.owner_token())
                return self.send_session(None, {"ok": True})
            if path == "/api/admin/sessions/revoke":
                if data.get("scope") == "others":
                    n = AUTH.store.revoke_all("owner", reason="owner signed out other sessions", keep=self.owner_token())
                elif data.get("scope") == "customer" and re.fullmatch(r"cus_[0-9a-f]{8}", data.get("customer") or ""):
                    n = AUTH.store.revoke_all("customer", data["customer"], reason="revoked by owner")
                else:
                    n = AUTH.store.revoke_id(data.get("id") or "")
                return self.send(200, {"revoked": n})
            if path == "/api/admin/integrity":
                return self.send(200, health()["stores"])
            m = re.fullmatch(r"/api/admin/customers/(cus_[0-9a-f]{8})/status", path)
            if m:
                c = Memory(OWNER).customer_set_status(m.group(1), data.get("status") or "")
                if c["status"] != "active":
                    AUTH.store.revoke_all("customer", m.group(1), reason="customer deactivated")
                return self.send(200, c)
            m = re.fullmatch(r"/api/admin/customers/(cus_[0-9a-f]{8})/telegram-code", path)
            if m:
                try:
                    return self.send(200, telegram_bridge().new_link_code(m.group(1)))
                except telegram.TelegramError as e:
                    return self.send(400, {"error": str(e)})
            if path == "/api/owner/password":
                return self.send_session(AUTH.change_password(data.get("current") or "", data.get("new") or ""),
                                         {"ok": True})
            if path == "/api/ftasks":
                with WF_LOCK:
                    return self.send(200, WF.create(
                        "owner", data.get("company") or "isc", data.get("title") or "", data.get("owner") or "",
                        data.get("task_class") or "", data.get("input_contract") or "", data.get("output_contract") or "",
                        data.get("acceptance_criteria") or "", project=data.get("project") or None,
                        customer=data.get("customer") or None, dependencies=data.get("dependencies") or [],
                        data_restrictions=data.get("data_restrictions") or "",
                        allowed_tools=data.get("allowed_tools") or "", idempotency_key=data.get("idempotency_key"),
                        test_command=data.get("test_command") or ""))
            if path == "/api/ftasks/sync":
                with WF_LOCK:
                    return self.send(200, {"changed": WF.sync()})
            m = re.fullmatch(r"/api/ftasks/(tsk_[0-9a-f]{8})/(transition|revise|evidence)", path)
            if m:
                tid, op = m.groups()
                with WF_LOCK:
                    if op == "transition":
                        return self.send(200, WF.transition(tid, data.get("to") or "", "owner",
                                                            int(data.get("expected_version") or 0),
                                                            data.get("reason") or "", data.get("idempotency_key")))
                    if op == "revise":
                        fields = {k: data.get(k) for k in ("input_contract", "output_contract", "acceptance_criteria",
                                                           "data_restrictions", "allowed_tools", "test_command")}
                        return self.send(200, WF.revise(tid, "owner", int(data.get("expected_version") or 0), **fields))
                    return self.send(200, WF.add_evidence(tid, "owner", data.get("kind") or "note", data.get("ref") or ""))
            if path == "/api/memory/write":  # the owner writes (owner goals, spending policy, any scope)
                return self.send(200, Memory(OWNER).write(
                    data.get("scope", ""), data.get("kind", ""), data.get("title", ""), data.get("body", ""),
                    data.get("status", "claim"), data.get("confidentiality", "internal"), data.get("source", "console")))
            if path == "/api/memory/customers":
                return self.send(200, Memory(OWNER).customer(
                    "register", data.get("company"), data.get("name"), data.get("channel"), data.get("handle"),
                    data.get("language"), data.get("details")))
            if path == "/api/memory/customers/login-code":
                return self.send(200, Memory(OWNER).customer_login_code(data.get("id", "")))
            if path == "/api/portal/login":
                key = f"portal:{self.client_address[0]}"  # guessing codes is throttled per client address
                AUTH.store.throttle_check(key)
                c = Memory(OWNER).customer_by_code(data.get("code", ""))
                if not c:
                    AUTH.store.throttle_fail(key)
                    return self.send(401, {"error": "wrong code"})
                AUTH.store.throttle_clear(key)
                old = portal_token(self)
                if old:  # never promote a cookie that existed before this login
                    AUTH.store.revoke(old, "replaced by a new login")
                token = AUTH.store.create("customer", c["id"], customer_version(c["id"]))
                body = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.set_cookie(PORTAL_COOKIE, token, AUTH.store.policy("customer")[0])
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return None
            if path == "/api/portal/logout":
                if portal_token(self):
                    AUTH.store.revoke(portal_token(self), "logout")
                body = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.set_cookie(PORTAL_COOKIE, None, 0)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return None
            if path == "/api/portal/chat":
                cid = portal_customer(self)
                msg = (data.get("message") or "").strip()
                if not cid:
                    return self.send(401, {"error": "not logged in"})
                if not msg:
                    return self.send(400, {"error": "message required"})
                c = Memory(OWNER).customer("get", cid)
                agent = f"{c['company']}-receptionist"
                conv = CONV.open("customer", cid, agent, c["company"], data.get("conversation_id"))
                state, stored = CONV.begin(conv, data.get("message_id"), msg)
                if state == "done":  # the same message delivered again: answer from the store, run nothing
                    return self.send(200, {"reply": stored, "ok": True, "conversation_id": conv, "repeat": True})
                # History comes from the server's store, never from the browser.
                convo = "\n".join(f"{r.upper()}: {t}" for r, t in CONV.history(conv))
                # The identity line comes from the server session, not from the customer.
                prompt = (f"[Verified by the platform: you are talking to customer {c['name']} ({cid}) of "
                          f"{BOARDS.get(c['company'], c['company'])}. The customer is already registered; your memory "
                          f"tools are bound to this customer. Read what we know with memory_search; save anything "
                          f"the customer asks you to remember with memory_write (scope 'customer'). Say something "
                          f"was saved only if memory_write returned an id. "
                          f"Reply in the customer's language ({c['language']}).]\n\n"
                          + (f"Conversation so far:\n{convo}\n\n" if convo else "") + f"CUSTOMER: {msg}")
                try:
                    rc, out, err = hermes("-p", agent, "-z", prompt, timeout=900, env={**ENV, "FED_CUSTOMER": cid})
                except Exception:
                    CONV.finish(conv, data.get("message_id"), "", False)
                    raise
                reply = out.strip() if rc == 0 else "The assistant could not answer right now; please send it again."
                CONV.finish(conv, data.get("message_id"), reply, rc == 0)
                return self.send(200, {"reply": reply, "ok": rc == 0, "conversation_id": conv})
            if path == "/api/memory/override":
                return self.send(200, Memory(OWNER).override(int(data.get("id", 0)), data.get("body", ""),
                                                              data.get("reason", "")))
            if path == "/api/memory/lessons":
                return self.send(200, Memory(OWNER).lesson(data.get("action"), lesson_id=data.get("id"),
                                                           sanitized_text=data.get("sanitized_text")))
            if path.startswith("/api/models/"):
                return self.models_post(path, data)
            known = {a["id"] for a in agents()}
            if path == "/api/tasks":
                board, title = data.get("board"), (data.get("title") or "").strip()
                assignee, parent = data.get("assignee") or "", data.get("parent") or ""
                if board not in BOARDS or not title or (assignee and assignee not in known):
                    return self.send(400, {"error": "board, title and a known assignee are required"})
                args = ["kanban", "--board", board, "create", title, "--workspace", "worktree",
                        "--max-runtime", str(int(data.get("max_runtime") or 1800)),
                        "--max-retries", "2"]  # R06.05: bounded retries, then the task blocks
                if data.get("body"):
                    args += ["--body", data["body"]]
                if assignee:
                    args += ["--assignee", assignee]
                if parent:
                    if not TASK_ID.match(parent):
                        return self.send(400, {"error": "bad parent id"})
                    args += ["--parent", parent]
                return self.send(200, hermes_json(*args))
            m = re.fullmatch(r"/api/boards/([a-z]+)/dispatch", path)
            if m and m.group(1) in BOARDS:
                _, out, err = hermes("kanban", "--board", m.group(1), "dispatch", timeout=180)
                return self.send(200, {"output": (out or err).strip()})
            if path == "/api/pilot/chat":  # G2-2 pilot: runs as hermes-agent, never one of the 118 live agents
                try:
                    return self.send(200, PILOT.chat(data.get("message") if isinstance(data.get("message"), str) else ""))
                except ValueError as e:
                    return self.send(400, {"error": str(e)})
                except agent_runner.PilotUnavailable as e:
                    return self.send(503, {"error": str(e)})
                except agent_runner.PilotBusy as e:
                    return self.send(409, {"error": str(e)})
                except agent_runner.AgentRunnerError as e:
                    return self.send(502, {"error": str(e)[:600]})
            if path == "/api/chat":
                profile, msg = data.get("profile"), (data.get("message") or "").strip()
                if profile not in known or not msg:
                    return self.send(400, {"error": "known profile and message required"})
                company = "federation" if profile.startswith("fed-") else profile.split("-", 1)[0]
                conv = CONV.open("owner", "owner", profile, company, data.get("conversation_id"))
                state, stored = CONV.begin(conv, data.get("message_id"), msg)
                if state == "done":
                    return self.send(200, {"reply": stored, "ok": True, "conversation_id": conv, "repeat": True})
                turns = CONV.history(conv)
                prompt = msg
                if turns:
                    convo = "\n".join(f"{r.upper()}: {t}" for r, t in turns)
                    prompt = f"Conversation so far:\n{convo}\n\nUSER: {msg}"
                try:
                    rc, out, err = hermes("-p", profile, "-z", prompt, timeout=900)
                except Exception:
                    CONV.finish(conv, data.get("message_id"), "", False)
                    raise
                reply = out.strip() if rc == 0 else (err.strip()[-800:] or "the agent did not answer")
                CONV.finish(conv, data.get("message_id"), reply, rc == 0)
                return self.send(200, {"reply": reply, "ok": rc == 0, "conversation_id": conv})
            return self.send(404, {"error": "not found"})

    def models_post(self, path, data):
        """Model management. A typed key is used for one request and never echoed; nothing changes an agent
        without a successful test, a preview and an explicit confirmation of that preview."""
        key = data.get("key") if isinstance(data.get("key"), str) else None
        try:
            if path == "/api/models/test":
                return self.send(200, models.check(data.get("provider") or "", data.get("base_url") or None,
                                                   data.get("model") or None, key or None))
            if path == "/api/models/save":
                name, provider = data.get("name") or "", data.get("provider") or ""
                stored = MODELS.read_secret(MODELS.key_env(name)) if models.NAME.match(name) else None
                res = models.check(provider, data.get("base_url") or None, data.get("model") or None, key or stored)
                if res["status"] != "ok":
                    return self.send(400, {"error": f"not saved: the test returned {res['status']}", "test": res})
                cfg = MODELS.save(name, provider, data.get("base_url") or None, data.get("model") or "", key or None,
                                  {"status": "ok", "at": time.time()})
                return self.send(200, {"saved": cfg, "test": res})
            if path == "/api/models/ollama-load":
                model = data.get("model") or ""
                if not models.MODEL_ID.match(model):
                    return self.send(400, {"error": "choose an installed model"})
                started = OLLAMA.load(model)
                return self.send(202, {"loading": model, "started": started})
            if path == "/api/models/retest":
                if (data.get("name") or "") not in MODELS.list():
                    return self.send(404, {"error": "no such configuration"})
                return self.send(200, MODELS.retest(data["name"]))
            if path == "/api/models/plan":
                return self.send(200, MODELS.plan(data.get("name") or "", data.get("purpose") or "",
                                                  data.get("company") or None, agents()))
            if path == "/api/models/apply":
                if data.get("confirm") != "APPLY":
                    return self.send(400, {"error": "type APPLY to confirm the previewed change"})
                plan, cfg = MODELS.take_plan(data.get("plan_id"))
                secret = MODELS.read_secret(cfg["key_env"]) if cfg.get("key_env") else None
                done = []
                for agent in plan["agents"]:
                    models.apply_to_profile(ROUTE.profile_dir(agent).parent, agent, plan["name"], cfg, secret)
                    admin_audit("admin/models/apply.agent", agent, "ok", f"{cfg['provider']}/{cfg['model']}")
                    done.append(agent)
                return self.send(200, {"applied": done, "config": plan["name"]})
            return self.send(404, {"error": "not found"})
        except models.ModelError as e:
            return self.send(400, {"error": models.scrub(e, key)})


# Windows: http.server sets SO_REUSEADDR, which there lets ANY other local process bind the same address and port and
# take over connections (an agent could capture the owner's sign-in). SO_EXCLUSIVEADDRUSE refuses every other bind on
# the port, before and while the console listens; a port already held makes the console exit instead of sharing it.
# Elsewhere the platform default stays (Linux/macOS SO_REUSEADDR does not allow two active listeners; not verified here).
EXCLUSIVE_BIND = hasattr(socket, "SO_EXCLUSIVEADDRUSE")


class ConsoleServer(ThreadingHTTPServer):
    allow_reuse_address = not EXCLUSIVE_BIND

    def server_bind(self):
        if EXCLUSIVE_BIND:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def bind_mode():
    return "exclusive (SO_EXCLUSIVEADDRUSE)" if EXCLUSIVE_BIND else "platform default (exclusivity not verified)"


if __name__ == "__main__":
    if HOST not in ("127.0.0.1", "localhost", "::1") and os.environ.get("CONSOLE_ALLOW_REMOTE") != "1":
        raise SystemExit("Listen on 127.0.0.1 and publish through Tailscale or Cloudflare Tunnel (R05.05); "
                         "set CONSOLE_ALLOW_REMOTE=1 only behind HTTPS.")
    sync_api_keys()
    apply_agent_model()
    stuck = CONV.recover_pending(older_than=0)  # nothing can still be answering a message after a restart
    if stuck:
        print(f"{stuck} unanswered chat message(s) from before the restart marked for retry", flush=True)
    if os.environ.get("FED_DISABLE_SYNC") != "1":  # a second console on the same data must not run a second sync
        threading.Thread(target=sync_loop, daemon=True).start()
    else:
        print("sync loop disabled (FED_DISABLE_SYNC=1)", flush=True)
    try:  # bind before announcing anything: the setup code is printed only once the port is ours
        server = ConsoleServer((HOST, PORT), Handler)
    except OSError as e:
        raise SystemExit(f"cannot listen on {HOST}:{PORT} ({e}); another process holds this port. "
                         "Find it with: netstat -ano | findstr :" + str(PORT))
    print(f"Federation Console on http://{HOST}:{PORT}  (owner sign-in at /login; bind: {bind_mode()})", flush=True)
    if AUTH.setup_code:
        print(f"No owner yet. One-time setup code: {AUTH.setup_code}  (enter it at /login to set the owner password)",
              flush=True)
    server.serve_forever()
