"""Federation guard: a Hermes pre_tool_call hook that enforces company boundaries outside the model.

Hermes runs this before every matching tool call, with the call as JSON on stdin. The agent identity
comes from --agent, fixed in the profile's config.yaml (the model cannot choose it) and cross-checked
against the profile Hermes reports. Exit code 2 blocks the call; the hook is configured fail_closed, so a
crash or timeout also blocks it.

Enforced (structured tools; these are real boundaries):
  kanban_*   a company agent may only touch its own company's board; only federation roles listed in
             CROSS_BOARD may name another board. kanban_create may only assign work inside the agent's own
             company (cross-company work goes through a work order) and may not use the "[tsk_" title prefix
             reserved for federation tasks. kanban_link: a dispatched worker may only link its own task; any
             other link needs a manager (level 50+) of the same company. Hermes itself already limits
             complete/block/unblock/review/heartbeat to the worker's own task.
  read_file, write_file, patch, search_files
             every path (relative paths against the session cwd) is resolved with realpath, so `..`,
             symbolic links and junctions cannot escape; it must stay inside the agent's own company
             workspace, its company's Kanban scratch space or its own profile folder, and never reach a
             secret or database file (.env, *.db, owner.json, auth.json) there.

Defence in depth only (NOT a boundary): terminal, process_manage and execute_code commands are refused
when their text names another company's workspace, another profile, the federation data folder, the
memory MCP server or a secret file. A determined agent can build such a path at run time; only an
operating-system sandbox (separate user or container per company) stops that.

The command denylist also names the guard's own folder, the Hermes runtime (hermes-agent), Hermes' auth.json,
other companies' Kanban boards and PowerShell -EncodedCommand / FromBase64String (2026-10-29 remediation). These
catch the literal forms only; they are NOT a boundary (see security/ISOLATION_PROPOSAL.md, OPEN-1/2/3).

Container mode (--container-workspace /workspace, off unless the flag is given): when the profile's terminal backend
is a per-company container, file-tool paths are container paths. /workspace/... maps to the company's host
workspace and is checked as above; any other absolute container path is refused, because nothing else is mounted.

Host-side tools (G2-1b; the profiles' matcher lists them, see install_guard.MATCHER):
  vision_analyze / video_analyze  a local source must be an absolute path that passes the read_file checks
  browser_navigate                http(s) only; never the federation's loopback services (ports 8765, 11434)
  computer_use, browser_cdp, browser_exec, browser_vault, cronjob_manage, skill_manage
                                  refused (host user's full reach) unless the agent is in HOST_POWER_ALLOWED
                                  (fed-hermes-admin only)

Every refusal is written to the federation audit log (memory.db, table audit) without the call's content.

    hooks:
      pre_tool_call:
        - matcher: "kanban_.*|read_file|write_file|patch|search_files|terminal|process_manage|execute_code"
          command: python -I fed_guard.py --agent isc-executor --home <HERMES_HOME> --workspaces <root>
          fail_closed: true
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

COMPANIES = ("isc", "auvia", "tabbeqai", "spectra", "spectrasolutions")
# Federation roles allowed to name any board (cross-company operations). Everyone else: own board only.
CROSS_BOARD = {"fed-hermes-admin", "fed-coo"}
# Authority levels (same table as memory/fedmem.py LEVEL); used for Kanban overrides.
LEVEL = {"ceo": 70, "delivery-director": 60, "engineering-director": 60, "project-manager": 50, "team-lead": 50,
         "solution-architect": 50, "hermes-admin": 90, "coo": 80}
KANBAN_MUTATIONS = ("create", "complete", "block", "unblock", "archive", "assign", "reassign", "link", "unlink",
                    "promote", "reopen", "reopen-review", "edit", "reclaim", "dispatch", "request-changes",
                    "request-review", "schedule", "set-model", "swarm", "decompose", "specify", "gc", "repair")
# Federation roles allowed to read every company workspace through the file tools (never write).
READ_ALL_WORKSPACES = {"fed-hermes-admin", "fed-audit"}
FILE_TOOLS = {"read_file", "write_file", "patch", "search_files"}
WRITE_TOOLS = {"write_file", "patch"}
COMMAND_TOOLS = {"terminal", "process_manage", "execute_code"}
SECRET_NAMES = re.compile(r"(^|[\\/])(\.env(\..*)?|owner\.json|auth\.json|shell-hooks-allowlist\.json)$|\.db(-wal|-shm)?$",
                          re.I)
# A profile's control files: identity, permissions, the guard hook and its approval. Agents may read them,
# never change them, even inside their own profile folder (otherwise an agent could remove its own guard).
CONTROL_FILES = re.compile(r"(^|[\\/])(config\.yaml(\.bak[^\\/]*)?|profile\.yaml|SOUL\.md)$", re.I)
PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete|Move to:?) File: (.+)$", re.M)
# PowerShell's -EncodedCommand in any accepted abbreviation (-e, -ec, -en, -enc, ...), with - or / prefix.
ENCODED_PS = re.compile(r"\b(?:powershell|pwsh)(?:\.exe)?\b[^\n;&|]*?\s[-/]e(?:c|n|nc|nco|ncod|ncode|ncoded|ncodedc\w*)?\b"
                        r"|frombase64string")
GUARD_DIR = Path(__file__).resolve().parent
# Host-side tools (they run in the Hermes process on the host, outside any terminal sandbox). Checked because the
# profiles' hook matcher lists them (G2-1b, approved 2026-10-30; install_guard.MATCHER).
# Media tools take a URL or a host path: a path must be absolute and pass the same checks as read_file (Hermes resolves
# a relative one against its own process directory, not the session cwd, so a relative path cannot be checked).
MEDIA_TOOLS = {"vision_analyze": "image_url", "video_analyze": "video_url"}
# The browser may not open local files or the federation's own loopback services (Console, Ollama).
FEDERATION_PORTS = {8765, 11434}
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}
# Tools that act with the whole host user's reach (desktop control, raw CDP/JS in the host browser, saved browser
# credentials, host scheduler, shared skills). Refused for every agent not listed here (owner decision 2026-10-30:
# only the federation administrator keeps them).
HOST_POWER_TOOLS = {"computer_use", "browser_cdp", "browser_exec", "browser_vault", "cronjob_manage", "skill_manage"}
HOST_POWER_ALLOWED = {"fed-hermes-admin"}


def company_of(agent):
    prefix = agent.split("-", 1)[0]
    return "federation" if prefix == "fed" else prefix


def norm(p):
    return os.path.normcase(os.path.realpath(p))


def inside(path, root):
    path, root = norm(path), norm(root)
    return path == root or path.startswith(root.rstrip("\\/") + os.sep)


class Guard:
    def __init__(self, agent, home, workspaces, db=None, container_workspace=None, protected=None):
        self.agent, self.company = agent, company_of(agent)
        self.home, self.workspaces = Path(home), Path(workspaces)
        self.db = Path(db) if db else self.home / "federation" / "memory.db"
        self.container_ws = PurePosixPath(container_workspace) if container_workspace else None
        # folders and files no command may name: the guard itself, the code that runs the hooks, provider auth
        self.protected = [Path(p) for p in (protected if protected is not None else
                                            (GUARD_DIR, self.home / "hermes-agent", self.home / "auth.json"))]

    def to_host(self, raw, cwd):
        """Container mode: map a container path to the host path it is mounted from (None: not mounted)."""
        p = PurePosixPath(raw.replace("\\", "/"))
        if not p.is_absolute():
            p = PurePosixPath(cwd or str(self.container_ws)) / p
        parts = []
        for part in p.parts[1:]:  # normalise .. lexically; the mount has no links pointing outside it
            if part == "..":
                if parts:
                    parts.pop()
            elif part not in (".", ""):
                parts.append(part)
        p = PurePosixPath("/", *parts)
        if p != self.container_ws and self.container_ws not in p.parents:
            return None
        return self.workspaces / self.company / p.relative_to(self.container_ws)

    # -- policy
    def roots(self, write):
        own = [self.workspaces / self.company, self.home / "kanban" / "boards" / self.company / "workspaces",
               self.home / "profiles" / self.agent]
        if not write and self.agent in READ_ALL_WORKSPACES:
            own += [self.workspaces / c for c in COMPANIES]
        return own

    def check_path(self, raw, cwd, write):
        if not raw:
            raw = "."
        if self.container_ws is not None:
            host = self.to_host(raw, cwd)
            if host is None:
                return f"{raw}: only {self.container_ws} is mounted in this company's sandbox"
            raw, cwd = str(host), None
        p = Path(raw)
        if not p.is_absolute():
            p = Path(cwd or ".") / p
        if SECRET_NAMES.search(str(p)) or SECRET_NAMES.search(os.path.realpath(p)):
            return f"{raw}: secret and database files are not reachable through the file tools"
        if write and (CONTROL_FILES.search(str(p)) or CONTROL_FILES.search(os.path.realpath(p))):
            return f"{raw}: profile control files (config.yaml, profile.yaml, SOUL.md) cannot be changed by an agent"
        if not any(inside(p, r) for r in self.roots(write)):
            where = "your company's workspace" if self.company != "federation" else "the federation workspace"
            return f"{raw} resolves to {os.path.realpath(p)}, outside {where}"
        return None

    def decide(self, call):
        tool = call.get("tool_name") or ""
        args = call.get("tool_input") or {}
        reported = call.get("profile")
        if reported and reported != self.agent:
            return f"identity mismatch: hook is bound to {self.agent}, call came from profile {reported}"
        if tool.startswith("kanban_"):
            board = (args.get("board") or "").strip().lower()
            if board and board != self.company and self.agent not in CROSS_BOARD:
                return f"{self.agent} may not use board '{board}'; only its own board '{self.company}'"
            if tool == "kanban_create":
                assignee = (args.get("assignee") or "").strip().lower()
                owner_co = "federation" if assignee.startswith("fed-") else assignee.split("-", 1)[0]
                if assignee and owner_co != self.company and self.agent not in CROSS_BOARD:
                    return (f"{self.agent} may not assign work to {assignee} of another company; "
                            "use a work order (work_order tool)")
                if (args.get("title") or "").lstrip().lower().startswith("[tsk_"):
                    return "the '[tsk_' title prefix is reserved for federation tasks created by the workflow"
            if tool == "kanban_link":
                own = os.environ.get("HERMES_KANBAN_TASK")
                child = str(args.get("child_id") or "")
                role = self.agent.split("-", 1)[1] if "-" in self.agent else ""
                if own and child != own:
                    return f"a worker may only link its own task ({own}), not {child or 'another task'}"
                if not own and LEVEL.get(role, 0) < 50:
                    return f"linking other tasks is a manager action (level 50+); {self.agent} cannot"
            return None
        if tool in FILE_TOOLS:
            write = tool in WRITE_TOOLS
            paths = [args.get("path") or args.get("file_path") or ("." if tool == "search_files" else "")]
            if tool == "patch" and isinstance(args.get("patch"), str):
                paths += [m.strip() for m in PATCH_FILE.findall(args["patch"])]
            for raw in paths:
                why = self.check_path(raw, call.get("cwd"), write)
                if why:
                    return why
            return None
        if tool in HOST_POWER_TOOLS:
            if self.agent not in HOST_POWER_ALLOWED:
                return f"{tool} acts with the host user's full reach; not available to {self.agent}"
            return None
        if tool in MEDIA_TOOLS:
            src = str(args.get(MEDIA_TOOLS[tool]) or "").strip()
            if src.lower().startswith(("http://", "https://", "data:")):
                return None  # network fetch: Hermes' own URL safety applies
            path = src[len("file://"):] if src.lower().startswith("file://") else src
            if self.container_ws is None and not Path(path).is_absolute():
                return f"{tool}: give an absolute path inside your workspace (relative host paths cannot be checked)"
            return self.check_path(path, call.get("cwd"), False)
        if tool == "browser_navigate":
            url = urlsplit(str(args.get("url") or "").strip())
            if url.scheme.lower() not in ("http", "https"):
                return f"browser_navigate: only http(s) URLs (not '{url.scheme or 'no scheme'}:')"
            try:
                port = url.port or (443 if url.scheme.lower() == "https" else 80)
            except ValueError:
                return "browser_navigate: invalid port"
            if (url.hostname or "").lower() in LOOPBACK_HOSTS | {"::1"} and port in FEDERATION_PORTS:
                return "browser_navigate: the federation's own local services (Console, Ollama) are off limits"
            return None
        if tool in COMMAND_TOOLS:
            text = json.dumps(args, ensure_ascii=False).replace("\\\\", "/").replace("\\", "/").lower()
            text = re.sub(r"/+", "/", text)
            home = str(self.home).replace("\\", "/").lower()
            ws = str(self.workspaces).replace("\\", "/").lower()
            banned = [f"{home}/federation", "memory.db", "workflow.db", "owner.json", "memory_mcp.py", "fed_guard.py",
                      "hermes-backups", "hermes-federation/.env", "shell-hooks-allowlist"]
            banned += [f"{ws}/{c}" for c in COMPANIES if c != self.company]
            banned += [f"workspaces/{c}" for c in COMPANIES if c != self.company]
            banned += [f"--board {c}" for c in COMPANIES + ("federation",) if c != self.company]
            banned += [f"profiles/{self.agent}/{f}" for f in ("config.yaml", "profile.yaml", "soul.md")]
            banned += [f"kanban/boards/{c}" for c in COMPANIES + ("federation",) if c != self.company]
            banned += [re.sub(r"/+", "/", str(p).replace("\\", "/").lower()) for p in self.protected]
            hit = next((b for b in banned if b in text), None)
            if hit is None and ENCODED_PS.search(text):
                hit = "an encoded PowerShell command (its content cannot be checked)"
            if hit is None:
                m = re.search(r"\bhermes(?:\.exe)?\s+(?:-p\s+\S+\s+)?kanban\b[^\n;&|]*?\b(" + "|".join(
                    map(re.escape, KANBAN_MUTATIONS)) + r")\b", text)
                if m:
                    hit = f"hermes kanban {m.group(1)} (Kanban changes go through the kanban tools)"
            if hit is None and re.search(r"profiles/(?!" + re.escape(self.agent) + r"\b)[a-z]+-[a-z-]+", text):
                hit = "another agent's profile"
            if hit is None and re.search(r"(^|[^a-z])\.env\b", text):
                hit = ".env"
            if hit:
                return f"command refers to '{hit}', outside {self.agent}'s company boundary"
            return None
        return None

    # -- audit (refusals only, no call content)
    def audit(self, tool, reason):
        try:
            c = sqlite3.connect(str(self.db), timeout=10)
            c.execute("CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, ts REAL NOT NULL, actor TEXT NOT NULL, "
                      "action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', result TEXT NOT NULL, "
                      "detail TEXT NOT NULL DEFAULT '', org_version TEXT NOT NULL)")
            c.execute("INSERT INTO audit (ts, actor, action, target, result, detail, org_version) VALUES (?,?,?,?,?,?,?)",
                      (time.time(), self.agent, f"guard.{tool}", self.company, "denied", reason[:300], "guard"))
            c.commit()
            c.close()
        except sqlite3.Error:
            pass  # never let auditing turn a refusal into an allow


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True)
    ap.add_argument("--home", required=True)
    ap.add_argument("--workspaces", required=True)
    ap.add_argument("--db")
    ap.add_argument("--container-workspace", help="container mode: the company workspace's mount point, e.g. /workspace")
    opts = ap.parse_args()
    raw = sys.stdin.buffer.read().decode("utf-8", "replace")
    try:
        call = json.loads(raw)
    except ValueError:
        call = None
    g = Guard(opts.agent, opts.home, opts.workspaces, opts.db, opts.container_workspace)
    if not isinstance(call, dict) or not call.get("tool_name"):
        call, reason = {"tool_name": "?"}, "malformed hook payload"
    else:
        reason = g.decide(call)
    if reason:
        g.audit(call.get("tool_name") or "?", reason)
        sys.stdout.write(json.dumps({"action": "block", "message": f"Federation guard: {reason}"}))
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
