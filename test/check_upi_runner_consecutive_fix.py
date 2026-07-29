"""Verify fix backend_exception threshold → consecutive trong upi_runner.py.

Chạy: python3 test/check_upi_runner_consecutive_fix.py
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

    # 1. Compile.
    try:
        py_compile.compile(str(TARGET), doraise=True)
    except py_compile.PyCompileError as exc:
        print(f"[FAIL] compile: {exc}", flush=True)
        return 1
    print("[PASS] compile ok", flush=True)

    src = TARGET.read_text(encoding="utf-8")

    # 2. Tên cũ phải bị xoá hoàn toàn (tránh shadow/typo).
    if "APPROVE_BACKEND_EXCEPTION_FAILS" in src:
        print("[FAIL] tên cũ APPROVE_BACKEND_EXCEPTION_FAILS vẫn còn trong upi_runner.py", flush=True)
        return 1
    print("[PASS] đã xoá hết tên cũ APPROVE_BACKEND_EXCEPTION_FAILS", flush=True)

    # 3. Tên mới + value default = 0 (DISABLED — backend_exception không
    # bao giờ fatal-break; loop chỉ dừng khi approved/hết retry).
    expected_constant = "APPROVE_BACKEND_EXCEPTION_CONSECUTIVE: int = 0"
    if expected_constant not in src:
        print(f"[FAIL] thiếu constant {expected_constant!r}", flush=True)
        return 1
    print("[PASS] APPROVE_BACKEND_EXCEPTION_CONSECUTIVE = 0 (disabled by default)", flush=True)

    # 4. Logic consecutive: phải có biến + reset block.
    expectations = [
        ("consecutive_backend_exception = 0", "init biến consecutive"),
        ("consecutive_backend_exception += 1", "increment trên backend_exception"),
        ("consecutive_backend_exception = 0", "reset on non-exception"),
        ("APPROVE_BACKEND_EXCEPTION_CONSECUTIVE > 0", "respect disable=0"),
        ("consecutive backend_exception threshold", "log message threshold"),
        ("reset consec be_excpt", "log message reset"),
    ]
    for needle, label in expectations:
        count = src.count(needle)
        if count < 1:
            print(f"[FAIL] thiếu {label!r}: {needle!r}", flush=True)
            return 1
        print(f"[PASS] {label} (occurrences={count})", flush=True)

    # 5. AST: backend_exception_count vẫn dùng làm tổng (cho stats).
    if "backend_exception_count += 1" not in src:
        print("[FAIL] backend_exception_count tổng không còn được increment", flush=True)
        return 1
    print("[PASS] backend_exception_count tổng vẫn được track", flush=True)

    # 6. Đảm bảo import test cũ đã update.
    test_imports = ROOT / "test" / "check_upi_module_imports.py"
    if test_imports.exists():
        t_src = test_imports.read_text(encoding="utf-8")
        if "APPROVE_BACKEND_EXCEPTION_FAILS" in t_src:
            print(f"[FAIL] test cũ vẫn import tên cũ: {test_imports}", flush=True)
            return 1
        if "APPROVE_BACKEND_EXCEPTION_CONSECUTIVE" not in t_src:
            print(f"[FAIL] test cũ chưa import tên mới: {test_imports}", flush=True)
            return 1
        print("[PASS] test/check_upi_module_imports.py đã sync tên mới", flush=True)

    print("[PASS] all checks ok", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
