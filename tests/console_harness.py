"""Start an isolated Federation Console for tests: its own port, temporary Hermes home (profile config.yaml
and profile.yaml copied read-only from the real home, never their .env), temporary databases and a stub
`hermes` that answers chats and records every call. The real console, databases and boards are not touched.

    from console_harness import Console
    with Console() as c:
        c.setup_owner()               # one-time code from the console's own output
        status, body = c.owner.get("/api/agents")
"""
import http.cookiejar
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
REAL = Path(os.environ.get("HERMES_HOME") or (
    Path(os.environ["LOCALAPPDATA"]) / "hermes" if os.name == "nt" else Path.home() / ".hermes"))
OURS = re.compile(r"^(fed|isc|auvia|tabbeqai|spectra|spectrasolutions)-[a-z-]+$")

STUB = r'''import json, os, sqlite3, sys, time, uuid
a = sys.argv[1:]
log = os.environ["FED_STUB_LOG"]
open(log, "a", encoding="utf-8").write(json.dumps(a[:4]) + "\n")
if "-z" in a:
    delay = float(os.environ.get("FED_STUB_DELAY", "0") or 0)
    flag = os.environ.get("FED_STUB_DELAY_FILE")
    if flag and os.path.exists(flag):
        time.sleep(float(open(flag).read() or 0))
    time.sleep(delay)
    prompt = a[a.index("-z") + 1]
    print("STUB REPLY [" + a[a.index("-p") + 1] + "] to: " + prompt.strip().splitlines()[-1][:120])
elif a[:1] == ["kanban"] and len(a) > 3 and a[3] == "create":
    kid = "t_" + uuid.uuid4().hex[:8]
    with sqlite3.connect(os.environ["FED_STUB_BOARD"]) as db:
        db.execute("INSERT INTO tasks (id, title, status) VALUES (?,?,?)", (kid, a[4], "ready"))
    print(json.dumps({"id": kid}))
'''


class Client:
    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method, path, body=None, timeout=60, origin=True):
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = self.base
        req = urllib.request.Request(self.base + path, json.dumps(body).encode() if body is not None else None,
                                     headers, method=method)
        try:
            with self.op.open(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw.decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"{}")
            except ValueError:
                return e.code, raw.decode("utf-8", "replace")

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body if body is not None else {}, **kw)

    def cookie(self, name):
        return next((c.value for c in self.jar if c.name == name), None)

    def set_cookie(self, name, value):
        self.jar.set_cookie(http.cookiejar.Cookie(0, name, value, None, False, "127.0.0.1", False, False, "/", True,
                                                  False, None, False, None, None, {"HttpOnly": None}))


