"""Stage 3-4 test (MIGRATION_PLAN.md): after `migrate.py --group G`, prove the group runs as its own identity.
Run as the owner, NOT elevated (Ollama running for the chat turn):

    python -I verify_group.py isc [--agent isc-executor] [--no-chat]

1. every migrated profile: guard from C:\\HermesFederation, its approval matches, memory tool on the shared databases,
   hermes-cloud off, nothing pointing into the owner's profile;
2. AS the identity (OS boundary): other groups' homes, workspaces and launcher runs, the live home, the owner's profile
   and the routing file are refused; its own workspace works; its control files are read-only;
3. one turn through the Console's own routing (console/routing.py): the guard is registered and the agent answers.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "console"))
import routing  # noqa: E402
from fedlayout import GROUPS, Layout, fwd, identity, read_layout  # noqa: E402
from migrate import CRED_DIR, LIVE, LIVE_WS  # noqa: E402

fails = 0


def show(name, ok, info=""):
    global fails
    fails += ok is False
    print(f"{'PASS' if ok else 'INFO' if ok is None else 'FAIL'}  {name}" + (f"  -- {info}" if info else ""), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("group", choices=GROUPS)
    ap.add_argument("--agent")
    ap.add_argument("--no-chat", action="store_true")
    a = ap.parse_args()
    g, lay = a.group, Layout()
    info = read_layout(lay)["groups"].get(g, {})
    if not info.get("migrated"):
        print(f"{g} is not migrated (migrate.py --group {g})")
        return 1
    agents = info["agents"]
    agent = a.agent or next((x for x in agents if x.endswith("-executor")), agents[0])
    owner_home = fwd(Path.home()).lower()

    print(f"== {len(agents)} profiles of {g} in {lay.home(g)}")
    bad = []
    for x in agents:
        d = lay.home(g) / "profiles" / x
        cfg = (d / "config.yaml").read_text(encoding="utf-8")
        allow = json.loads((d / "shell-hooks-allowlist.json").read_text(encoding="utf-8"))
        guard = re.search(r'command: "([^"]*fed_guard\.py[^"]*)"', cfg).group(1)
        mem = cfg.split("federation-memory:", 1)[1][:1500]
        ok = (guard == lay.guard_command(x) and [p["command"] for p in allow["approvals"]] == [guard]
              and fwd(lay.shared / "memory.db") in mem and owner_home not in guard.lower() and owner_home not in mem.lower()
              and re.search(r"(?ms)hermes-cloud:.*?enabled: false", cfg) is not None and "fail_closed: true" in cfg)
        if not ok:
            bad.append(x)
    show("guard, approval, memory tool and hermes-cloud correct in every profile", not bad, ", ".join(bad[:5]))

    route = routing.Routing(LIVE, LIVE_WS, ["hermes"], {**os.environ}, root=lay.root, cred_dir=CRED_DIR)
    runner = route.runner(g)
    others = [o for o in GROUPS if o != g]
    d = lay.home(g) / "profiles" / agent
    spec = {
        "agent": identity(g),
        "deny_read": [str(LIVE / "profiles" / agent / "config.yaml"), str(LIVE / "federation" / "owner.json"),
                      str(LIVE / "auth.json")],
        "deny_list": [str(lay.home(o)) for o in others] + [str(lay.root / "runs" / identity(o)) for o in others]
                     + ([str(lay.workspaces / o) for o in others] if g != "federation" else [])
                     + [str(LIVE / "profiles"), str(Path.home()), str(lay.root / "home-pilot")],
        "deny_update": [str(d / f) for f in ("config.yaml", "shell-hooks-allowlist.json", "SOUL.md", ".env") if (d / f).exists()]
                       + [str(lay.layout_file), str(lay.fed / "security" / "fed_guard.py"),
                          str(lay.root / "runtime" / "agent_exec.py")],
        "deny_create": [str(lay.root / "homes"), str(lay.fed / "security"), str(lay.root / "runtime"),
                        str(lay.home(g) / "installs")],
        "allow_read": [str(d / "config.yaml")],
        "allow_create": [str(lay.workspaces / g), str(lay.shared)],
        "junction_target": str(LIVE),
        "loopback": [["127.0.0.1", 11434, "Ollama"]],
    }
    print(f"== OS boundary, probed as {identity(g)}")
    for r in runner.probe(spec):
        show(r["name"], r["ok"], "" if r["ok"] else f"expected {r['expect']}, got {r['got']}" + (f" ({r['note']})" if r["note"] else ""))
    if g == "federation":
        show("hermes-fed reads company workspaces (fed-hermes-admin, fed-audit; residual R3)", None)

    print(f"== {agent} through the Console's routing")
    rc, out, err = route.run("-p", agent, "hooks", "list", timeout=180)
    show("guard registered and approved (run as the owner against the group home)",
         rc == 0 and fwd(lay.fed / "security" / "fed_guard.py") in out and "allowed" in out, f"rc {rc} " + (out + err)[-300:])
    if not a.no_chat:
        rc, out, err = route.run("-p", agent, "-z", "Reply with the single word READY.", timeout=900)
        show(f"one chat turn, run as {identity(g)}", rc == 0 and out.strip() != "", f"rc {rc} " + (out or err).strip()[-200:])
    print(f"\n{'all checks passed' if not fails else f'{fails} check(s) failed'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
