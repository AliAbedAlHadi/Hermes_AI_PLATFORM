"""Tune federation profiles for the local model after the 2026-10-20 failure analysis.

1. Tell Hermes the real context window of qwen3.5:9b on this Ollama (65536: start Ollama with OLLAMA_CONTEXT_LENGTH=65536, the Hermes minimum)
   so it compresses history before the window fills (the task died at n_tokens=32767, truncated=1).
2. Lower reasoning effort from "medium" to "low" so thinking does not eat the output budget.

Text edits only on the default home and the federation profiles; a .bak copy is kept.
Run:  python tune_local_agents.py [--dry-run]
"""
import os
import re
import shutil
import sys
from pathlib import Path

DRY = "--dry-run" in sys.argv
HOME = Path(os.environ["LOCALAPPDATA"]) / "hermes"
OURS = re.compile(r"^(fed|isc|auvia|tabbeqai|spectra|spectrasolutions)-")
CTX = 65536
MODEL_ENTRY = f"      qwen3.5:9b:\n        context_length: {CTX}\n"


def tune(cfg: Path):
    text = cfg.read_text(encoding="utf-8")
    changed = []
    # 1. context_length for qwen3.5:9b inside providers.local-ollama.models
    m = re.search(r"^  local-ollama:\n(?:    .*\n|      .*\n|        .*\n)*", text, re.M)
    if m and "qwen3.5:9b:" not in m.group(0):
        block = m.group(0)
        if "    models:\n" in block:
            block2 = block.replace("    models:\n", "    models:\n" + MODEL_ENTRY, 1)
        else:
            block2 = block + "    models:\n" + MODEL_ENTRY
        text = text[:m.start()] + block2 + text[m.end():]
        changed.append("context_length")
    # 2. reasoning effort medium -> low (the active, uncommented key only)
    text, n = re.subn(r'^(\s+reasoning_effort:\s*)"medium"\s*$', r'\g<1>"low"', text, count=1, flags=re.M)
    if n:
        changed.append("reasoning=low")
    if changed and not DRY:
        shutil.copy2(cfg, cfg.with_suffix(".yaml.bak-tune"))
        cfg.write_text(text, encoding="utf-8")
    return changed


targets = [HOME / "config.yaml"] + sorted(p / "config.yaml" for p in (HOME / "profiles").iterdir()
                                          if p.is_dir() and OURS.match(p.name))
summary = {}
for cfg in targets:
    key = ",".join(tune(cfg)) or "unchanged"
    summary[key] = summary.get(key, 0) + 1
print(summary)
