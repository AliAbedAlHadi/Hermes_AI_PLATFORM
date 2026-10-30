"""Phase 5 tests against an isolated console (tests/console_harness.py): the federation map (5.1) and
provider-independent model management (5.2). Providers are a local mock HTTP server: no real key, no paid
call, no request leaves this machine. Agent changes happen only in the harness's copied profiles.

    python -I tests/test_phase5.py
"""
import hashlib
import json
import socket
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "console"))
sys.path.insert(0, str(HERE.parent / "memory"))
from console_harness import Console  # noqa: E402
import models  # noqa: E402
import viz  # noqa: E402
from fedmem import load_org  # noqa: E402

results = {"pass": 0, "fail": 0}
FAKE_KEY = "sk-test-" + hashlib.sha256(str(time.time()).encode()).hexdigest()[:24]  # a made-up test value


def check(name, ok, info=""):
    results["pass" if ok else "fail"] += 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {info}" if info and not ok else ""), flush=True)


# ---------------------------------------------------------------- mock providers (loopback only)
class Mock(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *_):
        pass

    def reply(self, code, body=b"", headers=()):
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except ConnectionError:
            pass  # the client gave up first (the timeout case)

    def do_GET(self):
        Mock.seen.append((self.path, self.headers.get("Authorization")))
        auth = self.headers.get("Authorization") == f"Bearer {FAKE_KEY}"
        p = self.path
        if p == "/ok/v1/models":
            return self.reply(200, json.dumps({"data": [{"id": "mock-large"}, {"id": "mock-small"}]}).encode()) if auth \
                else self.reply(401, b'{"error":"bad key"}')
        if p == "/rl/v1/models":
            return self.reply(429)
        if p == "/bad/v1/models":
            return self.reply(200, b"<html>not json</html>")
        if p == "/empty/v1/models":
            return self.reply(200, b'{"data": []}')
        if p == "/redir/v1/models":
            return self.reply(302, headers=[("Location", "http://127.0.0.1:9/steal")])
        if p == "/slow/v1/models":
            time.sleep(4)
            return self.reply(200, b'{"data":[{"id":"x"}]}')
        if p == "/ollama/api/tags":
            return self.reply(200, json.dumps({"models": [{"name": "qwen3.5:9b"}, {"name": "tiny:1b"}]}).encode())
        if p == "/ollama/api/ps":
            return self.reply(200, json.dumps({"models": [{"name": "qwen3.5:9b", "context_length": 32768}]}).encode())
        return self.reply(404)


srv = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()
MOCK = f"http://127.0.0.1:{srv.server_address[1]}"

# ================================================================ unit: URL validation and SSRF (no network)
print("== 5.2 unit: base URL validation and SSRF refusal")
bad = [("custom", "http://example.com/v1", "plain http"), ("custom", "https://127.0.0.1/v1", "loopback"),
       ("custom", "https://10.1.2.3/v1", "private 10/8"), ("custom", "https://192.168.1.10/v1", "private 192.168/16"),
       ("custom", "https://169.254.169.254/latest", "metadata"), ("custom", "https://100.64.0.1/v1", "CGNAT"),
       ("custom", "https://[::1]/v1", "IPv6 loopback"), ("custom", "https://localhost/v1", "localhost name"),
       ("custom", "https://printer.local/v1", ".local name"), ("custom", "https://user:pw@example.com/v1", "credentials in URL"),
       ("custom", "https://example.com/v1?x=1", "query string"), ("custom", "file:///etc/passwd", "file scheme"),
       ("openai", "https://evil.example.com/v1", "official provider moved to another host"),
       ("groq", "http://api.groq.com/openai/v1", "official provider over http"),
       ("ollama", "http://192.168.1.5:11434", "Ollama on another machine"), ("nope", None, "unknown provider")]
for prov, url, why in bad:
    try:
        models.validate(prov, url, resolve=False)
        check(f"refused: {why}", False, url)
    except models.ModelError:
        check(f"refused: {why}", True)
check("official defaults accepted", all(models.validate(p, None, resolve=False) == models.PROVIDERS[p]["base"]
                                         for p in ("openai", "groq", "deepseek", "ollama")))
