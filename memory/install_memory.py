"""Connect every federation agent to the federation memory MCP server.

Adds an `mcp_servers.federation-memory` entry to each profile's config.yaml with that profile's
fixed identity (--agent). Idempotent; keeps one config.yaml.bak-memory per profile.

    python -I install_memory.py            # all 118 federation profiles
    python -I install_memory.py --remove   # take the entry out again
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from fedmem import OWNER, default_db, default_roster, load_org  # noqa: E402

HOME = Path(os.environ.get("HERMES_HOME") or (
    Path(os.environ["LOCALAPPDATA"]) / "hermes" if os.name == "nt" else Path.home() / ".hermes"))
NAME = "federation-memory"


def entry(profile, python, db, roster):
    q = lambda s: json.dumps(str(s).replace("\\", "/"))  # noqa: E731  JSON strings are valid YAML
    args = ["-I", HERE / "memory_mcp.py", "--agent", profile, "--db", db, "--roster", roster,
            "--workflow-db", Path(db).parent / "workflow.db"]
    lines = [f"  {NAME}:", f"    command: {q(python)}", "    args:"]
    lines += [f"      - {q(a)}" for a in args]
    lines += ["    env:", '      FED_CUSTOMER: "${FED_CUSTOMER}"', "    enabled: true"]
    return lines


def strip_entry(lines):
    """Remove an existing federation-memory block under mcp_servers."""
    out, skip = [], False
    for line in lines:
        if line.startswith(f"  {NAME}:"):
            skip = True
            continue
        if skip and (line.startswith("    ") or not line.strip()):
            continue
        skip = False
        out.append(line)
    return out


def disable_native_memory(lines):
    """Turn off Hermes' built-in `memory` toolset: it keeps one notes file per agent across every
    conversation, so a receptionist would carry one customer's details into the next customer's chat.
    The federation memory's agent scope replaces it, with audit and customer binding."""
    agent = next((i for i, line in enumerate(lines) if line.rstrip() == "agent:"), None)
    if agent is None:
        return lines + ["agent:", "  disabled_toolsets:", "    - memory"]
    end = next((j for j in range(agent + 1, len(lines)) if lines[j][:1] not in (" ", "\t", "#", "")), len(lines))
    ds = next((j for j in range(agent + 1, end) if lines[j].startswith("  disabled_toolsets:")), None)
    if ds is None:
        return lines[:agent + 1] + ["  disabled_toolsets:", "    - memory"] + lines[agent + 1:]
    if lines[ds].strip() == "disabled_toolsets: []":
        lines[ds] = "  disabled_toolsets:"
    items = []
    j = ds + 1
    while j < end and lines[j].startswith("    - "):
        items.append(lines[j].strip()[2:])
        j += 1
    if "memory" not in items:
        lines.insert(ds + 1, "    - memory")
    return lines


def eager_tools(lines):
    """Tool Search hides every MCP tool behind tool_search/tool_call; small local models then reach for
    the built-in tools instead. Keep the memory tools in the model's tool list directly."""
    if any(line.rstrip() == "tools:" for line in lines):
        return lines
    return lines + ["tools:", "  tool_search:", '    enabled: "off"']


def install(path, block, remove=False):
    text = path.read_text(encoding="utf-8")
    bak = path.with_name("config.yaml.bak-memory")
    if not bak.exists():
        bak.write_text(text, encoding="utf-8")
    lines = strip_entry(text.splitlines())
    if not remove:
        top = [i for i, line in enumerate(lines) if line.rstrip() == "mcp_servers:"]
        if top:
            lines[top[0] + 1:top[0] + 1] = block
        else:
            lines += ["mcp_servers:"] + block
        lines = eager_tools(disable_native_memory(lines))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    remove = "--remove" in sys.argv
    org = load_org()
    db, roster = default_db(), default_roster()
    done = missing = 0
    for profile in sorted(p for p in org if p != OWNER):
        cfg = HOME / "profiles" / profile / "config.yaml"
        if not cfg.exists():
            print(f"missing  {profile}")
            missing += 1
            continue
        install(cfg, entry(profile, sys.executable, db, roster), remove)
        done += 1
    print(f"{'removed from' if remove else 'installed in'} {done} profiles, {missing} missing; db {db}")


if __name__ == "__main__":
    main()
