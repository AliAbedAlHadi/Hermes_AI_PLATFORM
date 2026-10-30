"""Provider-independent model management (Phase 5.2).

Adapters: openai, groq, deepseek (fixed official hosts), ollama (local only) and custom OpenAI-compatible
endpoints. A connection test is one read-only, free request (GET <base>/models, or Ollama's /api/tags and
/api/ps); no chat call is ever made, so nothing is billed and no company data is sent.

Safety rules:
  * Official providers: HTTPS on their own host only; a base-URL override to another host is refused.
  * Custom endpoints: HTTPS only, and the host must not be (or resolve to) loopback, private, link-local,
    CGNAT or metadata addresses (SSRF). Ollama: http(s) to loopback only.
  * Redirects are never followed (a redirect could carry the key elsewhere); they are reported as errors.
  * A key typed in the browser is used for that one test request only, never stored unless the owner saves
    it, never returned, logged or put in an error message (every message is scrubbed).
  * Saved configurations keep only non-secret fields in models.json; the key goes to the secrets file under
    its own FED_MODEL_KEY_<NAME> variable. Existing lines in that file (e.g. GROQ_API_KEY) are never touched.
  * Applying a configuration to agents is a two-step, confirmed operation limited to the chosen purpose and
    company; every other agent's model stays as it is.
"""
import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROVIDERS = {
    "openai": {"label": "OpenAI", "base": "https://api.openai.com/v1", "hosts": {"api.openai.com"}, "key": True},
    "groq": {"label": "Groq", "base": "https://api.groq.com/openai/v1", "hosts": {"api.groq.com"}, "key": True},
    "deepseek": {"label": "DeepSeek", "base": "https://api.deepseek.com/v1", "hosts": {"api.deepseek.com"}, "key": True},
    # 127.0.0.1, not "localhost": on Windows "localhost" tries ::1 first and Ollama listens on IPv4 only, which costs
    # about 2 s on every new connection (measured: ~2,090 ms vs ~30 ms).
    "ollama": {"label": "Ollama (local)", "base": "http://127.0.0.1:11434", "hosts": None, "key": False},
    "custom": {"label": "OpenAI-compatible (custom)", "base": "", "hosts": None, "key": True},
}
# Which agents a purpose covers (role suffix of the profile name).
PURPOSES = {
    "worker": {"junior-developer", "senior-developer", "executor", "devops", "qa", "team-lead", "optimizer",
               "release-controller", "security", "solution-architect", "document-operations"},
    "reviewer": {"reviewer"},
    "verifier": {"verifier"},
    "business": {"receptionist", "ceo", "delivery-director", "engineering-director", "business-analyst",
                 "project-manager", "customer-success", "finance", "marketing"},
    "federation": None,  # every fed-* profile
}
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")
MODEL_ID = re.compile(r"^[A-Za-z0-9._:/@+-]{1,120}$")
TIMEOUT = float(os.environ.get("FED_MODELS_TIMEOUT") or 8)


class ModelError(Exception):
    pass


def scrub(text, *secrets_):
    """Remove any secret (and anything that looks like a bearer key) from a message."""
    text = str(text)
    for s in secrets_:
        if s:
            text = text.replace(s, "[redacted]")
    return re.sub(r"(sk-|gsk_|Bearer\s+)[A-Za-z0-9._-]{6,}", r"\1[redacted]", text)


def _blocked_ip(ip):
    ip = ipaddress.ip_address(ip)
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified
            or ip in ipaddress.ip_network("100.64.0.0/10") or ip in ipaddress.ip_network("169.254.0.0/16"))