check("a public custom endpoint is accepted", models.validate("custom", "https://93.184.215.14/v1", resolve=False)
      == "https://93.184.215.14/v1")
real_gai = socket.getaddrinfo
socket.getaddrinfo = lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 443))]
try:
    models.validate("custom", "https://rebind.example.com/v1")
    check("a custom host that resolves to a private address is refused (DNS check)", False)
except models.ModelError:
    check("a custom host that resolves to a private address is refused (DNS check)", True)
finally:
    socket.getaddrinfo = real_gai
check("scrub removes the key and key-shaped strings", FAKE_KEY not in models.scrub(f"x {FAKE_KEY} y", FAKE_KEY)
      and "gsk_abcdefghijkl" not in models.scrub("gsk_abcdefghijkl"))

c = Console(port=8769, env={"FED_MODELS_ALLOW_LOOPBACK_CUSTOM": "1", "FED_MODELS_TIMEOUT": "2"}).build()
keys_file = c.home / "federation" / "model-keys.env"
keys_file.write_text("UNRELATED_SETTING=keep-me-exactly\n", encoding="utf-8")
cfg_hash = lambda: {d.name: hashlib.sha256((d / "config.yaml").read_bytes()).hexdigest()
                    for d in (c.home / "profiles").iterdir()}
c.start()
try:
    anon = c.client()
    # ================================================================ 5.1 map
    print("== 5.1 federation map: inventory, relationships, status, authorisation")
    check("map and models refuse anonymous callers", anon.get("/api/viz")[0] == 401 and anon.get("/api/models")[0] == 401
          and anon.post("/api/models/test", {"provider": "ollama"})[0] == 401)
    owner = c.setup_owner()
    # a conversation and a task body carrying markers that must never reach the map
    owner.post("/api/chat", {"profile": "isc-ceo", "message": "CONV-MARKER-77 private", "message_id": "m5-0001"})
    now = time.time()
    isc = c.home / "kanban" / "boards" / "isc" / "kanban.db"
    with sqlite3.connect(isc) as k:
        k.executemany("INSERT INTO tasks (id, title, body, assignee, status, created_at) VALUES (?,?,?,?,?,?)", [
            ("t_aa000001", "live work", "BODY-MARKER-55", "isc-executor", "running", now),
            ("t_aa000002", "old work", "", "isc-qa", "running", now - 7200),
            ("t_aa000003", "waiting on vendor", "", "isc-devops", "blocked", now),
            ("t_aa000004", "crashed work", "", "isc-security", "ready", now),
            ("t_aa000005", "finished", "", "isc-finance", "done", now)])
        k.executemany("INSERT INTO task_runs (task_id, profile, status, started_at, ended_at) VALUES (?,?,?,?,?)", [
            ("t_aa000001", "isc-executor", "running", now - 30, None),
            ("t_aa000002", "isc-qa", "running", now - 7200, None),
            ("t_aa000004", "isc-security", "crashed", now - 900, now - 600),
            ("t_aa000005", "isc-finance", "done", now - 900, now - 800)])
    t0 = time.perf_counter()
    s, m = owner.get("/api/viz")
    rt_ms = (time.perf_counter() - t0) * 1000
    check("the owner gets the map", s == 200, s)
    nodes = {n["id"]: n for n in m["agents"]}
    profiles = sorted(d.name for d in (c.home / "profiles").iterdir())
    check("118 agents, exactly the profiles on disk", len(nodes) == 118 and sorted(nodes) == profiles)
    org = load_org(HERE.parent / "roster" / "roster.json")
    check("company, role, department and level match the roster for every agent", all(
        n["company"] == org[i]["company"] and n["role"] == org[i]["role"] and n["department"] == org[i]["department"]
        and n["level"] == org[i]["level"] for i, n in nodes.items()), [i for i, n in nodes.items() if n["company"] != org[i]["company"]][:3])
    check("6 companies: federation 8 agents, each company 22", {co["id"]: co["agents"] for co in m["companies"]} == {
        "federation": 8, "isc": 22, "auvia": 22, "tabbeqai": 22, "spectra": 22, "spectrasolutions": 22})
    esc_map = viz._escalate_map(HERE.parent)
    expected = set()
    for i, n in nodes.items():
        t = viz.FED_ESCALATE.get(n["role"]) if n["company"] == "federation" else esc_map.get(n["role"])
        tid = (f"fed-{t}" if n["company"] == "federation" else f"{n['company']}-{t}") if t else (
            None if n["company"] == "federation" else "fed-coo")
        if tid:
            expected.add((i, tid))
    got = {(e["from"], e["to"]) for e in m["edges"]}
    check("escalation lines equal create_profiles.ESCALATE (plus CEO -> fed-coo)", got == expected,
          f"missing {sorted(expected - got)[:3]} extra {sorted(got - expected)[:3]}")
    check("every edge joins two known agents", all(e["from"] in nodes and e["to"] in nodes for e in m["edges"]))
    check("statuses come only from the defined set", {n["status"] for n in nodes.values()} <= set(viz.STATUSES)
          and m["statuses"] == list(viz.STATUSES))
    check("live: a running run with a fresh heartbeat", nodes["isc-executor"]["status"] == "live", nodes["isc-executor"])
    check("stale: marked running, heartbeat two hours old", nodes["isc-qa"]["status"] == "stale", nodes["isc-qa"])
    check("blocked: an assigned Kanban task is blocked", nodes["isc-devops"]["status"] == "blocked", nodes["isc-devops"])
    check("failed: the last run crashed 10 minutes ago", nodes["isc-security"]["status"] == "failed", nodes["isc-security"])
    check("idle: worked before, nothing active", nodes["isc-finance"]["status"] == "idle", nodes["isc-finance"])
    check("unknown exactly when no run or federation task was ever recorded (never invented)", all(
        (n["status"] == "unknown") == (n["runs"] == 0 and n["federation_tasks"] == 0)
        for n in nodes.values() if n["status"] != "blocked"))
    st = {s_: 0 for s_ in viz.STATUSES}
    for n in nodes.values():
        st[n["status"]] += 1
    check("company status counts add up to the agents", sum(sum(co["status"].values()) for co in m["companies"]) == 118
          and all(sum(co["status"][x] for co in m["companies"]) == st[x] for x in st))
    blob = json.dumps(m)
    check("no conversation text or task body in the map", "CONV-MARKER-77" not in blob and "BODY-MARKER-55" not in blob)
    fields = set()

    def walk(o):
        if isinstance(o, dict):
            for k_, v_ in o.items():
                fields.add(k_)
                walk(v_)
        elif isinstance(o, list):
            for v_ in o:
                walk(v_)
    walk(m)
    check("no secret-shaped fields in the map", not any(w in f.lower() for f in fields for w in
                                                        ("key", "token", "password", "secret", "summary", "body")), fields)
    check(f"map performance: built in {m['build_ms']} ms on the server, {rt_ms:.0f} ms round trip",
          m["build_ms"] < 3000 and rt_ms < 5000)
    (c.home / "kanban" / "boards" / "spectrasolutions" / "kanban.db").write_bytes(b"not a database" * 50)
    s, m2 = owner.get("/api/viz")
    check("an unreadable board does not break the map (its agents fall back to recorded facts only)",
          s == 200 and len(m2["agents"]) == 118)
    check("the rest of the console keeps working", owner.get("/api/agents")[0] == 200 and owner.get("/api/ftasks")[0] == 200)

    # ================================================================ 5.2 model management
    print("== 5.2 model management: tests, errors, secrets, two-step apply")
    s, info = owner.get("/api/models")
    check("providers listed: openai, groq, deepseek, ollama, custom", s == 200 and set(info["providers"]) ==
          {"openai", "groq", "deepseek", "ollama", "custom"} and info["configs"] == [])
    T = lambda **b: owner.post("/api/models/test", b)
    s, r = T(provider="custom", base_url=MOCK + "/ok/v1", model="mock-large", key=FAKE_KEY)
    check("valid key: ok and the model is offered", s == 200 and r["status"] == "ok" and "mock-large" in r["models"], r)
    check("the key reached the provider only as a bearer header", (f"/ok/v1/models", f"Bearer {FAKE_KEY}") in Mock.seen)
    s, r = T(provider="custom", base_url=MOCK + "/ok/v1", model="other-model", key=FAKE_KEY)
    check("model not offered: model_missing", r["status"] == "model_missing", r)
    s, r = T(provider="custom", base_url=MOCK + "/ok/v1", model="mock-large", key="sk-wrong-000000000")
    check("refused key: auth", r["status"] == "auth", r)
    s, r = T(provider="custom", base_url=MOCK + "/ok/v1", model="mock-large")
    check("missing key: auth, no request made", r["status"] == "auth")
    check("rate limit: rate_limited", T(provider="custom", base_url=MOCK + "/rl/v1", key=FAKE_KEY)[1]["status"] == "rate_limited")
    check("non-JSON answer: malformed", T(provider="custom", base_url=MOCK + "/bad/v1", key=FAKE_KEY)[1]["status"] == "malformed")
    check("empty model list: malformed", T(provider="custom", base_url=MOCK + "/empty/v1", key=FAKE_KEY)[1]["status"] == "malformed")
    n_before = len(Mock.seen)
    r = T(provider="custom", base_url=MOCK + "/redir/v1", key=FAKE_KEY)[1]
    check("redirect: reported, not followed", r["status"] == "redirect" and not any("/steal" in p for p, _ in Mock.seen[n_before:]), r)
    check("timeout: reported after the limit", T(provider="custom", base_url=MOCK + "/slow/v1", key=FAKE_KEY)[1]["status"] == "timeout")
    check("unreachable endpoint: unreachable", T(provider="custom", base_url="http://127.0.0.1:9/v1", key=FAKE_KEY)[1]["status"]
          in ("unreachable", "timeout"))
    check("private address refused before any request", T(provider="custom", base_url="https://10.0.0.5/v1", key=FAKE_KEY)[1]["status"] == "invalid")
    check("official provider pointed elsewhere refused", T(provider="openai", base_url=MOCK + "/ok/v1", key=FAKE_KEY)[1]["status"] == "invalid")
    s, r = T(provider="ollama", base_url=MOCK + "/ollama", model="qwen3.5:9b")
    check("Ollama: reachable, installed vs loaded distinguished", r["status"] == "ok" and "tiny:1b" in r["installed"]
          and [x["name"] for x in r["loaded"]] == ["qwen3.5:9b"], r)
    s, r = T(provider="ollama", base_url=MOCK + "/ollama", model="tiny:1b")
    check("Ollama: an installed model that is not loaded is said so", r["status"] == "ok" and "not loaded" in r["detail"], r)
    check("Ollama: a missing model is model_missing", T(provider="ollama", base_url=MOCK + "/ollama", model="absent:7b")[1]["status"] == "model_missing")

    s, r = owner.post("/api/models/save", {"name": "rev-mock", "provider": "custom", "base_url": MOCK + "/ok/v1",
                                           "model": "mock-large", "key": "sk-wrong-000000000"})
    check("save is refused when the test fails (test before activate)", s == 400 and "rev-mock" not in json.dumps(owner.get("/api/models")[1]["configs"]))
    s, r = owner.post("/api/models/save", {"name": "rev-mock", "provider": "custom", "base_url": MOCK + "/ok/v1",
                                           "model": "mock-large", "key": FAKE_KEY})
    check("save after a passing test", s == 200 and r["saved"]["has_key"] and r["saved"]["key_env"] == "FED_MODEL_KEY_REV_MOCK", r)
    s, r = owner.post("/api/models/save", {"name": "local-qwen", "provider": "ollama", "base_url": MOCK + "/ollama",
                                           "model": "qwen3.5:9b"})
    check("an Ollama configuration needs no key", s == 200 and r["saved"]["key_env"] is None, r)
    check("models.json holds no key", FAKE_KEY not in (c.home / "federation" / "models.json").read_text(encoding="utf-8"))
    kf = keys_file.read_text(encoding="utf-8")
    check("the key file keeps every other line as it was", kf.startswith("UNRELATED_SETTING=keep-me-exactly\n")
          and kf.count("FED_MODEL_KEY_REV_MOCK=") == 1)
    check("retest of a saved configuration uses the stored key", owner.post("/api/models/retest", {"name": "rev-mock"})[1]["status"] == "ok")

    before = cfg_hash()
    s, r = owner.post("/api/models/plan", {"name": "rev-mock", "purpose": "reviewer", "company": "isc"})
    check("preview lists exactly the chosen agents", s == 200 and [x["agent"] for x in r["changes"]] == ["isc-reviewer"], r)
    check("nothing changes on preview", cfg_hash() == before)
    check("apply without the typed confirmation is refused", owner.post("/api/models/apply", {"plan_id": r["plan_id"]})[0] == 400)
    s, a = owner.post("/api/models/apply", {"plan_id": r["plan_id"], "confirm": "APPLY"})
    check("apply with confirmation", s == 200 and a["applied"] == ["isc-reviewer"], a)
    after = cfg_hash()
    check("only the chosen agent's config changed (117 identical)", [k_ for k_ in after if after[k_] != before[k_]] == ["isc-reviewer"])
    rv = c.home / "profiles" / "isc-reviewer"
    txt = (rv / "config.yaml").read_text(encoding="utf-8")
    check("the agent now selects the new provider; backup kept", 'provider: "fed-rev-mock"' in txt and '"mock-large"' in txt
          and "  fed-rev-mock:" in txt and "ollama_num_ctx" not in txt.split("\nmodel:", 1)[1][:2500]
          and (rv / "config.yaml.bak-models").exists() and FAKE_KEY not in txt)
    check("the guard hook and memory tool are still in its config", "fed_guard.py" in txt and "federation-memory:" in txt)
    check("the key reached only that agent's own .env", (rv / ".env").read_text(encoding="utf-8").strip() ==
          f"FED_MODEL_KEY_REV_MOCK={FAKE_KEY}" and not any((d / ".env").exists() for d in (c.home / "profiles").iterdir()
                                                           if d.name != "isc-reviewer"))
    check("a used preview cannot be applied twice", owner.post("/api/models/apply", {"plan_id": r["plan_id"], "confirm": "APPLY"})[0] == 400)
    s, r2 = owner.post("/api/models/plan", {"name": "local-qwen", "purpose": "reviewer", "company": "isc"})
    owner.post("/api/models/save", {"name": "local-qwen", "provider": "ollama", "base_url": MOCK + "/ollama", "model": "tiny:1b"})
    check("a preview is void once its configuration changed", owner.post("/api/models/apply", {"plan_id": r2["plan_id"], "confirm": "APPLY"})[0] == 400)
    check("unknown purpose refused", owner.post("/api/models/plan", {"name": "rev-mock", "purpose": "everyone"})[0] == 400)
    s, ags = owner.get("/api/agents")
    check("the console reports the new model for that agent only", [x["id"] for x in ags if x["provider"].startswith("fed-")] == ["isc-reviewer"])
    c.restart(AGENT_PROVIDER="local-ollama", AGENT_MODEL="qwen3.5:9b")
    s, info = owner.get("/api/models")
    check("saved configurations persist across a restart", s == 200 and {x["name"] for x in info["configs"]} == {"rev-mock", "local-qwen"})
    check("a start-up AGENT_PROVIDER switch keeps the owner's explicit assignment",
          'provider: "fed-rev-mock"' in (rv / "config.yaml").read_text(encoding="utf-8"))
    con = sqlite3.connect(c.home / "federation" / "memory.db")
    rows = con.execute("SELECT action, target, result, detail FROM audit WHERE action LIKE 'admin/models%'").fetchall()
    con.close()
    check("model actions are audited, per agent for apply", any(x[0] == "admin/models/apply.agent" and x[1] == "isc-reviewer" for x in rows)
          and any(x[0] == "admin/models/save" for x in rows))
    out = c.output()
    check("the key never appears in responses' logs, audit rows or console output", FAKE_KEY not in out and
          not any(FAKE_KEY in json.dumps(x) for x in rows) and FAKE_KEY not in json.dumps(info))
finally:
    c.stop()
    srv.shutdown()
    import shutil
    shutil.rmtree(c.dir, ignore_errors=True)
print(f"\n{results['pass']} passed, {results['fail']} failed")
sys.exit(1 if results["fail"] else 0)
