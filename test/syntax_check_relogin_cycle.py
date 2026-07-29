#!/usr/bin/env python3
"""AST syntax-check các file đã sửa cho feature re-login cycle.

Chạy: python3 test/syntax_check_relogin_cycle.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    "db/repositories.py",
    "web/manager.py",
    "web/upi_runner.py",
    "web/server.py",
]


def main() -> int:
    rc = 0
    for i, rel in enumerate(FILES, 1):
        p = ROOT / rel
        try:
            ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            print(f"[PASS] [{i}/{len(FILES)}] {rel} — AST OK", flush=True)
        except SyntaxError as e:
            rc = 1
            print(f"[FAIL] [{i}/{len(FILES)}] {rel} — {e}", flush=True)
    print("=== DONE ===", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
