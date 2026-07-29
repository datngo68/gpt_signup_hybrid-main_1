"""py_compile check để xác nhận file biên dịch được (bao gồm bytecode).

Chạy: python3 test/check_session_phase_compile.py
"""
from __future__ import annotations

import py_compile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = [
    ROOT / "session_phase.py",
    ROOT / "request_phase.py",
    ROOT / "web" / "upi_runner.py",
]


def main() -> int:
    for idx, target in enumerate(TARGETS, start=1):
        prefix = f"[{idx}/{len(TARGETS)}]"
        if not target.exists():
            print(f"{prefix} [FAIL] file not found: {target}", flush=True)
            return 1
        try:
            py_compile.compile(str(target), doraise=True)
        except py_compile.PyCompileError as exc:
            print(f"{prefix} [FAIL] compile error in {target.name}: {exc}", flush=True)
            return 1
        print(f"{prefix} [PASS] compile ok — {target.name}", flush=True)
    print("[PASS] all targets compile clean", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
