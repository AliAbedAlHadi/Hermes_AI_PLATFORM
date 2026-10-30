"""Stage 2 test (MIGRATION_PLAN.md): one real agent, isc-executor, copied into the new layout and run as hermes-isc.
Run as the owner, NOT elevated, after 2_setup_identities.ps1:

    python -I canary.py [--no-chat]

Changes nothing in the live federation: the profile is COPIED into C:\\HermesFederation\\homes\\isc, and its guard
and memory tool point at a copy of the databases in homes\\isc\\canary (stage 3 replaces both). Everything below the
"OS boundary" line runs AS hermes-isc, so the results are what Windows enforces for the company identity.
"""
import argparse
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "console"))
from agent_runner import AgentRunner  # noqa: E402
from fedlayout import GROUPS, Layout, identity  # noqa: E402
from migrate import CRED_DIR, LIVE, copy_db, install_profile  # noqa: E402

AGENT, GROUP = "isc-executor", "isc"
fails = 0


def show(name, ok, info=""):
    global fails
    fails += ok is False
    print(f"{'PASS' if ok else 'INFO' if ok is None else 'FAIL'}  {name}" + (f"  -- {info}" if info else ""), flush=True)


def detail(rc, out, err, n=300):
    return f"rc {rc} " + ((out or "") + (err or "")).strip()[-n:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-chat", action="store_true")
    a = ap.parse_args()
    base = Layout()
    print("== stage 2 setup present")
    for g in GROUPS:
        show(f"credential for {identity(g)}", (CRED_DIR / f"{identity(g)}.xml").exists())
        show(f"Hermes dependencies in homes\\{g}",
             bool(list((base.home(g) / "installs").glob("*/environments/*/venv/pyvenv.cfg"))))
    show("federation code copied to fed", (base.fed / "security" / "fed_guard.py").exists()
         and (base.fed / "memory" / "memory_mcp.py").exists())
    if fails:
        print("\nstage 2 setup incomplete - run 2_setup_identities.ps1 (elevated) first")
        return 1

    print(f"== canary copy of {AGENT} (live profile untouched)")
    canary = base.home(GROUP) / "canary"
    if canary.exists():
        shutil.rmtree(canary)
    canary.mkdir(parents=True)
    for n in ("memory.db", "workflow.db"):
        copy_db(LIVE / "federation" / n, canary / n)
    lay = Layout(shared=canary)
    dst = install_profile(AGENT, lay)
    show("profile copied, rewritten and its control files locked", (dst / "config.yaml").exists())
    cfg = (dst / "config.yaml").read_text(encoding="utf-8")
    show("no path into the owner's profile left in its guard or memory tool",
         str(Path.home()).replace("\\", "/").lower() not in cfg.lower().split("federation-memory:")[1][:1200]
         and "aliab" not in lay.guard_command(AGENT).lower())

    runner = AgentRunner(base.root, hermes_home=base.home(GROUP), cred=CRED_DIR / f"{identity(GROUP)}.xml",
                         runs=base.runs(GROUP))
    other = "auvia"
    spec = {
        "agent": identity(GROUP),
        "deny_read": [str(LIVE / "profiles" / AGENT / "config.yaml"), str(LIVE / "federation" / "memory.db"),
                      str(LIVE / "auth.json")],
        "deny_list": [str(base.home(other)), str(base.workspaces / other), str(base.root / "runs" / identity(other)),
                      str(base.home("federation")), str(LIVE / "profiles"), str(Path.home()), str(base.root / "home-pilot")],
        "deny_update": [str(dst / f) for f in ("config.yaml", "shell-hooks-allowlist.json", "SOUL.md", ".env")
                        if (dst / f).exists()]
                       + [str(base.fed / "security" / "fed_guard.py"), str(base.root / "runtime" / "agent_exec.py")],
        "deny_create": [str(base.fed / "security"), str(base.root / "runtime"), str(base.home(GROUP) / "installs"),
                        str(base.root / "homes")],
        "allow_read": [str(dst / "config.yaml")],
        "allow_create": [str(base.workspaces / GROUP), str(dst / "sessions") if (dst / "sessions").is_dir() else str(dst)],
        "junction_target": str(LIVE),
        "loopback": [["127.0.0.1", 11434, "Ollama"]],
    }
    print(f"== OS boundary, probed as {identity(GROUP)}")
    for r in runner.probe(spec):
        show(r["name"], r["ok"], "" if r["ok"] else f"expected {r['expect']}, got {r['got']}" + (f" ({r['note']})" if r["note"] else ""))

    print(f"== {AGENT} runs as {identity(GROUP)} from homes\\{GROUP}")
    ws = base.workspaces / GROUP
    rc, out, err = runner.hermes("-p", AGENT, "hooks", "list", timeout=180, cwd=ws)
    show("its guard hook is registered and approved (new command)",
         rc == 0 and "C:/HermesFederation/fed/security/fed_guard.py" in out and "allowed" in out, detail(rc, out, err))
    if not a.no_chat:
        rc, out, err = runner.hermes("-p", AGENT, "-z", "Reply with the single word READY.", timeout=900, cwd=ws)
        show(f"one chat turn as {identity(GROUP)} (local Ollama)", rc == 0 and out.strip() != "", detail(rc, out, err, 200))
    print(f"\n{'all checks passed' if not fails else f'{fails} check(s) failed'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
