"""Stage 1 of the per-company identity migration (G2-2 rollout): READ-ONLY inventory of the live federation.

    python -I deploy/migration/inventory.py [--out inventory.json] [--sizes]

Reads the live Hermes home (%LOCALAPPDATA%\\hermes), the federation workspaces and the profile configs; opens no
database, never reads a .env or any secret (only whether the file exists), writes only the --out file. Every check
below must PASS before stage 2: it is the evidence that the migration plan (MIGRATION_PLAN.md) covers every agent and
every path the agents depend on.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME") or Path(os.environ["LOCALAPPDATA"]) / "hermes")
FED = Path.home() / "OneDrive" / "Documents" / "Hermes"
WORKSPACES = Path.home() / "hermes-federation" / "workspaces"
COMPANIES = ("isc", "auvia", "tabbeqai", "spectra", "spectrasolutions")
GROUPS = COMPANIES + ("federation",)
OURS = re.compile(r"^(fed|isc|auvia|tabbeqai|spectra|spectrasolutions)-[a-z-]+$")
# Per-company Windows identity of each group (stage 2 creates them; hermes-agent stays the pilot account)
IDENTITY = {g: ("hermes-spectrasol" if g == "spectrasolutions" else f"hermes-{'fed' if g == 'federation' else g}")
            for g in GROUPS}  # account names: at most 20 characters
SHARED_DBS = ("memory.db", "workflow.db")          # agents reach these (memory MCP, guard audit)
OWNER_ONLY = ("owner.json", "sessions.db", "conversations.db", "models.json")  # Console only, never an agent


def group_of(profile):
    return "federation" if profile.startswith("fed-") else profile.split("-", 1)[0]


def dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.stat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def block(text, key, width=3000):
    m = re.search(r"(?m)^" + re.escape(key) + r":\s*$", text)
    return text[m.end():m.end() + width] if m else ""


def profile_info(p):
    cfg = (p / "config.yaml").read_text(encoding="utf-8", errors="replace")
    hooks = block(cfg, "hooks")
    guard = re.search(r'command: "([^"]*fed_guard\.py[^"]*)"', hooks)
    args = dict(re.findall(r"--(agent|home|workspaces|db) (\S+)", guard.group(1))) if guard else {}
    mcp = block(cfg, "mcp_servers", 1500)
    mem = re.search(r"federation-memory:(.*?)(?:\n  \S|\Z)", mcp, re.S)
    mem_args = re.findall(r'^\s+- "([^"]*)"', mem.group(1), re.M) if mem else []
    model = block(cfg, "model", 4000)
    prov = re.search(r'(?m)^  provider: "?([^"\n]+)"?', model)
    return {
        "profile": p.name, "group": group_of(p.name), "identity": IDENTITY[group_of(p.name)],
        "guard": bool(guard), "guard_args": args, "guard_python": guard.group(1).split(" -I ")[0] if guard else None,
        "fail_closed": "fail_closed: true" in hooks,
        "memory_mcp": bool(mem), "memory_args": mem_args,
        "hermes_cloud_mcp": "hermes-cloud:" in mcp,
        "provider": prov.group(1).strip() if prov else None,
        "has_env": (p / ".env").exists(),
        "files": sorted(x.name for x in p.iterdir()),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--sizes", action="store_true", help="also measure folder sizes (slow: minutes)")
    a = ap.parse_args()

    fails = []

    def check(name, ok, info=""):
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {info}" if info and not ok else ""))
        if not ok:
            fails.append(name)

    profiles = sorted((p for p in (HOME / "profiles").iterdir() if p.is_dir() and (p / "config.yaml").exists()),
                      key=lambda p: p.name)
    ours = [profile_info(p) for p in profiles if OURS.match(p.name)]
    others = [p.name for p in profiles if not OURS.match(p.name)]
    by_group = {g: [x["profile"] for x in ours if x["group"] == g] for g in GROUPS}

    inv = {"home": str(HOME), "fed_code": str(FED), "workspaces": str(WORKSPACES), "identities": IDENTITY,
           "groups": by_group, "others_untouched": others, "agents": ours,
           "boards": {b: str(HOME / "kanban" / "boards" / b / "kanban.db") for b in GROUPS},
           "federation_dbs": {f.name: f.stat().st_size for f in (HOME / "federation").iterdir() if f.is_file()},
           "root_state": sorted(f.name for f in HOME.iterdir() if f.suffix == ".db"),
           "external_paths": sorted({x["guard_python"] for x in ours if x["guard_python"]}
                                    | {m for x in ours for m in x["memory_args"] if m.endswith(".py")}
                                    | {x["guard_args"]["agent"] and str(FED / "security" / "fed_guard.py").replace("\\", "/")
                                       for x in ours if x["guard"]})}
    if a.sizes:
        inv["sizes_mb"] = {
            **{f"profiles/{g}": round(sum(dir_size(HOME / "profiles" / n) for n in names) / 2**20, 1)
               for g, names in by_group.items()},
            **{f"workspaces/{g}": round(dir_size(WORKSPACES / g) / 2**20, 1) for g in GROUPS},
            **{f"boards/{g}": round(dir_size(HOME / "kanban" / "boards" / g) / 2**20, 1) for g in GROUPS}}

    print("== agents")
    check("118 federation agents found", len(ours) == 118, len(ours))
    for g in COMPANIES:
        check(f"company {g}: 22 agents", len(by_group[g]) == 22, len(by_group[g]))
    check("federation: 8 agents", len(by_group["federation"]) == 8, len(by_group["federation"]))
    check(f"other profiles left out of the migration: {', '.join(others) or 'none'}", True)
    check("every agent has the guard hook, fail-closed", all(x["guard"] and x["fail_closed"] for x in ours),
          [x["profile"] for x in ours if not (x["guard"] and x["fail_closed"])][:5])
    check("every guard runs for its own agent name", all(x["guard_args"].get("agent") == x["profile"] for x in ours))
    check("every agent has the federation memory tool", all(x["memory_mcp"] for x in ours),
          [x["profile"] for x in ours if not x["memory_mcp"]][:5])
    check("every agent has its own .env (model keys; content not read)", all(x["has_env"] for x in ours),
          [x["profile"] for x in ours if not x["has_env"]][:5])

    print("== paths the agents depend on (all must exist; the plan rewrites each)")
    homes = {x["guard_args"].get("home") for x in ours}
    wss = {x["guard_args"].get("workspaces") for x in ours}
    dbs = {x["guard_args"].get("db") for x in ours} | {x["memory_args"][x["memory_args"].index("--db") + 1]
                                                       for x in ours if "--db" in x["memory_args"]}
    check("one Hermes home for all agents (today)", len(homes) == 1 and Path(next(iter(homes))) == HOME, homes)
    check("one workspace root for all agents (today)", len(wss) == 1 and Path(next(iter(wss))) == WORKSPACES, wss)
    check("one shared memory.db (guard audit + memory tool)", len(dbs) == 1 and Path(next(iter(dbs))).exists(), dbs)
    for p in inv["external_paths"]:
        check(f"external dependency exists: {p}", Path(p).exists())
    for g in GROUPS:
        check(f"board {g} exists", Path(inv["boards"][g]).exists())
        check(f"workspace {g} exists", (WORKSPACES / g).is_dir())
    for f in SHARED_DBS + OWNER_ONLY:
        check(f"federation/{f} present", f in inv["federation_dbs"])

    print("== what the per-company identities change (facts for the plan)")
    owner_py = [p for p in inv["external_paths"] if str(Path.home()).replace("\\", "/").lower() in p.replace("\\", "/").lower()]
    check(f"tools run from the owner's profile today ({len(owner_py)} paths) - stage 2 moves them to C:\\HermesFederation",
          True)
    cloud = [x["profile"] for x in ours if x["hermes_cloud_mcp"]]
    check(f"agents with the hermes-cloud MCP (OAuth token stays with the owner; disabled after migration): {len(cloud)}",
          True)

    if a.out:
        Path(a.out).write_text(json.dumps(inv, indent=1), encoding="utf-8")
        print(f"\ninventory written to {a.out}")
    print(f"\n{'all checks passed' if not fails else f'{len(fails)} check(s) failed'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