def validate(provider, base_url, resolve=True):
    """Normalised base URL for this provider, or ModelError. Pure checks first, DNS check last."""
    if provider not in PROVIDERS:
        raise ModelError(f"unknown provider '{provider}'")
    spec = PROVIDERS[provider]
    base = (base_url or spec["base"]).strip().rstrip("/")
    try:
        u = urllib.parse.urlsplit(base)
    except ValueError:
        raise ModelError("the base URL is not a valid URL") from None
    if not u.scheme or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ModelError("the base URL must be scheme://host[:port]/path, without credentials, query or fragment")
    host = u.hostname.lower()
    allow_loop = os.environ.get("FED_MODELS_ALLOW_LOOPBACK_CUSTOM") == "1"  # tests only: mock servers on 127.0.0.1
    if provider == "ollama":
        if u.scheme not in ("http", "https") or host not in ("localhost", "127.0.0.1", "::1"):
            raise ModelError("Ollama must be reached on this machine (localhost); it is never exposed")
        # Ollama's own API lives at the root (/api/tags); "/v1" is only the chat path, added when applied to agents.
        path = re.sub(r"/v1$", "", u.path.rstrip("/"))
        netloc = u.netloc.replace("localhost", "127.0.0.1") if host == "localhost" else u.netloc
        return urllib.parse.urlunsplit((u.scheme, netloc, path, "", "")).rstrip("/")
    if spec["hosts"] is not None:
        if u.scheme != "https" or host not in spec["hosts"]:
            raise ModelError(f"{spec['label']} is only reachable at https://{next(iter(spec['hosts']))}")
        return base
    # custom OpenAI-compatible endpoint
    if allow_loop and host in ("127.0.0.1", "localhost") and u.scheme in ("http", "https"):
        return base
    if u.scheme != "https":
        raise ModelError("custom endpoints must use https")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and _blocked_ip(literal):
        raise ModelError("custom endpoints may not point at local, private or metadata addresses")
    if host in ("localhost",) or host.endswith((".local", ".internal", ".localhost")):
        raise ModelError("custom endpoints may not point at local names")
    if resolve and literal is None:
        try:
            infos = socket.getaddrinfo(host, u.port or 443, proto=socket.IPPROTO_TCP)
        except OSError:
            raise ModelError(f"cannot resolve {host}") from None
        if any(_blocked_ip(i[4][0]) for i in infos):
            raise ModelError("custom endpoint resolves to a local or private address; refused")
    return base


LOAD_TIMEOUT = float(os.environ.get("FED_OLLAMA_LOAD_TIMEOUT") or 600)  # a 9B model can take minutes from disk


def load_ollama_model(model, base_url=None, keep_alive="15m", num_ctx=None):
    """Ask Ollama to load `model` into memory and wait until it is ready (no prompt: nothing is generated or sent).
    `num_ctx` must be the context size the agents send: Ollama reloads a model whenever a request asks for a different
    one, so loading with another size would be undone by the first agent message. Returns the seconds it took.
    Raises ModelError if the model is not installed or Ollama refuses."""
    base = validate("ollama", base_url)
    if not MODEL_ID.match(model or ""):
        raise ModelError("the model id has unexpected characters")
    code, body, _ = _get(base + "/api/tags")
    installed = [m.get("name") for m in json.loads(body).get("models", [])] if code == 200 else []
    if model not in installed:
        raise ModelError(f"{model} is not installed in Ollama")
    payload = {"model": model, "keep_alive": keep_alive, "stream": False}
    if num_ctx:
        payload["options"] = {"num_ctx": int(num_ctx)}
    req = urllib.request.Request(base + "/api/generate", method="POST", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "hermes-federation/1"})
    started = time.perf_counter()
    try:
        with _OPENER.open(req, timeout=LOAD_TIMEOUT) as r:
            r.read(65536)
    except urllib.error.HTTPError as e:
        raise ModelError(f"Ollama refused to load {model} (HTTP {e.code})") from None
    except (urllib.error.URLError, OSError) as e:
        raise ModelError(f"loading {model} failed: {type(getattr(e, 'reason', e)).__name__}") from None
    return round(time.perf_counter() - started, 1)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None  # never follow: report instead


_OPENER = urllib.request.build_opener(_NoRedirect)


def _get(url, key=None):
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-federation/1", "Accept": "application/json"})
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    started = time.perf_counter()
    try:
        with _OPENER.open(req, timeout=TIMEOUT) as r:
            body = r.read(4_000_000)
            return r.status, body, round((time.perf_counter() - started) * 1000)
    except urllib.error.HTTPError as e:
        return e.code, b"", round((time.perf_counter() - started) * 1000)


