"""Model gateway health probe and failure classification (M1 item 8, recovery).

Before work is dispatched, and when a worker dies, the workflow asks: can the agent's model endpoint answer
right now? The answer is classified so recovery knows what to do:

  ok                       endpoint answered and the model is listed
  down        (transient)  connection refused / reset: the server is not running
  timeout     (transient)  no answer within the deadline
  malformed   (transient)  answered, but not with the JSON the API defines
  unavailable (transient)  HTTP 429/5xx: overloaded or restarting
  model_missing (permanent) the server is up but does not have the configured model
  auth        (permanent)  401/403: credentials are wrong; retrying cannot fix it
  config      (permanent)  the profile has no usable endpoint

Transient failures are retried with bounded exponential backoff; permanent ones block immediately with the
reason. The probe never changes the configured provider or model.
"""
import json
import os
import re
import socket
import urllib.error
import urllib.request
from pathlib import Path

TRANSIENT = {"down", "timeout", "malformed", "unavailable"}
PERMANENT = {"model_missing", "auth", "config"}
# Error text a crashed worker leaves when its model endpoint failed (Kanban run error / worker output).
GATEWAY_ERROR = re.compile(
    r"connection (?:refused|reset|error)|actively refused|failed to establish|max retries exceeded|"
    r"timed? ?out|read timeout|connecterror|apiconnectionerror|service unavailable|bad gateway|"
    r"\b50[234]\b|model .{0,40}not found|could not connect", re.I)


def endpoint_of(home, profile):
    """(provider, base_url, model, key_env) for a profile, read from its config.yaml without importing Hermes."""
    cfg_path = Path(home) / "profiles" / profile / "config.yaml"
    if not cfg_path.exists():
        return None
    cfg = cfg_path.read_text(encoding="utf-8")
    block = cfg.split("\nmodel:", 1)[-1][:3000]
    prov = re.search(r'^\s+provider:\s*"?([^"\n]+)', block, re.M)
    model = re.search(r'^\s+default:\s*"?([^"\n]+)', block, re.M)
    if not (prov and model):
        return None
    provider = prov.group(1).strip()
    key = provider.split(":", 1)[-1]
    m = re.search(rf"^  {re.escape(key)}:\n((?:    .*\n)+)", cfg.split("\nproviders:", 1)[-1], re.M)
    api = re.search(r"^\s+api:\s*\"?([^\"\n]+)", m.group(1), re.M) if m else None
    key_env = re.search(r"^\s+key_env:\s*\"?([^\"\n]+)", m.group(1), re.M) if m else None
    return provider, (api.group(1).strip() if api else ""), model.group(1).strip(), (key_env.group(1).strip() if key_env else "")


def probe(base_url, model, api_key="", timeout=5.0):
    """Ask an OpenAI-compatible endpoint for its model list. Returns (status, detail). Cheap: no inference."""
    if not base_url:
        return "config", "no endpoint configured"
    req = urllib.request.Request(base_url.rstrip("/") + "/models", headers={"User-Agent": "hermes-federation/1"})
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(2_000_000)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return "auth", f"HTTP {e.code}"
        if e.code == 404:
            return "model_missing", "model list endpoint not found (HTTP 404)"
        if e.code == 429 or e.code >= 500:
            return "unavailable", f"HTTP {e.code}"
        return "malformed", f"unexpected HTTP {e.code}"
    except (socket.timeout, TimeoutError):
        return "timeout", f"no answer in {timeout:.0f}s"
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return "timeout", f"no answer in {timeout:.0f}s"
        return "down", str(reason)[:200]
    except (ConnectionError, OSError) as e:
        return "down", str(e)[:200]
    try:
        data = json.loads(raw)
        ids = {m.get("id") or m.get("name") for m in data.get("data") or data.get("models") or []}
    except (ValueError, AttributeError, TypeError):
        return "malformed", "model list is not valid JSON"
    if not ids:
        return "malformed", "model list is empty or has an unknown shape"
    if model and model not in ids and f"{model}:latest" not in ids:
        return "model_missing", f"{model} is not served by {base_url}"
    return "ok", f"{model} available"


def probe_profile(home, profile, timeout=5.0):
    ep = endpoint_of(home, profile)
    if ep is None:
        return "config", f"no model/provider configuration for {profile}"
    provider, base_url, model, key_env = ep
    key = ""
    if key_env:  # read the key from the profile's own .env, never print it
        env = Path(home) / "profiles" / profile / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith(key_env + "="):
                    key = line.split("=", 1)[1].strip().strip("'\"")
    status, detail = probe(base_url, model, key, timeout)
    return status, f"{provider}/{model}: {detail}"


def backoff(attempt, base=30, cap=900):
    """Seconds to wait before recovery attempt `attempt` (1-based): 30, 60, 120, 240, 480, 900..."""
    return min(cap, base * 2 ** max(0, attempt - 1))


def is_gateway_error(text):
    return bool(text and GATEWAY_ERROR.search(text))
