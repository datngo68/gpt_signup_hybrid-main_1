"""Syntax + AST check sau khi thêm CF warm-up + chrome142 ưu tiên vào upi_runner.

Mục tiêu:
  1. Parse AST web/upi_runner.py — fail nếu syntax error.
  2. Verify _warm_cf_cookie tồn tại như async function.
  3. Verify _IMPERSONATE_CHAIN local = ("chrome142", "chrome145", "chrome136")
     (đảm bảo override đã apply, không bị shadow).
  4. Verify markers `[FIX-CF-WARMUP-2026-06-20]` + `[FIX-CHROME142-FIRST-2026-06-20]`
     có mặt để revert dễ.

Run: python3 test/syntax_check_upi_runner_cf_warmup.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET = REPO / "web" / "upi_runner.py"


def main() -> int:
    src = TARGET.read_text(encoding="utf-8")

    # 1. AST parse
    print(f"[1/4] AST parse {TARGET.relative_to(REPO)}", flush=True)
    try:
        tree = ast.parse(src, filename=str(TARGET))
    except SyntaxError as exc:
        print(f"  [FAIL] SyntaxError: {exc}", flush=True)
        return 1
    print("  [PASS] parsed OK", flush=True)

    # 2. _warm_cf_cookie là async function
    print("[2/4] check _warm_cf_cookie tồn tại + async", flush=True)
    found_warm = False
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_warm_cf_cookie":
            found_warm = True
            break
    if not found_warm:
        print("  [FAIL] _warm_cf_cookie không có hoặc không phải async def", flush=True)
        return 1
    print("  [PASS] _warm_cf_cookie là async def", flush=True)

    # 3. _IMPERSONATE_CHAIN local override
    print("[3/4] check _IMPERSONATE_CHAIN = chrome142 đầu chain", flush=True)
    chain_value: tuple[str, ...] | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == "_IMPERSONATE_CHAIN":
            if isinstance(node.value, ast.Tuple):
                chain_value = tuple(
                    (e.value if isinstance(e, ast.Constant) else None)
                    for e in node.value.elts
                )
                break
    if chain_value is None:
        print("  [FAIL] không tìm thấy assignment _IMPERSONATE_CHAIN", flush=True)
        return 1
    expected = ("chrome142", "chrome145", "chrome136")
    if chain_value != expected:
        print(f"  [FAIL] chain={chain_value} ≠ expected={expected}", flush=True)
        return 1
    print(f"  [PASS] chain={chain_value}", flush=True)

    # 4. Markers
    print("[4/4] check revert markers", flush=True)
    markers = (
        "[FIX-CF-WARMUP-2026-06-20]",
        "[FIX-CHROME142-FIRST-2026-06-20]",
    )
    missing = [m for m in markers if m not in src]
    if missing:
        print(f"  [FAIL] thiếu marker: {missing}", flush=True)
        return 1
    for m in markers:
        count = src.count(m)
        print(f"  [PASS] marker {m} xuất hiện {count} lần", flush=True)

    print("[DONE] tất cả check pass", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
