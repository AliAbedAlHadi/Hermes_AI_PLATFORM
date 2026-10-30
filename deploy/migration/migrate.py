"""Stages 3-4 of the per-company identity migration (MIGRATION_PLAN.md). Run as the owner, NOT elevated, with the
Console stopped:

    python -I migrate.py --status
    python -I migrate.py --shared              # once: shared databases -> C:\\HermesFederation\\shared
    python -I migrate.py --group isc           # then each group: isc, auvia, tabbeqai, spectra, spectrasolutions, federation
    python -I migrate.py --rollback isc        # the Console runs that group as the owner again, from the live home

Copies, never moves: the live home (%LOCALAPPDATA%\\hermes) keeps every profile, board and database as the rollback.
Everything changed in the live home (config.yaml and hook approvals when --shared repoints the databases) is backed up
to %USERPROFILE%\\hermes-backups\\migration-<time> first. Secrets (.env) are copied as files, never read.
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fedlayout import (GROUPS, OURS, Layout, RewriteError, allowlist, fwd, group_of, identity,  # noqa: E402
                       read_layout, rewrite_config)

LIVE = Path(os.environ.get("FED_LIVE_HOME") or Path(os.environ["LOCALAPPDATA"]) / "hermes")
LIVE_WS = Path(os.environ.get("FED_LIVE_WORKSPACES") or Path.home() / "hermes-federation" / "workspaces")
BACKUPS = Path(os.environ.get("FED_BACKUPS") or Path.home() / "hermes-backups")
CRED_DIR = Path(os.environ["LOCALAPPDATA"]) / "hermes-federation-launcher"
CONTROL = ("config.yaml", "SOUL.md", "profile.yaml", "shell-hooks-allowlist.json", ".env")
SKIP = shutil.ignore_patterns("*.lock", "config.yaml.bak*")


class MigrationError(Exception):
    pass


def log(m):
    print(m, flush=True)


def live_activity():
    """Processes that would race a copy: the Console, or any Hermes run (agents, kanban workers)."""
    ps = ("Get-CimInstance Win32_Process | Where-Object { ($_.Name -match '^hermes(-acp)?\\.exe$') -or "
          "($_.CommandLine -match 'console[\\\\/]server\\.py') } | ForEach-Object { \"$($_.ProcessId) $($_.Name)\" }")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True)
    return [l for l in r.stdout.splitlines() if l.strip()]


def require_quiet():
    busy = live_activity()
    if busy:
        raise MigrationError("stop the Console and every Hermes run first: " + "; ".join(busy[:5]))


def copy_db(src, dst):
    """A consistent copy through SQLite's backup API (includes the WAL); contents unchanged."""
    if dst.exists():
        raise MigrationError(f"{dst} already exists - refusing to overwrite")
    with closing(sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)) as s, closing(sqlite3.connect(dst)) as d:
        s.backup(d)
        if d.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MigrationError(f"integrity check failed on the copy {dst}")


def set_acl(path, grants, inherit=False):
    """Replace a file's ACL (the owner created it, so the owner may set it without elevation)."""
    sid = {"SYSTEM": "*S-1-5-18", "ADMINS": "*S-1-5-32-544"}
    args = ["icacls", str(path)] + ([] if inherit else ["/inheritance:r"]) + ["/grant:r"]
    args += [f"{sid.get(who, who)}:{right}" for who, right in grants] + ["/Q"]
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode:
        raise MigrationError(f"icacls failed on {path}: {r.stdout.strip()} {r.stderr.strip()}")


def install_profile(agent, lay, src_home=LIVE):
    """Copy one profile into its group's home, rewrite it, approve the new guard command, lock its control files."""
    g = group_of(agent)
    src, dst = src_home / "profiles" / agent, lay.home(g) / "profiles" / agent
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=SKIP)
    cfg = rewrite_config((src / "config.yaml").read_text(encoding="utf-8"), agent, lay)
    (dst / "config.yaml").write_text(cfg, encoding="utf-8", newline="\n")
    (dst / "shell-hooks-allowlist.json").write_text(allowlist(lay.guard_command(agent)), encoding="utf-8", newline="\n")
    owner, ident = os.environ["USERNAME"], f"{os.environ['COMPUTERNAME']}\\{identity(g)}"
    for f in CONTROL:
        if (dst / f).exists():  # the agent runs with these; it must not change its own permissions, soul or keys
            set_acl(dst / f, [("SYSTEM", "F"), ("ADMINS", "F"), (owner, "M"), (ident, "R")])
    return dst


def set_layout(lay, update):
    data = read_layout(lay)
    update(data)
    tmp = lay.layout_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, lay.layout_file)
    return data


def backup_dir():
    d = BACKUPS / f"migration-{time.strftime('%Y%m%d-%H%M%S')}"
    d.mkdir(parents=True)
    return d


