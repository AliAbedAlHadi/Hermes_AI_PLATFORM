"""Apply federation settings to the Hermes profiles and boards created by create_profiles.py.

(a) models per tier       -- applied only when the provider credential exists (R08.06: exact ID from the account)
(b) persistent workspaces -- one git repo per board, so every task gets a preserved worktree (R11.06)
(c) tool restrictions     -- agent.disabled_toolsets per role (enforced by Hermes, R04.02)

Idempotent. Run:  python apply_settings.py [--dry-run]
"""
import json
import os
import subprocess
import sys
from pathlib import Path

DRY = "--dry-run" in sys.argv
WORK_ROOT = Path(os.environ["USERPROFILE"]) / "hermes-federation" / "workspaces"  # outside OneDrive on purpose
BOARDS = ["federation", "isc", "auvia", "tabbeqai", "spectra", "spectrasolutions"]
PREFIXES = ["isc", "auvia", "tabbeqai", "spectra", "spectrasolutions"]

# (c) Toolsets each role must not have. Terminal stays for reviewers so they can run tests;
# terminal can still write files, so full read-only needs OS-level sandboxing later (R10.03).
READ_ONLY = ["file", "computer_use", "browser", "image_gen", "cronjob", "delegation"]
FRONT_DESK = ["terminal", "file", "code_execution", "computer_use", "browser", "image_gen", "cronjob", "delegation"]
POLICY = {
    "reviewer": READ_ONLY,
    "verifier": READ_ONLY,
    "receptionist": FRONT_DESK,
}
FED_POLICY = {"audit": READ_ONLY}

# (a) Owner direction 2026-10-20: DeepSeek for every tier, pending Dr. Hassan's confirmation.
# The model ID is confirmed from the account after the key is added (R08.06), then set here.
MODEL = {"provider": "deepseek", "default": None}  # e.g. "deepseek-v4.1-flash" once confirmed


def sh(args):
    if DRY:
        print("DRY:", " ".join(args))
        return 0, ""
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, r.stdout + r.stderr


def profiles():
    names = [f"fed-{k}" for k in ("hermes-admin", "coo", "modelops", "finops", "infra-recovery",
                                  "knowledge", "security-policy", "audit")]
    roster = json.loads((Path(__file__).parent / "roster.json").read_text(encoding="utf-8"))
    for p in PREFIXES:
        names += [f"{p}-{t['role']}" for t in roster["role_templates"]]
    return names


def apply_workspaces():
    for board in BOARDS:
        repo = WORK_ROOT / board
        if not DRY:
            repo.mkdir(parents=True, exist_ok=True)
            if not (repo / ".git").exists():
                (repo / "README.md").write_text(f"# {board} workspace\n\nTask worktrees for the `{board}` Kanban board.\n",
                                                encoding="utf-8")
                for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "README.md"],
                            ["git", "-c", "user.name=Hermes Federation", "-c", "user.email=federation@localhost",
                             "commit", "-q", "-m", f"Initialize {board} workspace"]):
                    subprocess.run(cmd, cwd=repo, check=True)
        rc, _ = sh(["hermes", "kanban", "boards", "set-default-workdir", board, str(repo)])
        print(f"{'ok' if rc == 0 else 'FAILED':7} workspace {board} -> {repo}")


def apply_tools():
    for name in profiles():
        role = name.split("-", 1)[1]
        deny = FED_POLICY.get(role) if name.startswith("fed-") else POLICY.get(role)
        if not deny:
            continue
        rc, _ = sh(["hermes", "-p", name, "config", "set", "agent.disabled_toolsets", json.dumps(deny)])
        print(f"{'ok' if rc == 0 else 'FAILED':7} tools {name}: disabled {', '.join(deny)}")


def apply_models():
    if not MODEL["default"]:
        print("SKIP    models: DeepSeek model ID not confirmed yet (add the key, then set MODEL['default'])")
        return
    for name in profiles():
        for key in ("provider", "default"):
            rc, _ = sh(["hermes", "-p", name, "config", "set", f"model.{key}", MODEL[key]])
            if rc:
                print(f"FAILED  model.{key} {name}")
    print(f"ok      models: {MODEL['provider']}/{MODEL['default']} on {len(profiles())} profiles")


# Interim local setup (owner request 2026-10-20) until the DeepSeek key exists: everything on Ollama.
# Tool-calling probe (R06.03), 2026-10-20: only qwen3.5:9b makes real tool calls through Hermes.
# qwen2.5-coder:7b prints tool calls as text; llama3 is rejected by Ollama for tools. So one model for all.
LOCAL_MODEL = "qwen3.5:9b"


def apply_local_models():
    for name in profiles():
        model = LOCAL_MODEL
        ok = all(sh(["hermes", "-p", name, "config", "set", f"model.{k}", v])[0] == 0
                 for k, v in (("provider", "local-ollama"), ("default", model)))
        print(f"{'ok' if ok else 'FAILED':7} model {name}: local-ollama/{model}", flush=True)


if __name__ == "__main__":
    if "--local" in sys.argv:
        apply_local_models()
        sys.exit(0)
    apply_workspaces()
    apply_tools()
    apply_models()
