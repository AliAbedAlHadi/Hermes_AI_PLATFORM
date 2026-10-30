"""Fast path: set model.provider/model.default in each federation profile's config.yaml.

Edits only the first `provider:` and `default:` lines inside the top-level `model:` block.
Run:  python set_local_model.py <provider> <model>
"""
import os, re, sys
from pathlib import Path

provider, model = sys.argv[1], sys.argv[2]
home = Path(os.environ.get("HERMES_HOME") or (
    Path(os.environ["LOCALAPPDATA"]) / "hermes" if os.name == "nt" else Path.home() / ".hermes"))
root = home / "profiles"
ours = re.compile(r"^(fed|isc|auvia|tabbeqai|spectra|spectrasolutions)-")
done = failed = 0
for d in sorted(root.iterdir()):
    cfg = d / "config.yaml"
    if not (d.is_dir() and ours.match(d.name) and cfg.exists()):
        continue
    text = cfg.read_text(encoding="utf-8")
    m = re.search(r"^model:\n((?:[ \t].*\n|\n)*)", text, re.M)
    if not m:
        failed += 1; print("no model block:", d.name); continue
    block = m.group(1)
    new, n1 = re.subn(r'^(\s+default:\s*).*$', rf'\g<1>"{model}"', block, count=1, flags=re.M)
    new, n2 = re.subn(r'^(\s+provider:\s*).*$', rf'\g<1>"{provider}"', new, count=1, flags=re.M)
    if n1 != 1 or n2 != 1:
        failed += 1; print("keys missing:", d.name, n1, n2); continue
    # Ollama needs model.ollama_num_ctx (64K+) to pass Hermes' context floor; hosted providers reject it
    # (it adds Ollama `options` to every request), so it exists only while the agents are local.
    new = re.sub(r"^\s+ollama_num_ctx:.*\n", "", new, flags=re.M)
    if provider.startswith("local"):
        new = re.sub(r'^(\s+)(provider:.*\n)', r'\g<1>\g<2>\g<1>ollama_num_ctx: 65536\n', new, count=1, flags=re.M)
    cfg.write_text(text[:m.start(1)] + new + text[m.end(1):], encoding="utf-8")
    done += 1
print(f"set {provider}/{model} on {done} profiles, failed {failed}")
