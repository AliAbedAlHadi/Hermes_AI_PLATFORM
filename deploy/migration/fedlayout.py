"""Per-company layout of the federation (see MIGRATION_PLAN.md): paths, identities and the profile rewrite.

Used by canary.py, migrate.py and the tests. Pure functions over text; the callers do the copying.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(r"C:\HermesFederation")
COMPANIES = ("isc", "auvia", "tabbeqai", "spectra", "spectrasolutions")
GROUPS = COMPANIES + ("federation",)
OURS = re.compile(r"^(fed|isc|auvia|tabbeqai|spectra|spectrasolutions)-[a-z-]+$")


def group_of(profile):
    return "federation" if profile.startswith("fed-") else profile.split("-", 1)[0]


# Windows account names are limited to 20 characters ("hermes-spectrasolutions" has 23)
IDENTITIES = {"isc": "hermes-isc", "auvia": "hermes-auvia", "tabbeqai": "hermes-tabbeqai", "spectra": "hermes-spectra",
              "spectrasolutions": "hermes-spectrasol", "federation": "hermes-fed"}


def identity(group):
    return IDENTITIES[group]


class Layout:
    def __init__(self, root=ROOT, python=None, shared=None):
        self.root = Path(root)
        self.fed = self.root / "fed"
        self.shared = Path(shared) if shared else self.root / "shared"   # canary: a copy inside the company home
        self.workspaces = self.root / "workspaces"
        # Routing of the Console (which groups run as their identity). In homes\, writable by the owner only: no
        # identity can mark its group unmigrated and so be run as the owner again.
        self.layout_file = self.root / "homes" / "layout.json"
        self._python = Path(python) if python else None

    def home(self, group):
        return self.root / "homes" / group

    def runs(self, group):
        return self.root / "runs" / identity(group)

    @property
    def python(self):
        """The runtime's own Python (admin-owned, readable by every identity)."""
        if self._python is None:
            found = sorted(p for p in (self.root / "runtime" / "tools").glob("python-*/python.exe"))
            if not found:
                raise FileNotFoundError(f"no runtime python under {self.root / 'runtime' / 'tools'}")
            self._python = found[0]
        return self._python

    def guard_command(self, agent):
        g = group_of(agent)
        return (f"{fwd(self.python)} -I {fwd(self.fed / 'security' / 'fed_guard.py')} --agent {agent} "
                f"--home {fwd(self.home(g))} --workspaces {fwd(self.workspaces)} --db {fwd(self.shared / 'memory.db')}")

    def memory_args(self, agent):
        return ["-I", fwd(self.fed / "memory" / "memory_mcp.py"), "--agent", agent, "--db", fwd(self.shared / "memory.db"),
                "--roster", fwd(self.fed / "roster" / "roster.json"), "--workflow-db", fwd(self.shared / "workflow.db")]


def fwd(p):
    return str(p).replace("\\", "/")


class RewriteError(Exception):
    pass


def rewrite_config(text, agent, lay):
    """The profile's config.yaml for the new layout: guard and memory tool run from C:\\HermesFederation against the
    company home and the shared databases; the owner's hermes-cloud OAuth MCP is disabled. Everything else (model,
    toolsets, matcher, SOUL) is unchanged. Raises RewriteError when the text is not shaped as expected."""
    out = text
    # guard hook: exactly one command line naming fed_guard.py for this agent
    hook = re.compile(r'(?m)^(      command: )"([^"\n]*fed_guard\.py --agent ' + re.escape(agent) + r' [^"\n]*)"$')
    if len(hook.findall(out)) != 1:
        raise RewriteError(f"{agent}: expected one guard command")
    out = hook.sub(lambda m: f'{m.group(1)}"{lay.guard_command(agent)}"', out)
    # memory MCP: command + args list
    mem = re.compile(r'(?ms)^(  federation-memory:\n    command: )"[^"\n]*"\n    args:\n((?:      - "[^"\n]*"\n)+)')
    m = mem.search(out)
    if not m or f'"{agent}"' not in m.group(2):
        raise RewriteError(f"{agent}: expected the federation-memory MCP for this agent")
    args = "".join(f'      - "{a}"\n' for a in lay.memory_args(agent))
    out = out[:m.start()] + f'{m.group(1)}"{fwd(lay.python)}"\n    args:\n{args}' + out[m.end():]
    # hermes-cloud MCP: its OAuth token belongs to the owner's home; off for migrated agents
    cloud = re.compile(r"(?ms)^(  hermes-cloud:\n(?:    .*\n)*?    enabled: )true$")
    out = cloud.sub(r"\1false", out)
    return out


def allowlist(command):
    """shell-hooks-allowlist.json approving exactly the new guard command (same shape Hermes writes)."""
    return json.dumps({"approvals": [{"event": "pre_tool_call", "command": command, "script_mtime_at_approval": None,
                                      "approved_at": datetime.now(timezone.utc).isoformat()}]}, indent=2) + "\n"


def read_layout(lay):
    try:
        return json.loads(lay.layout_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "groups": {}}