class Console:
    def __init__(self, port=8768, env=None, src=None):
        self.port, self.extra_env = port, env or {}
        self.src = Path(src) if src else SRC
        self.base = f"http://127.0.0.1:{port}"
        self.dir = Path(tempfile.mkdtemp(prefix="fed-console-"))
        self.home = self.dir / "home"
        self.proc, self.logs = None, []
        self.password = None

    # -- fixture
    def build(self):
        prof = self.home / "profiles"
        for d in (REAL / "profiles").iterdir():
            if d.is_dir() and OURS.match(d.name):
                (prof / d.name).mkdir(parents=True)
                for f in ("config.yaml", "profile.yaml"):
                    if (d / f).exists():
                        shutil.copy2(d / f, prof / d.name / f)
        fed = self.home / "federation"
        fed.mkdir(parents=True)
        for n in ("memory.db", "workflow.db"):  # copies of the real stores, so migrations run on real shapes
            src = sqlite3.connect(f"file:{REAL / 'federation' / n}?mode=ro", uri=True)
            dst = sqlite3.connect(fed / n)
            src.backup(dst)
            src.close()
            dst.close()
        board = self.home / "kanban" / "boards"
        for b in ("federation", "isc", "auvia", "tabbeqai", "spectra", "spectrasolutions"):
            (board / b).mkdir(parents=True)
            with sqlite3.connect(board / b / "kanban.db") as k:
                k.executescript("""CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT,
                    status TEXT, branch_name TEXT, result TEXT, last_failure_error TEXT, workspace_kind TEXT,
                    created_at INTEGER, started_at INTEGER, completed_at INTEGER);
                    CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, profile TEXT,
                    status TEXT, outcome TEXT, summary TEXT, error TEXT, started_at REAL, ended_at REAL, metadata TEXT);
                    CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER,
                    kind TEXT, payload TEXT, created_at INTEGER);
                    CREATE TABLE task_comments (id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT, created_at INTEGER);
                    CREATE TABLE task_links (parent_id TEXT, child_id TEXT);""")
        (self.dir / "stub.py").write_text(STUB, encoding="utf-8")
        self.stub_log = self.dir / "stub.log"
        self.stub_log.write_text("")
        return self

    def env(self):
        e = {**os.environ, "HERMES_HOME": str(self.home), "FED_OWNER_FILE": str(self.home / "federation" / "owner.json"),
             "FED_MEMORY_DB": str(self.home / "federation" / "memory.db"),
             "FED_WORKFLOW_DB": str(self.home / "federation" / "workflow.db"),
             "FED_CONVERSATIONS_DB": str(self.home / "federation" / "conversations.db"),
             "FED_HERMES_CMD": f'"{sys.executable}" -I "{self.dir / "stub.py"}"', "FED_WORKSPACES": str(self.dir / "ws"),
             "FED_STUB_LOG": str(self.stub_log), "FED_STUB_BOARD": str(self.home / "kanban" / "boards" / "isc" / "kanban.db"),
             "CONSOLE_PORT": str(self.port), "CONSOLE_HOST": "127.0.0.1", "GROQ_API_KEY": "", "PYTHONIOENCODING": "utf-8"}
        for k in ("AGENT_PROVIDER", "AGENT_MODEL", "CONSOLE_PASSWORD"):
            e.pop(k, None)
        e.update(self.extra_env)
        return e

    # -- process
    def start(self, **env):
        self.extra_env.update(env)
        log = self.dir / f"console-{len(self.logs)}.log"
        self.logs.append(log)
        self._log = open(log, "w", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, "-I", str(self.src / "console" / "server.py")], env=self.env(),
                                     stdout=self._log, stderr=subprocess.STDOUT)
        for _ in range(80):
            try:
                urllib.request.urlopen(self.base + "/login", timeout=2)
                return self
            except (urllib.error.URLError, ConnectionError, OSError):
                if self.proc.poll() is not None:
                    raise RuntimeError("console exited: " + log.read_text(encoding="utf-8")[-800:])
                time.sleep(0.25)
        raise RuntimeError("console did not start")

    def stop(self, kill=False):
        if self.proc and self.proc.poll() is None:
            (self.proc.kill if kill else self.proc.terminate)()
            self.proc.wait(timeout=15)
        if getattr(self, "_log", None):
            self._log.close()

    def restart(self, **env):
        self.stop()
        return self.start(**env)

    def output(self):
        return "".join(p.read_text(encoding="utf-8") for p in self.logs)

    def stub_calls(self, kind="-z"):
        lines = [json.loads(x) for x in self.stub_log.read_text(encoding="utf-8").splitlines() if x.strip()]
        return [x for x in lines if kind in x or (kind != "-z" and x[:1] == [kind])]

    def setup_owner(self):
        code = re.findall(r"setup code: ([0-9a-f]{8})", self.output())[-1]
        self.password = "Test-" + secrets.token_urlsafe(12)
        self.owner = Client(self.base)
        status, _ = self.owner.post("/api/owner/setup", {"code": code, "password": self.password})
        assert status == 200, status
        return self.owner

    def client(self):
        return Client(self.base)

    def __enter__(self):
        return self.build().start()

    def __exit__(self, *exc):
        self.stop()
        shutil.rmtree(self.dir, ignore_errors=True)