def check(provider, base_url=None, model=None, key=None):
    """One read-only connection test. Returns a dict with status in: ok, model_missing, auth, rate_limited,
    not_found, redirect, timeout, unreachable, malformed, invalid. Never contains the key."""
    try:
        base = validate(provider, base_url)
    except ModelError as e:
        return {"status": "invalid", "detail": scrub(e, key)}
    if PROVIDERS[provider]["key"] and not key:
        return {"status": "auth", "detail": "an API key is required for this provider"}
    if model is not None and model and not MODEL_ID.match(model):
        return {"status": "invalid", "detail": "the model id has unexpected characters"}
    try:
        if provider == "ollama":
            code, body, ms = _get(base + "/api/tags")
            if code != 200:
                return {"status": "unreachable", "detail": f"Ollama answered HTTP {code}", "ms": ms}
            installed = [m.get("name") for m in json.loads(body).get("models", [])]
            code2, body2, _ = _get(base + "/api/ps")
            loaded = [{"name": m.get("name"), "context_length": m.get("context_length")}
                      for m in (json.loads(body2).get("models", []) if code2 == 200 else [])]
            res = {"status": "ok", "ms": ms, "installed": installed, "loaded": loaded}
            if model and model not in installed and f"{model}:latest" not in installed:
                res.update(status="model_missing", detail=f"{model} is not installed in Ollama")
            elif model:
                res["detail"] = f"{model} is installed" + (" and loaded" if any(l["name"] == model for l in loaded)
                                                          else " (not loaded right now; Ollama loads it on first use)")
            return res
        code, body, ms = _get(base + "/models", key)
    except (TimeoutError, socket.timeout):
        return {"status": "timeout", "detail": f"no answer in {TIMEOUT}s"}
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), (TimeoutError, socket.timeout)):
            return {"status": "timeout", "detail": f"no answer in {TIMEOUT}s"}
        return {"status": "unreachable", "detail": scrub(f"cannot connect: {e.reason}", key)}
    except (ValueError, OSError) as e:
        return {"status": "unreachable", "detail": scrub(f"{type(e).__name__}", key)}
    if code in (301, 302, 303, 307, 308):
        return {"status": "redirect", "detail": "the endpoint redirected; redirects are not followed", "ms": ms}
    if code in (401, 403):
        return {"status": "auth", "detail": "the key was refused (invalid, revoked or expired)", "ms": ms}
    if code == 429:
        return {"status": "rate_limited", "detail": "rate limited by the provider; try again later", "ms": ms}
    if code == 404:
        return {"status": "not_found", "detail": "no /models endpoint at this base URL", "ms": ms}
    if code != 200:
        return {"status": "unreachable", "detail": f"HTTP {code}", "ms": ms}
    try:
        data = json.loads(body)
        ids = sorted({m.get("id") for m in data.get("data", []) if isinstance(m, dict) and m.get("id")})
    except (ValueError, AttributeError):
        return {"status": "malformed", "detail": "the model list is not valid JSON", "ms": ms}
    if not ids:
        return {"status": "malformed", "detail": "the provider returned an empty model list", "ms": ms}
    res = {"status": "ok", "ms": ms, "models": ids[:500]}
    if model and model not in ids:
        res.update(status="model_missing", detail=f"{model} is not offered by this account")
    return res


class Registry:
    """Saved, non-secret model configurations (models.json) plus their keys in the secrets file."""

    def __init__(self, path, secrets_file):
        self.path, self.secrets_file = Path(path), Path(secrets_file)
        self.lock = threading.Lock()
        self.plans = {}

    def _load(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"configs": {}}

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def list(self):
        return self._load()["configs"]

    @staticmethod
    def key_env(name):
        return "FED_MODEL_KEY_" + name.upper().replace("-", "_")

    def _write_secret(self, var, value):
        """Upsert one FED_MODEL_KEY_* line; every other line of the secrets file is kept byte for byte."""
        lines = self.secrets_file.read_text(encoding="utf-8").splitlines() if self.secrets_file.exists() else []
        kept = [ln for ln in lines if ln.split("=", 1)[0].strip() != var]
        kept.append(f"{var}={value}")
        self.secrets_file.parent.mkdir(parents=True, exist_ok=True)
        self.secrets_file.write_text("\n".join(kept) + "\n", encoding="utf-8")

    def read_secret(self, var):
        if not self.secrets_file.exists():
            return None
        for ln in self.secrets_file.read_text(encoding="utf-8").splitlines():
            k, _, v = ln.partition("=")
            if k.strip() == var:
                return v.strip().strip("'\"") or None
        return None

    def save(self, name, provider, base_url, model, key=None, test=None):
        if not NAME.match(name or ""):
            raise ModelError("name: 2-31 lowercase letters, digits or dashes")
        if not MODEL_ID.match(model or ""):
            raise ModelError("model id required")
        base = validate(provider, base_url)
        with self.lock:
            data = self._load()
            prev = data["configs"].get(name, {})
            entry = {"provider": provider, "base_url": base, "model": model, "updated_at": time.time(),
                     "key_env": self.key_env(name) if PROVIDERS[provider]["key"] else None,
                     "last_test": test or prev.get("last_test")}
            if PROVIDERS[provider]["key"]:
                if key:
                    self._write_secret(entry["key_env"], key)
                elif not self.read_secret(entry["key_env"]):
                    raise ModelError("this provider needs an API key")
            data["configs"][name] = entry
            self._save(data)
        return self.public(name)

    def public(self, name):
        c = self.list().get(name)
        if c is None:
            raise ModelError(f"no saved configuration '{name}'")
        return {**c, "name": name, "has_key": bool(c["key_env"] and self.read_secret(c["key_env"]))}

    def retest(self, name):
        c = self.list()[name]
        res = check(c["provider"], c["base_url"], c["model"], self.read_secret(c["key_env"]) if c["key_env"] else None)
        with self.lock:
            data = self._load()
            data["configs"][name]["last_test"] = {"status": res["status"], "at": time.time()}
            self._save(data)
        return res

    # -- applying to agents (two steps)
    def plan(self, name, purpose, company, profiles):
        """Preview: which agents would change, from what to what. Returns a short-lived confirmation id."""
        c = self.public(name)
        if (c.get("last_test") or {}).get("status") != "ok":
            raise ModelError("test this configuration successfully before applying it")
        if purpose not in PURPOSES:
            raise ModelError(f"purpose must be one of {', '.join(PURPOSES)}")
        targets = []
        for p in profiles:
            co = "federation" if p["id"].startswith("fed-") else p["id"].split("-", 1)[0]
            role = p["id"].split("-", 1)[1]
            if purpose == "federation" and co != "federation":
                continue
            if purpose != "federation" and (co == "federation" or role not in PURPOSES[purpose]):
                continue
            if company and co != company:
                continue
            targets.append({"agent": p["id"], "from": f"{p.get('provider')}/{p.get('model')}",
                            "to": f"fed-{name}/{c['model']}"})
        if not targets:
            raise ModelError("no agent matches this purpose and company")
        pid = secrets.token_hex(6)
        self.plans[pid] = {"name": name, "agents": [t["agent"] for t in targets], "expires": time.time() + 300,
                           "digest": hashlib.sha256(json.dumps(c, sort_keys=True, default=str).encode()).hexdigest()}
        return {"plan_id": pid, "config": name, "changes": targets, "expires_in": 300}

    def take_plan(self, plan_id):
        p = self.plans.pop(plan_id or "", None)
        if not p or p["expires"] < time.time():
            raise ModelError("unknown or expired plan; preview again")
        c = self.public(p["name"])
        if hashlib.sha256(json.dumps(c, sort_keys=True, default=str).encode()).hexdigest() != p["digest"]:
            raise ModelError("the configuration changed since the preview; preview again")
        return p, c


