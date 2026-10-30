"""Permission fingerprint of the 118 federation profiles.

Hashes only what defines an agent's identity and permissions: its SOUL.md, model/provider, disabled
toolsets, the federation memory MCP entry, the guard hook and the tool-search setting. Hermes also writes
runtime UI flags into config.yaml (for example `onboarding: seen: ...`); those are ignored, so the fingerprint
changes only when a permission-relevant setting changes.

    python -I fingerprint.py                 # print the fingerprint
    python -I fingerprint.py --save FILE     # also save per-profile hashes
    python -I fingerprint.py --compare FILE  # list profiles whose permissions changed since FILE
"""
import hashlib
import json
import os
import re
import sys
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME") or (
    Path(os.environ["LOCALAPPDATA"]) / "hermes" if os.name == "nt" else Path.home() / ".hermes"))
OURS = re.compile(r"^(fed|isc|auvia|tabbeqai|spectra|spectrasolutions)-[a-z-]+$")
# per-company identities (deploy/migration): a migrated group's profiles live in its own home
LAYOUT = Path(os.environ.get("FED_LAYOUT_ROOT") or r"C:\HermesFederation") / "homes" / "layout.json"


def profile_dirs(home=HOME):
    """Every federation profile, each from where it currently lives (the live home, or its group's home)."""
    try:
        groups = json.loads(LAYOUT.read_text(encoding="utf-8")).get("groups", {})
    except (OSError, ValueError):
        groups = {}
    moved = {a: Path(g["home"]) / "profiles" / a for g in groups.values() if g.get("migrated") for a in g.get("agents", [])}
    live = [p for p in sorted((home / "profiles").iterdir()) if p.is_dir() and OURS.match(p.name)]         if (home / "profiles").is_dir() else []
    return sorted([moved.get(p.name, p) for p in live] + [d for n, d in moved.items() if not (home / "profiles" / n).is_dir()],
                  key=lambda p: p.name)


def block(text, key, indent=""):
    """The lines of a YAML block `key:` at the given indentation (empty when absent)."""
    m = re.search(rf"^{indent}{re.escape(key)}:.*\n((?:{indent}[ \t]+.*\n|\n)*)", text, re.M)
    return m.group(0) if m else ""


def profile_digest(pdir):
    cfg = (pdir / "config.yaml").read_text(encoding="utf-8")
    model = block(cfg, "model")
    parts = {
        "soul": (pdir / "SOUL.md").read_text(encoding="utf-8"),
        "model": "\n".join(re.findall(r"^\s+(?:provider|default):.*$", model, re.M)[:2]),
        "disabled_toolsets": block(cfg, "disabled_toolsets", "  "),
        "memory_mcp": block(cfg, "federation-memory", "  "),
        "hooks": block(cfg, "hooks"),
        "tools": block(cfg, "tools"),
    }
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def fingerprint():
    per = {p.name: profile_digest(p) for p in profile_dirs()}
    total = hashlib.sha256(json.dumps(per, sort_keys=True).encode()).hexdigest()[:16]
    return total, per


if __name__ == "__main__":
    total, per = fingerprint()
    print(f"{len(per)} profiles, permission fingerprint {total}")
    if "--save" in sys.argv:
        Path(sys.argv[sys.argv.index("--save") + 1]).write_text(json.dumps({"total": total, "profiles": per}, indent=1))
    if "--compare" in sys.argv:
        old = json.loads(Path(sys.argv[sys.argv.index("--compare") + 1]).read_text())
        changed = sorted(p for p in set(per) | set(old["profiles"]) if per.get(p) != old["profiles"].get(p))
        print("unchanged" if not changed else f"{len(changed)} changed: {', '.join(changed)}")
        sys.exit(1 if changed else 0)
