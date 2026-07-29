"""Syntax check các file đã sửa cho China iCloud provider.

Run: python3 test/syntax_check_china_icloud.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FILES = [
    "mail_providers.py",
    "models.py",
    "signup.py",
    "web/mail_modes.py",
    "web/server.py",
]


def main() -> int:
    total = len(FILES)
    failed = 0
    for i, rel in enumerate(FILES, 1):
        path = ROOT / rel
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            print(f"[PASS] [{i}/{total}] {rel}", flush=True)
        except SyntaxError as exc:
            failed += 1
            print(f"[FAIL] [{i}/{total}] {rel} :: {exc}", flush=True)
    print(f"=== {total - failed}/{total} OK ===", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