def migrate_shared(lay):
    """memory.db and workflow.db -> shared; the Console and every LIVE profile (guard audit, memory tool) point there."""
    data = read_layout(lay)
    if data.get("shared"):
        raise MigrationError("the shared databases are already migrated")
    require_quiet()
    bk = backup_dir()
    old = {n: LIVE / "federation" / n for n in ("memory.db", "workflow.db")}
    new = {n: lay.shared / n for n in old}
    for n in old:
        copy_db(old[n], bk / n)            # the rollback copy
        copy_db(old[n], new[n])
        log(f"copied {n} -> {new[n]} (backup {bk / n})")
    changed = 0
    for p in sorted((LIVE / "profiles").iterdir()):
        if not (p.is_dir() and OURS.match(p.name)):
            continue
        cfg_p, allow_p = p / "config.yaml", p / "shell-hooks-allowlist.json"
        text = cfg_p.read_text(encoding="utf-8")
        out = text
        for n in old:
            out = out.replace(fwd(old[n]), fwd(new[n]))
        if out == text:
            raise MigrationError(f"{p.name}: no database path to repoint")
        (bk / "profiles" / p.name).mkdir(parents=True)
        shutil.copy2(cfg_p, bk / "profiles" / p.name / "config.yaml")
        shutil.copy2(allow_p, bk / "profiles" / p.name / "shell-hooks-allowlist.json")
        cmd = next(l.split('command: "', 1)[1].rstrip('"') for l in out.splitlines()
                   if "fed_guard.py --agent " + p.name + " " in l and 'command: "' in l)
        cfg_p.write_text(out, encoding="utf-8", newline="\n")
        allow_p.write_text(allowlist(cmd), encoding="utf-8", newline="\n")
        changed += 1
    set_layout(lay, lambda d: d.update(shared={n.split(".")[0]: str(new[n]) for n in new}, shared_backup=str(bk),
                                       shared_at=time.time()))
    log(f"{changed} live profiles now use the shared databases; backup of every changed file: {bk}")


def migrate_group(lay, g):
    data = read_layout(lay)
    if not data.get("shared"):
        raise MigrationError("migrate the shared databases first (--shared)")
    if data["groups"].get(g, {}).get("migrated"):
        raise MigrationError(f"{g} is already migrated")
    if not (CRED_DIR / f"{identity(g)}.xml").exists():
        raise MigrationError(f"no credential for {identity(g)} - run 2_setup_identities.ps1")
    if not list((lay.home(g) / "installs").glob("*/environments/*/venv/pyvenv.cfg")):
        raise MigrationError(f"no Hermes dependencies in {lay.home(g)} - run 2_setup_identities.ps1")
    require_quiet()
    agents = sorted(p.name for p in (LIVE / "profiles").iterdir() if OURS.match(p.name) and group_of(p.name) == g)
    for a in agents:
        install_profile(a, lay)
    log(f"{g}: {len(agents)} profiles copied, rewritten and locked")
    canary = lay.home(g) / "canary"
    if canary.exists():
        shutil.rmtree(canary)
    board_src, board_dst = LIVE / "kanban" / "boards" / g, lay.home(g) / "kanban" / "boards" / g
    if board_dst.exists():
        shutil.rmtree(board_dst)
    shutil.copytree(board_src, board_dst, ignore=SKIP)
    shutil.copytree(LIVE_WS / g, lay.workspaces / g, dirs_exist_ok=True)
    log(f"{g}: board and workspace copied")
    set_layout(lay, lambda d: d["groups"].__setitem__(g, {
        "migrated": True, "identity": identity(g), "home": str(lay.home(g)), "workspace": str(lay.workspaces / g),
        "agents": agents, "at": time.time()}))
    log(f"{g}: the Console now runs these agents as {identity(g)}")


def rollback_group(lay, g):
    info = read_layout(lay)["groups"].get(g)
    if not info or not info.get("migrated"):
        raise MigrationError(f"{g} is not migrated")
    since = info["at"]
    newer = [p for p in lay.home(g).rglob("*") if p.is_file() and p.stat().st_mtime > since + 60
             and "installs" not in p.parts]
    set_layout(lay, lambda d: d["groups"][g].update(migrated=False, rolled_back_at=time.time()))
    log(f"{g}: the Console runs it as the owner from the live home again. {len(newer)} files changed in "
        f"{lay.home(g)} after the cutover stay there (not copied back).")


def status(lay):
    data = read_layout(lay)
    log(json.dumps({"shared": data.get("shared"), "groups": {g: data["groups"].get(g, {}).get("migrated", False)
                                                              for g in GROUPS}}, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--shared", action="store_true")
    ap.add_argument("--group", choices=GROUPS)
    ap.add_argument("--rollback", choices=GROUPS)
    a = ap.parse_args()
    lay = Layout()
    try:
        if a.shared:
            migrate_shared(lay)
        if a.group:
            migrate_group(lay, a.group)
        if a.rollback:
            rollback_group(lay, a.rollback)
        status(lay)
    except (MigrationError, RewriteError) as e:
        log(f"REFUSED: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
