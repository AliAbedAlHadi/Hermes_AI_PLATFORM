"""Where each federation group lives and as whom it runs (per-company identity migration, deploy/migration).

C:\\HermesFederation\\homes\\layout.json (written by migrate.py, writable by the owner only) lists the migrated groups.
A migrated group's profiles, board and workspace are in its own home, and its agents run as its Windows identity
through agent_runner; every other group stays in the live home and runs as the owner, exactly as before. Without a
layout file nothing changes.
"""
import json
import os
import re
import subprocess
import threading
from pathlib import Path

from agent_runner import ROOT, AgentRunner

OURS = re.compile(r"^(fed|isc|auvia|tabbeqai|spectra|spectrasolutions)-[a-z-]+$")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
CRED_DIR = Path.home() / "AppData" / "Local" / "hermes-federation-launcher"
PASS_ENV = ("FED_CUSTOMER",)  # the only Console variables an agent run may receive


def group_of(key):
    """Group of a profile name or a board name."""
    return "federation" if key == "federation" or key.startswith("fed-") else key.split("-", 1)[0]


# Windows account names are limited to 20 characters ("hermes-spectrasolutions" has 23)
IDENTITIES = {"isc": "hermes-isc", "auvia": "hermes-auvia", "tabbeqai": "hermes-tabbeqai", "spectra": "hermes-spectra",
              "spectrasolutions": "hermes-spectrasol", "federation": "hermes-fed"}


def identity(group):
    return IDENTITIES[group]


def agent_run(args):
    """True for the commands that execute agents: a chat turn or a board dispatch (these run as the identity)."""
    a = list(args)
    return (len(a) == 4 and a[0] == "-p" and a[2] == "-z") or (len(a) == 4 and a[:2] == ["kanban", "--board"] and a[3] == "dispatch")


class Routing:
    def __init__(self, live_home, live_workspaces, hermes_cmd, env, root=ROOT, cred_dir=CRED_DIR, runner_factory=AgentRunner,
                 migrated_cmd=None):
        self.live_home, self.live_ws = Path(live_home), Path(live_workspaces)
        self.hermes_cmd = list(hermes_cmd) if isinstance(hermes_cmd, (list, tuple)) else [hermes_cmd]
        # A migrated home holds the dependencies of the runtime checkout only (Hermes keys them by checkout path):
        # owner commands there must use the runtime's launcher, or Hermes would install a second set into the home.
        self.migrated_cmd = list(migrated_cmd) if migrated_cmd else [str(Path(root) / "runtime" / "bin" / "hermes.exe")]
        self.env, self.root, self.cred_dir, self.factory = env, Path(root), Path(cred_dir), runner_factory
        self.layout_file = self.root / "homes" / "layout.json"
        self._data, self._mtime, self._runners, self._lock = {"groups": {}}, None, {}, threading.RLock()

    # -- layout
    def data(self):
        try:
            m = self.layout_file.stat().st_mtime
        except OSError:
            return {"groups": {}}
        with self._lock:
            if m != self._mtime:
                self._data, self._mtime = json.loads(self.layout_file.read_text(encoding="utf-8")), m
            return self._data

    def migrated(self, key):
        return bool(self.data()["groups"].get(group_of(key), {}).get("migrated"))

    def shared(self):
        return self.data().get("shared") or None

    def home(self, key):
        g = self.data()["groups"].get(group_of(key), {})
        return Path(g["home"]) if g.get("migrated") else self.live_home

    def workspace(self, key):
        g = self.data()["groups"].get(group_of(key), {})
        return Path(g["workspace"]) if g.get("migrated") else self.live_ws / group_of(key)

    def profile_dir(self, name):
        return self.home(name) / "profiles" / name

    def profile_dirs(self):
        """Every federation profile, each from where it currently lives (other projects' profiles are never listed)."""
        names = {d.name for d in (self.live_home / "profiles").iterdir() if d.is_dir() and OURS.match(d.name)} \
            if (self.live_home / "profiles").is_dir() else set()
        for g in self.data()["groups"].values():
            if g.get("migrated"):
                names |= set(g.get("agents", []))
        return [self.profile_dir(n) for n in sorted(names) if self.profile_dir(n).is_dir()]

    def runner(self, key):
        g = group_of(key)
        with self._lock:
            if g not in self._runners:
                self._runners[g] = self.factory(self.root, hermes_home=self.home(g), cred=self.cred_dir / f"{identity(g)}.xml",
                                                runs=self.root / "runs" / identity(g))
            return self._runners[g]

    # -- running Hermes
    def target(self, args):
        a = list(args)
        if "-p" in a[:1] and len(a) > 1:
            return a[1]
        if a[:2] == ["kanban", "--board"] and len(a) > 2:
            return a[2]
        return None

    def run(self, *args, timeout=120, env=None):
        """(rc, stdout, stderr). Agent runs of a migrated group execute as its identity; management commands on a
        migrated group run as the owner against that group's home; everything else is unchanged."""
        key = self.target(args)
        if key and self.migrated(key):
            if agent_run(args):
                extra = {k: v for k, v in (env or {}).items() if k in PASS_ENV and v}
                rc, out, err = self.runner(key).hermes(*args, timeout=timeout, cwd=self.workspace(key), env=extra)
                return rc, ANSI.sub("", out), ANSI.sub("", err)
            cmd, env = self.migrated_cmd, {**(env or self.env), "HERMES_HOME": str(self.home(key))}
        else:
            cmd = self.hermes_cmd
        r = subprocess.run([*cmd, *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env or self.env)
        return r.returncode, ANSI.sub("", r.stdout), ANSI.sub("", r.stderr)

    def kanban(self, board, *args, timeout=180):
        return self.run("kanban", "--board", board, *args, timeout=timeout)
