"""Move the legacy `custom_providers` Ollama entry into the `providers:` map (hermes doctor issue).

Touches only the default home and the federation profiles. A .bak copy is written next to each file.
Run:  python migrate_ollama_provider.py [--dry-run]
"""
import os
import re
import shutil
import sys
from pathlib import Path

DRY = "--dry-run" in sys.argv
HOME = Path(os.environ["LOCALAPPDATA"]) / "hermes"
OURS = re.compile(r"^(fed|isc|auvia|tabbeqai|spectra|spectrasolutions)-")

NEW_BLOCK = """providers:
  local-ollama:
    api: http://localhost:11434/v1
    transport: openai_chat
    models:
      qwen2.5-coder:7b:
        context_length: 65536
"""


def migrate(cfg: Path) -> str:
    text = cfg.read_text(encoding="utf-8")
    if re.search(r"^providers:", text, re.M):
        return "skip (providers: already present)"
    m = re.search(r"^custom_providers:\n(?:[ \t-].*\n|\n)*", text, re.M)
    if not m:
        return "skip (no custom_providers)"
    block = m.group(0)
    if "localhost:11434" not in block or block.count("- name:") != 1:
        return "skip (custom_providers is not the single Ollama entry)"
    if not DRY:
        shutil.copy2(cfg, cfg.with_suffix(".yaml.bak-ollama-migration"))
        cfg.write_text(text[:m.start()] + NEW_BLOCK + text[m.end():], encoding="utf-8")
    return "migrated"


targets = [HOME / "config.yaml"] + sorted(p / "config.yaml" for p in (HOME / "profiles").iterdir()
                                          if p.is_dir() and OURS.match(p.name))
counts = {}
for cfg in targets:
    result = migrate(cfg) if cfg.exists() else "skip (missing)"
    counts[result] = counts.get(result, 0) + 1
    if cfg.parent == HOME:
        print(f"default: {result}")
print(counts)
