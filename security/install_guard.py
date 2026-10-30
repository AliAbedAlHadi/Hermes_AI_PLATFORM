"""Install the federation guard as a fail-closed pre_tool_call hook in every federation profile.

Writes a `hooks:` block with the profile's fixed identity and records that exact command in the profile's
shell-hook allowlist (no blanket hooks_auto_accept). Idempotent; keeps one config.yaml.bak-guard.

    python -I install_guard.py            # all 118 federation profiles
    python -I install_guard.py --remove   # take the hook out again
"""
import datetime
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("HERMES_HOME") or (
    Path(os.environ["LOCALAPPDATA"]) / "hermes" if os.name == "nt" else Path.home() / ".hermes"))
WORKSPACES = Path(os.environ.get("FED_WORKSPACES") or Path.home() / "hermes-federation" / "workspaces")
OURS = re.compile(r"^(fed|isc|auvia|tabbeqai|spectra|spectrasolutions)-[a-z-]+$")
MATCHER = "kanban_.*|read_file|write_file|patch|search_files|terminal|process_manage|execute_code"
MARK = "# federation guard (security/install_guard.py)"


def command(profile):
    fwd = lambda p: str(p).replace("\\", "/")  # noqa: E731
    return (f"{fwd(sys.executable)} -I {fwd(HERE / 'fed_guard.py')} --agent {profile} --home {fwd(HOME)} "
            f"--workspaces {fwd(WORKSPACES)} --db {fwd(HOME / 'federation' / 'memory.db')}")


def strip(lines):
    """Remove our marked block: the marker, the `hooks:` line after it and that block's indented lines."""
    out, i = [], 0
    while i < len(lines):
        if lines[i].rstrip() == MARK:
            i += 1
            if i < len(lines) and lines[i].rstrip() == "hooks:":
                i += 1
                while i < len(lines) and (lines[i].startswith(" ") or not lines[i].strip()):
                    i += 1
            continue
        out.append(lines[i])
        i += 1
    return out


def install(profile, remove=False):
    cfg = HOME / "profiles" / profile / "config.yaml"
    text = cfg.read_text(encoding="utf-8")
    bak = cfg.with_name("config.yaml.bak-guard")
    if not bak.exists():
        bak.write_text(text, encoding="utf-8")
    lines = strip(text.splitlines())
    if any(line.rstrip() == "hooks:" for line in lines):
        raise SystemExit(f"{profile}: has its own hooks block; merge by hand")
    cmd = command(profile)
    if not remove:
        lines += [MARK, "hooks:", "  pre_tool_call:", f'    - matcher: "{MATCHER}"', f"      command: {json.dumps(cmd)}",
                  "      timeout: 20", "      fail_closed: true"]
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    allow = HOME / "profiles" / profile / "shell-hooks-allowlist.json"
    data = json.loads(allow.read_text(encoding="utf-8-sig")) if allow.exists() else {"approvals": []}
    data["approvals"] = [e for e in data.get("approvals", []) if "fed_guard.py" not in e.get("command", "")]
    if not remove:
        data["approvals"].append({"event": "pre_tool_call", "command": cmd, "script_mtime_at_approval": None,
                                  "approved_at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
    allow.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main():
    remove = "--remove" in sys.argv
    done = [p.name for p in sorted((HOME / "profiles").iterdir()) if p.is_dir() and OURS.match(p.name)]
    for profile in done:
        install(profile, remove)
    print(f"guard {'removed from' if remove else 'installed in'} {len(done)} profiles")


if __name__ == "__main__":
    main()
