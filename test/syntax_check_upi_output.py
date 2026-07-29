#!/usr/bin/env python3
"""Syntax check: parse AST mọi file Python đã chỉnh trong patch UPI output.

Kiểm:
- web/upi_runner.py     (thêm auth_sink param + fill sau login)
- web/manager.py        (UpiJob plus_cache, _run_job timeout sink, helpers)
- web/server.py         (2 endpoints mới: secrets, delete plus)

Chạy:
    python3 test/syntax_check_upi_output.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TARGETS = [
    ROOT / "web" / "upi_runner.py",
    ROOT / "web" / "manager.py",
    ROOT / "web" / "server.py",
]


def main() -> int:
    failures = 0
    for idx, path in enumerate(TARGETS, start=1):
        rel = path.relative_to(ROOT)
        if not path.exists():
            print(f"[FAIL] [{idx}/{len(TARGETS)}] missing: {rel}", flush=True)
            failures += 1
            continue
        src = path.read_text(encoding="utf-8")
        try:
            ast.parse(src, filename=str(path))
        except SyntaxError as exc:
            print(
                f"[FAIL] [{idx}/{len(TARGETS)}] {rel} :: SyntaxError "
                f"line={exc.lineno} col={exc.offset}: {exc.msg}",
                flush=True,
            )
            failures += 1
            continue
        print(f"[PASS] [{idx}/{len(TARGETS)}] {rel}", flush=True)

    if failures:
        print(f"\n{failures}/{len(TARGETS)} files failed", flush=True)
        return 1
    print(f"\nAll {len(TARGETS)} files OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
