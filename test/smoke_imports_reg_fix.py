#!/usr/bin/env python3
"""Smoke import các module đã sửa cho fix reg pure-request.

Verify import OK + symbol mới tồn tại + CLI signup có flag --reg-mode.
Chạy: python3 test/smoke_imports_reg_fix.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    failures = 0

    # [1/4] request_phase import + symbol mới
    try:
        import request_phase as rp
        assert hasattr(rp, "_consume_callback")
        assert hasattr(rp, "_read_session_token_cookie")
        assert hasattr(rp, "_get_session_tokens")
        print("[PASS] [1/4] request_phase import + _read_session_token_cookie tồn tại", flush=True)
    except Exception as exc:
        print(f"[FAIL] [1/4] request_phase :: {type(exc).__name__}: {exc}", flush=True)
        failures += 1

    # [2/4] session_phase import (reuse _consume_callback)
    try:
        import session_phase  # noqa: F401
        print("[PASS] [2/4] session_phase import OK", flush=True)
    except Exception as exc:
        print(f"[FAIL] [2/4] session_phase :: {type(exc).__name__}: {exc}", flush=True)
        failures += 1

    # [3/4] cli import + signup command có param reg_mode
    try:
        import inspect
        import cli
        sig = inspect.signature(cli.signup_cmd)
        assert "reg_mode" in sig.parameters, "signup_cmd thiếu param reg_mode"
        print("[PASS] [3/4] cli.signup_cmd có param reg_mode", flush=True)
    except Exception as exc:
        print(f"[FAIL] [3/4] cli :: {type(exc).__name__}: {exc}", flush=True)
        failures += 1

    # [4/4] SignupRequest accept reg_mode=pure_request
    try:
        from models import SignupRequest
        req = SignupRequest(email="x@hotmail.com", reg_mode="pure_request")
        assert req.reg_mode == "pure_request"
        print("[PASS] [4/4] SignupRequest(reg_mode='pure_request') OK", flush=True)
    except Exception as exc:
        print(f"[FAIL] [4/4] SignupRequest :: {type(exc).__name__}: {exc}", flush=True)
        failures += 1

    print(f"\n[summary] failures={failures}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
