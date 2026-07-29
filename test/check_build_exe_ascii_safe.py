#!/usr/bin/env python3
"""Verify scripts/build_exe.py không có ký tự non-ASCII trong print() calls
(tránh UnicodeEncodeError trên Windows console default cp1252).

Comments + docstring có thể dùng unicode (file đã set encoding="utf-8" qua
PEP 263 default). Chỉ check string literals trong print() / f-string trong
print().

Cũng verify cp1252 encode được toàn bộ output có thể của script — fail ngay
local nếu lỡ thêm '→' trong print mới.

Chạy:
    .venv/bin/python3 test/check_build_exe_ascii_safe.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "scripts" / "build_exe.py"


def main() -> int:
    if not TARGET.is_file():
        print(f"[FAIL] {TARGET} không tồn tại", flush=True)
        return 1

    src = TARGET.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(TARGET))

    # Walk AST, tìm mọi Call tới print() và check arg literal có ASCII-safe không.
    violations: list[tuple[int, str, str]] = []  # (lineno, snippet, char)

    def _check_string(node: ast.AST, lineno: int) -> None:
        # ast.Constant (Python 3.8+) for str literal.
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for ch in node.value:
                # cp1252 covers most Latin-1; reject only known problem chars.
                if ord(ch) > 0x7F:
                    try:
                        ch.encode("cp1252")
                    except UnicodeEncodeError:
                        violations.append((
                            lineno,
                            node.value[:60],
                            f"{ch} (U+{ord(ch):04X})",
                        ))
                        break
        elif isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.Constant):
                    _check_string(part, lineno)
                elif isinstance(part, ast.FormattedValue):
                    # f-string {value} part — value có thể là expression,
                    # không thể static check. Bỏ qua.
                    pass

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Chỉ care print() và print(file=sys.stderr/stdout)
        func = node.func
        is_print = (
            (isinstance(func, ast.Name) and func.id == "print")
        )
        if not is_print:
            continue
        for arg in node.args:
            _check_string(arg, node.lineno)

    if violations:
        print(f"[FAIL] scripts/build_exe.py có {len(violations)} non-cp1252 chars trong print():",
              flush=True)
        for lineno, snippet, ch in violations:
            print(f"  line {lineno}: {ch} in {snippet!r}", flush=True)
        return 1
    print("[PASS] scripts/build_exe.py print() string literals đều cp1252-safe", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
