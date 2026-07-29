"""Syntax check web/manager.py _NO_RETRY_ERROR_KEYS contains permanent signals.

User dùng UI manual reg (web/manager.py) — KHÔNG dùng autoreg/runner.py.
Bug: invalid_auth_step / user_already_exists trigger _maybe_auto_retry vô ích.

Verify:
  TC-01 py_compile web/manager.py
  TC-02 _NO_RETRY_ERROR_KEYS chứa cả 4 signal permanent:
        invalid_auth_step / user_already_exists / AccountAlreadyExistsError / đã được đăng ký
  TC-03 _is_fatal_error vẫn dùng _NO_RETRY_ERROR_KEYS (chưa bị disable)
  TC-04 _maybe_auto_retry methods (≥2 = signup Job + SessionJob/LinkJob) gọi _is_fatal_error

Chạy: .venv/bin/python test/syntax_check_web_manager_no_retry.py
"""
from __future__ import annotations

import py_compile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "web" / "manager.py"

FAIL = 0
PASS = 0


def _ok(msg: str) -> None:
    global PASS
    PASS += 1
    print(f"[PASS] {msg}", flush=True)


def _fail(msg: str) -> None:
    global FAIL
    FAIL += 1
    print(f"[FAIL] {msg}", flush=True)


def main() -> int:
    if not TARGET.exists():
        _fail(f"TC-00 target missing: {TARGET}")
        return 1

    # TC-01
    print("[1/4] TC-01 py_compile web/manager.py", flush=True)
    try:
        py_compile.compile(str(TARGET), doraise=True)
        _ok("TC-01 py_compile OK")
    except py_compile.PyCompileError as exc:
        _fail(f"TC-01 py_compile fail: {exc}")
        return 1

    src = TARGET.read_text(encoding="utf-8")

    # TC-02 signals
    print("[2/4] TC-02 _NO_RETRY_ERROR_KEYS contains permanent signals", flush=True)
    if "_NO_RETRY_ERROR_KEYS = (" not in src:
        _fail("TC-02 _NO_RETRY_ERROR_KEYS không tồn tại")
        return 1
    # Locate tuple block — line-based, find ")" at column 0 after opening line
    lines = src.splitlines()
    in_tuple = False
    tuple_lines: list[str] = []
    for line in lines:
        if "_NO_RETRY_ERROR_KEYS = (" in line:
            in_tuple = True
            tuple_lines.append(line)
            continue
        if in_tuple:
            tuple_lines.append(line)
            if line.strip() == ")":
                break
    tuple_block = "\n".join(tuple_lines)

    for sig in ("invalid_auth_step", "user_already_exists", "AccountAlreadyExistsError", "đã được đăng ký"):
        if f'"{sig}"' in tuple_block:
            _ok(f"TC-02 chứa signal {sig!r}")
        else:
            _fail(f"TC-02 thiếu signal {sig!r} trong _NO_RETRY_ERROR_KEYS")

    # TC-03 _is_fatal_error dùng _NO_RETRY_ERROR_KEYS
    print("[3/4] TC-03 _is_fatal_error wires _NO_RETRY_ERROR_KEYS", flush=True)
    if "any(k.lower() in error_lower for k in _NO_RETRY_ERROR_KEYS)" in src:
        _ok("TC-03 _is_fatal_error iterate _NO_RETRY_ERROR_KEYS")
    else:
        _fail("TC-03 _is_fatal_error không dùng _NO_RETRY_ERROR_KEYS")

    # TC-04 _maybe_auto_retry methods exist + call _is_fatal_error
    print("[4/4] TC-04 _maybe_auto_retry gates by _is_fatal_error", flush=True)
    method_count = src.count("async def _maybe_auto_retry(self, job:")
    if method_count >= 2:
        _ok(f"TC-04 có {method_count} _maybe_auto_retry methods (signup Job + Session/Link)")
    else:
        _fail(f"TC-04 chỉ {method_count} _maybe_auto_retry method (cần ≥2)")
    gate_count = src.count("if _is_fatal_error(job.error):")
    if gate_count >= 2:
        _ok(f"TC-04 _is_fatal_error gate ở {gate_count} chỗ (≥2 methods)")
    else:
        _fail(f"TC-04 _is_fatal_error gate chỉ ở {gate_count} chỗ (cần ≥2)")

    print(f"\n=== TỔNG KẾT: {PASS} PASS, {FAIL} FAIL ===", flush=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
