"""Smoke import sau khi gỡ session-cookie-cache: bắt NameError/symbol sót.

Chạy: python3 test/smoke_imports_after_remove.py
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GSH_DB_PATH", str(ROOT / "runtime" / "data.db"))

MODULES = [
    "session_phase",
    "db.repositories",
    "web.upi_runner",
    "web.manager",
]

_fails = 0


def _check(ok: bool, tag: str, desc: str, detail: str = "") -> None:
    global _fails
    status = "PASS" if ok else "FAIL"
    if not ok:
        _fails += 1
    print(f"[{status}] {tag} — {desc} :: {detail}", flush=True)


def main() -> int:
    for i, mod in enumerate(MODULES, 1):
        try:
            importlib.import_module(mod)
            _check(True, f"IMP-{i:02d}", f"import {mod}", "ok")
        except Exception as exc:  # noqa: BLE001
            _check(False, f"IMP-{i:02d}", f"import {mod}", f"{type(exc).__name__}: {exc}")

    print(f"\n=== {'ALL PASS' if _fails == 0 else str(_fails) + ' FAIL'} ===", flush=True)
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