def apply_to_profile(profiles_dir, agent, name, cfg, key):
    """Point one agent at a saved configuration: add provider 'fed-<name>' and select it. Backs up config.yaml
    once (config.yaml.bak-models); writes the key only into this agent's own .env."""
    path = Path(profiles_dir) / agent / "config.yaml"
    text = path.read_text(encoding="utf-8")
    bak = path.with_name("config.yaml.bak-models")
    if not bak.exists():
        bak.write_text(text, encoding="utf-8")
    pkey = f"fed-{name}"
    api = cfg["base_url"] + ("/v1" if cfg["provider"] == "ollama" and not cfg["base_url"].endswith("/v1") else "")
    block = [f"  {pkey}:", f"    api: {api}", "    transport: openai_chat"]
    if cfg.get("key_env"):
        block.insert(2, f"    key_env: {cfg['key_env']}")
    block += ["    models:", f"      {json.dumps(cfg['model'])}: {{}}"]
    text = re.sub(rf"^  {re.escape(pkey)}:\n(?:    .*\n)*", "", text, flags=re.M)
    if re.search(r"^providers:\n", text, re.M):
        text = re.sub(r"^providers:\n", "providers:\n" + "\n".join(block) + "\n", text, count=1, flags=re.M)
    else:
        text += "providers:\n" + "\n".join(block) + "\n"
    m = re.search(r"^model:\n((?:[ \t].*\n|\n)*)", text, re.M)
    if not m:
        raise ModelError(f"{agent}: no model block")
    mb = re.sub(r'^(\s+default:\s*).*$', rf'\g<1>{json.dumps(cfg["model"])}', m.group(1), count=1, flags=re.M)
    mb = re.sub(r'^(\s+provider:\s*).*$', rf'\g<1>"{pkey}"', mb, count=1, flags=re.M)
    mb = re.sub(r"^\s+ollama_num_ctx:.*\n", "", mb, flags=re.M)
    if cfg["provider"] == "ollama":
        mb = re.sub(r'^(\s+)(provider:.*\n)', r'\g<1>\g<2>\g<1>ollama_num_ctx: 65536\n', mb, count=1, flags=re.M)
    path.write_text(text[:m.start(1)] + mb + text[m.end(1):], encoding="utf-8")
    if cfg.get("key_env") and key:
        env = Path(profiles_dir) / agent / ".env"
        lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
        lines = [ln for ln in lines if ln.split("=", 1)[0].strip() != cfg["key_env"]] + [f"{cfg['key_env']}={key}"]
        env.write_text("\n".join(lines) + "\n", encoding="utf-8")
