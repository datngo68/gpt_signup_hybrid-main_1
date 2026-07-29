"""Search Camoufox source for allowMainWorld + xray handling."""
from __future__ import annotations
from pathlib import Path

camoufox_dir = Path(__file__).resolve().parent.parent / ".venv/lib/python3.13/site-packages/camoufox"
for py in camoufox_dir.rglob("*.py"):
    try:
        text = py.read_text(encoding="utf-8")
    except Exception:
        continue
    for kw in ("allowMainWorld", "xray", "Xray", "evaluate_in_main", "main_world"):
        if kw in text:
            print(f"\n=== {py.name} (kw={kw}) ===")
            for i, line in enumerate(text.splitlines(), 1):
                if kw in line:
                    print(f"  {i}: {line[:130]}")
            break
