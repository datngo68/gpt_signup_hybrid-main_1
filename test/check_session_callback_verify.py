"""Verify fix consume_callback verify + session cookie pre-check.

Chạy: python3 test/check_session_callback_verify.py
"""
from __future__ import annotations

import ast
import py_compile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "session_phase.py"


def main() -> int:
    if not TARGET.exists():
        print(f"[FAIL] file not found: {TARGET}", flush=True)
        return 1

    try:
        py_compile.compile(str(TARGET), doraise=True)
    except py_compile.PyCompileError as exc:
        print(f"[FAIL] compile: {exc}", flush=True)
        return 1
    print("[PASS] compile ok", flush=True)

    src = TARGET.read_text(encoding="utf-8")

    # Helpers mới phải tồn tại.
    expectations = [
        ("def _has_session_cookie", "_has_session_cookie helper"),
        ("def _consume_callback_verified", "_consume_callback_verified helper"),
        ("__Secure-next-auth.session-token", "session token cookie name"),
        ("__Secure-next-auth.session-token.0", "split cookie .0"),
        ("_consume_callback_verified(callback_url)", "verified call"),
        ("if not _has_session_cookie()", "pre-check before /api/auth/session"),
        ("WARNING_BANNER", "warning banner detection"),
        ("only_warning", "warning-only branch"),
        ("không được set", "Vietnamese error message"),
    ]
    for needle, label in expectations:
        if needle not in src:
            print(f"[FAIL] thiếu {label!r}: {needle!r}", flush=True)
            return 1
        print(f"[PASS] {label}", flush=True)

    # Chuỗi cũ không nên còn (đã thay bằng verified version).
    # _consume_callback (no _verified) vẫn được dùng INSIDE helper. Nhưng caller chính
    # phải dùng _consume_callback_verified.
    if src.count("_consume_callback(session, callback_url, log)") != 1:
        # Cho phép đúng 1 lần ở trong helper _consume_callback_verified
        count = src.count("_consume_callback(session, callback_url, log)")
        print(
            f"[WARN] _consume_callback raw call count = {count} "
            f"(kỳ vọng = 1, chỉ ở trong helper)",
            flush=True,
        )

    # AST: get_session_pure_request có chứa _has_session_cookie và _consume_callback_verified
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        print(f"[FAIL] AST parse: {exc}", flush=True)
        return 1

    found_helpers = {"_has_session_cookie": False, "_consume_callback_verified": False}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "get_session_pure_request":
            for sub in ast.walk(node):
                if isinstance(sub, ast.FunctionDef) and sub.name in found_helpers:
                    found_helpers[sub.name] = True
    for name, ok in found_helpers.items():
        if not ok:
            print(f"[FAIL] {name} không tìm thấy trong get_session_pure_request", flush=True)
            return 1
        print(f"[PASS] {name} defined inside get_session_pure_request", flush=True)

    print("[PASS] all checks ok", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
