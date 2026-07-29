"""Search for main_world_eval impl in Camoufox."""
from __future__ import annotations
import os
from pathlib import Path

camoufox_dir = Path(__file__).resolve().parent.parent / ".venv/lib/python3.13/site-packages/camoufox"
print(f"Searching: {camoufox_dir}")
for py in camoufox_dir.rglob("*.py"):
    try:
        text = py.read_text(encoding="utf-8")
    except Exception:
        continue
    if "main_world_eval" in text:
        print(f"\n=== {py.name} ===")
        for i, line in enumerate(text.splitlines(), 1):
            if "main_world_eval" in line:
                print(f"  {i}: {line}")
