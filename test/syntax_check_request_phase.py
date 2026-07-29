"""Parse AST request_phase.py để verify syntax sau khi sửa OTP retry loop."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "request_phase.py"


def main() -> int:
    src = TARGET.read_text(encoding="utf-8")
    try:
        ast.parse(src, filename=str(TARGET))
    except SyntaxError as exc:
        print(f"[FAIL] syntax — {TARGET}:{exc.lineno}:{exc.offset} {exc.msg}", flush=True)
        return 1
    print(f"[PASS] syntax — {TARGET.name} ({len(src.splitlines())} lines)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
