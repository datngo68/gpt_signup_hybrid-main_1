"""Verify fix login retry trong upi_runner.

Chạy: python3 test/check_login_retry.py
"""
from __future__ import annotations

import ast
import py_compile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "web" / "upi_runner.py"


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

    expectations = [
        ("from session_phase import SessionError, get_session_pure_request", "import SessionError (absolute)"),
        ("LOGIN_MAX_ATTEMPTS = 3", "constant max attempts"),
        ("LOGIN_RETRY_DELAY = 3.0", "constant delay"),
        ("NON_RETRYABLE_PATTERNS", "non-retryable list"),
        ("password verify failed", "non-retryable: wrong password"),
        ("mfa verify failed", "non-retryable: MFA"),
        ("no mail_provider available", "non-retryable: mail provider"),
        ("def _is_login_error_retryable", "retryable detector"),
        ("for login_attempt in range(1, LOGIN_MAX_ATTEMPTS + 1)", "retry loop"),
        ("except SessionError as exc", "catch SessionError"),
        ("await asyncio.sleep(LOGIN_RETRY_DELAY)", "sleep between retries"),
        ("login transient error", "log message transient"),
        ("login fail (non-retryable)", "log message non-retryable"),
        ("login OK ở attempt", "log message recovery"),
    ]
    for needle, label in expectations:
        if needle not in src:
            print(f"[FAIL] thiếu {label!r}: {needle!r}", flush=True)
            return 1
        print(f"[PASS] {label}", flush=True)

    # AST: đảm bảo retry loop nằm INSIDE run_upi_qr_probe.
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        print(f"[FAIL] AST parse: {exc}", flush=True)
        return 1

    found_retry_in_run = False
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_upi_qr_probe":
            for sub in ast.walk(node):
                if isinstance(sub, ast.For):
                    # Retry loop dùng range(1, LOGIN_MAX_ATTEMPTS + 1)
                    if isinstance(sub.iter, ast.Call) and getattr(sub.iter.func, "id", "") == "range":
                        # Check arg uses LOGIN_MAX_ATTEMPTS
                        ast_dump = ast.dump(sub.iter)
                        if "LOGIN_MAX_ATTEMPTS" in ast_dump:
                            found_retry_in_run = True
                            break
            break

    if not found_retry_in_run:
        print("[FAIL] retry loop không tìm thấy trong run_upi_qr_probe", flush=True)
        return 1
    print("[PASS] retry loop nằm trong run_upi_qr_probe", flush=True)

    print("[PASS] all checks ok", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
