"""Syntax check các file đã sửa cho per-tab mode.

Run: python3 test/syntax_check_per_tab_mode.py
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PY_FILES = ["db/repositories.py"]
JS_FILES = [
    "web/static/app.js",
    "web/static/session.js",
    "web/static/upi.js",
]


def check_python(path: Path, idx: int, total: int) -> bool:
    try:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        print(f"[PASS] [{idx}/{total}] py {path.relative_to(ROOT)}", flush=True)
        return True
    except SyntaxError as exc:
        print(f"[FAIL] [{idx}/{total}] py {path.relative_to(ROOT)} :: {exc}", flush=True)
        return False


def check_js(path: Path, idx: int, total: int) -> bool:
    """Dùng `node --check` để parse JS syntax."""
    try:
        proc = subprocess.run(
            ["node", "--check", str(path)],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        print(f"[SKIP] [{idx}/{total}] js {path.relative_to(ROOT)} :: node not found", flush=True)
        return True
    except subprocess.TimeoutExpired:
        print(f"[FAIL] [{idx}/{total}] js {path.relative_to(ROOT)} :: timeout", flush=True)
        return False
    if proc.returncode == 0:
        print(f"[PASS] [{idx}/{total}] js {path.relative_to(ROOT)}", flush=True)
        return True
    print(
        f"[FAIL] [{idx}/{total}] js {path.relative_to(ROOT)} :: "
        f"{proc.stderr.strip()[:300]}",
        flush=True,
    )
    return False


def main() -> int:
    total = len(PY_FILES) + len(JS_FILES)
    failed = 0
    idx = 0
    for rel in PY_FILES:
        idx += 1
        if not check_python(ROOT / rel, idx, total):
            failed += 1
    for rel in JS_FILES:
        idx += 1
        if not check_js(ROOT / rel, idx, total):
            failed += 1
    print(f"=== {total - failed}/{total} OK ===", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
