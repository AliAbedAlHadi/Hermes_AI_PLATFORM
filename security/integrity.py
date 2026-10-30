"""Tamper tripwire for the federation's security-critical files. DETECTION ONLY: it does not prevent anything.

Every agent runs as the same Windows user that owns these files, so an agent can change them (OPEN-3); this
tool only tells you afterwards. It hashes:

  guard      the federation code next to this file (security/, memory/, workflow/ *.py) - the guard every hook runs
  profiles   per federation profile: the permission digest of config.yaml (fingerprint.py, so Hermes' own UI flags
             do not count), SOUL.md, profile.yaml and shell-hooks-allowlist.json
  runtime    the Hermes runtime code that dispatches the hooks (hermes-agent/**/*.py, without tests and caches)

Secret files (.env, auth.json, owner.json, *.db) are never read. Keep the baseline somewhere agents cannot write
(an offline copy); a baseline next to the files can be rewritten together with them.

    python -I integrity.py --save BASELINE.json
    python -I integrity.py --verify BASELINE.json     # exit 1 and list the files that changed
    --code-root DIR   hash the federation code in DIR instead of this checkout (e.g. the folder the hooks run from)
"""
import hashlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from fingerprint import HOME, LAYOUT, profile_dirs, profile_digest  # noqa: E402

FEDERATION_CODE = [HERE.parent / d for d in ("security", "memory", "workflow")]
# the copy the migrated agents' guard and memory tool run from (deploy/migration/2_setup_identities.ps1)
FED_COPY = [LAYOUT.parent.parent / "fed" / d for d in ("security", "memory", "workflow")]
RUNTIME = HOME / "hermes-agent"
SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".cache", "tests", "tests-js", "evals", "website", "contributors"}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def py_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        yield from (Path(dirpath) / f for f in sorted(filenames) if f.endswith(".py"))


def manifest(home=HOME, code=FEDERATION_CODE, runtime=RUNTIME, fed_copy=FED_COPY):
    m = {}
    for root in code:
        if root.is_dir():
            m.update({f"guard:{root.name}/{f.relative_to(root).as_posix()}": sha(f) for f in py_files(root)})
    for root in fed_copy:
        if root.is_dir():
            m.update({f"fedcopy:{root.name}/{f.relative_to(root).as_posix()}": sha(f) for f in py_files(root)})
    for p in profile_dirs(home):
        if (p / "config.yaml").is_file() and (p / "SOUL.md").is_file():
            m[f"profile:{p.name}/permissions"] = profile_digest(p)
        for n in ("profile.yaml", "shell-hooks-allowlist.json"):
            if (p / n).is_file():
                m[f"profile:{p.name}/{n}"] = sha(p / n)
    if runtime.is_dir():
        m.update({f"runtime:{f.relative_to(runtime).as_posix()}": sha(f) for f in py_files(runtime)})
    return m


def compare(old, new):
    return {"changed": sorted(k for k in old.keys() & new.keys() if old[k] != new[k]),
            "added": sorted(new.keys() - old.keys()), "removed": sorted(old.keys() - new.keys())}


def main():
    root = Path(sys.argv[sys.argv.index("--code-root") + 1]) if "--code-root" in sys.argv else HERE.parent
    m = manifest(code=[root / d for d in ("security", "memory", "workflow")])
    groups = {g: sum(1 for k in m if k.startswith(g + ":")) for g in ("guard", "profile", "runtime")}
    print(f"{len(m)} entries: " + ", ".join(f"{g} {n}" for g, n in groups.items()))
    if "--save" in sys.argv:
        Path(sys.argv[sys.argv.index("--save") + 1]).write_text(json.dumps(m, indent=0, sort_keys=True), encoding="utf-8")
    if "--verify" in sys.argv:
        diff = compare(json.loads(Path(sys.argv[sys.argv.index("--verify") + 1]).read_text(encoding="utf-8")), m)
        for kind, keys in diff.items():
            for k in keys:
                print(f"{kind.upper():8} {k}")
        bad = sum(map(len, diff.values()))
        print("unchanged" if not bad else f"{bad} differences")
        sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
