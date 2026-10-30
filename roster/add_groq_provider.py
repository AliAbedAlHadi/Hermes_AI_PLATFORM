"""Add the Groq provider (custom:groq) to the default home and the federation profiles.

The key is NOT written here: Hermes reads it from the GROQ_API_KEY environment variable (key_env).
Context length is set from the models.dev catalog for qwen/qwen3.8-27b (131042).
Run:  python add_groq_provider.py [--dry-run]
"""
import os
import re
import shutil
import sys
from pathlib import Path

DRY = "--dry-run" in sys.argv
HOME = Path(os.environ["LOCALAPPDATA"]) / "hermes"
OURS = re.compile(r"^(fed|isc|auvia|tabbeqai|spectra|spectrasolutions)-")
GROQ = """  groq:
    api: https://api.groq.com/openai/v1
    key_env: GROQ_API_KEY
    transport: openai_chat
    models:
      qwen/qwen3.8-27b:
        context_length: 131042
"""


def add(cfg: Path) -> str:
    text = cfg.read_text(encoding="utf-8")
    if re.search(r"^  groq:\s*$", text, re.M):
        return "already"
    if not re.search(r"^providers:\s*$", text, re.M):
        return "no providers: section"
    text = re.sub(r"^providers:\s*\n", "providers:\n" + GROQ, text, count=1, flags=re.M)
    if not DRY:
        shutil.copy2(cfg, cfg.with_suffix(".yaml.bak-groq"))
        cfg.write_text(text, encoding="utf-8")
    return "added"


targets = [HOME / "config.yaml"] + sorted(p / "config.yaml" for p in (HOME / "profiles").iterdir()
                                          if p.is_dir() and OURS.match(p.name))
summary = {}
for cfg in targets:
    r = add(cfg)
    summary[r] = summary.get(r, 0) + 1
print(summary)
