"""Syntax + import sanity check cho fix get_session_pure_request.

Chạy: python3 test/check_session_phase_syntax.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "session_phase.py"


def main() -> int:
    if not TARGET.exists():
        print(f"[FAIL] file not found: {TARGET}", flush=True)
        return 1

    src = TARGET.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(TARGET))
    except SyntaxError as exc:
        print(f"[FAIL] syntax error: {exc}", flush=True)
        return 1
    print(f"[PASS] AST parse — {TARGET.name}", flush=True)

    # Verify get_session_pure_request có _do_bootstrap helper sau khi refactor.
    found_pure = False
    found_helper = False
    found_detect = False
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "get_session_pure_request":
            found_pure = True
            for sub in ast.walk(node):
                if isinstance(sub, ast.FunctionDef):
                    if sub.name == "_do_bootstrap":
                        found_helper = True
                    elif sub.name == "_detect_flow_from_landing":
                        found_detect = True

    if not found_pure:
        print("[FAIL] get_session_pure_request không tìm thấy", flush=True)
        return 1
    print("[PASS] get_session_pure_request defined", flush=True)

    if not found_helper:
        print("[FAIL] _do_bootstrap helper không tìm thấy", flush=True)
        return 1
    print("[PASS] _do_bootstrap helper defined", flush=True)

    if not found_detect:
        print("[FAIL] _detect_flow_from_landing helper không tìm thấy", flush=True)
        return 1
    print("[PASS] _detect_flow_from_landing helper defined", flush=True)

    # Đảm bảo chuỗi quan trọng tồn tại
    expectations = [
        ("re-bootstrap KHÔNG login_hint", "fallback comment"),
        ("HTTP 409", "409 detection"),
        ("invalid_state", "invalid_state detection"),
        ("authorize/continue failed", "passthrough error message"),
    ]
    for snippet, label in expectations:
        if snippet not in src:
            print(f"[FAIL] thiếu {label!r} ({snippet!r})", flush=True)
            return 1
        print(f"[PASS] có chuỗi {label}", flush=True)

    print("[PASS] all checks ok", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
