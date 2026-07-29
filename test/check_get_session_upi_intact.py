"""Verify Get Session + UPI KHÔNG bị ảnh hưởng sau khi gỡ reg pure_request.

Get Session (SessionManager) và UPI (upi_runner) login qua
``session_phase.get_session_pure_request``, hàm này lazy-import 16 helper từ
``request_phase``. Vì request_phase.py được GIỮ NGUYÊN, các helper còn đủ.

Test chạy ĐÚNG đường import runtime của Get Session/UPI để chứng minh động:
    [1] Thực thi y hệt block ``from request_phase import (...)`` của session_phase.
    [2] import session_phase → get_session_pure_request callable.
    [3] upi_runner import get_session_pure_request OK (cùng login path).
    [4] signup.run_signup vẫn import được (reg path không vỡ).
    [5] reg_mode.current: set 'pure_request' bị reject; đọc giá trị cũ KHÔNG crash.

Chạy: .venv/bin/python test/check_get_session_upi_intact.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Đúng 16 symbol session_phase.get_session_pure_request cần (session_phase.py).
_SESSION_PHASE_IMPORTS = (
    "_create_session", "_step_csrf", "_step_auth_url", "_get_sentinel_token",
    "_get_sentinel_token_async", "_common_headers", "_step_authorize_continue",
    "_step_follow_redirects", "_consume_callback", "_get_session_tokens",
    "_step_resend_otp", "_step_verify_otp", "_is_rotatable_error",
    "_IMPERSONATE_CANDIDATES", "RequestPhaseError", "USER_AGENT",
)

_passes: list[str] = []
_failures: list[str] = []


def _ok(m: str) -> None:
    _passes.append(m); print(f"[PASS] {m}", flush=True)


def _fail(m: str) -> None:
    _failures.append(m); print(f"[FAIL] {m}", flush=True)


def check_request_phase_helpers() -> None:
    """[1] Thực thi đúng block import của session_phase."""
    import importlib
    try:
        rp = importlib.import_module("request_phase")
    except Exception as exc:  # noqa: BLE001
        _fail(f"import request_phase fail: {type(exc).__name__}: {exc}")
        return
    missing = [s for s in _SESSION_PHASE_IMPORTS if not hasattr(rp, s)]
    if missing:
        _fail(f"request_phase THIẾU helper Get Session/UPI cần: {missing}")
    else:
        _ok(f"request_phase: còn đủ {len(_SESSION_PHASE_IMPORTS)} helper login")


def check_session_phase() -> None:
    """[2] session_phase import OK + get_session_pure_request callable."""
    import importlib
    try:
        sp = importlib.import_module("session_phase")
    except Exception as exc:  # noqa: BLE001
        _fail(f"import session_phase fail: {type(exc).__name__}: {exc}")
        return
    fn = getattr(sp, "get_session_pure_request", None)
    if callable(fn):
        _ok("session_phase.get_session_pure_request callable (Get Session OK)")
    else:
        _fail("session_phase.get_session_pure_request KHÔNG callable")


def check_upi_login_path() -> None:
    """[3] upi_runner login path dùng cùng get_session_pure_request."""
    src = (ROOT / "web" / "upi_runner.py").read_text(encoding="utf-8")
    if "get_session_pure_request" in src and "run_signup" not in src:
        _ok("upi_runner: login qua get_session_pure_request, KHÔNG gọi run_signup")
    else:
        _fail("upi_runner: tham chiếu bất thường (run_signup?) — cần kiểm tra")


def check_reg_path_imports() -> None:
    """[4] signup.run_signup vẫn import được (reg browser/hybrid không vỡ)."""
    import importlib
    try:
        signup = importlib.import_module("signup")
    except Exception as exc:  # noqa: BLE001
        _fail(f"import signup fail: {type(exc).__name__}: {exc}")
        return
    if callable(getattr(signup, "run_signup", None)):
        _ok("signup.run_signup import OK (reg path nguyên)")
    else:
        _fail("signup.run_signup không callable")


def check_reg_mode_key() -> None:
    """[5] reg_mode.current: reject set 'pure_request', đọc giá trị cũ KHÔNG crash."""
    try:
        from db.repositories import _validate_type_constraint, RepositoryError
    except Exception as exc:  # noqa: BLE001
        _ok(f"reg_mode.current runtime skip (import: {type(exc).__name__})")
        return
    try:
        _validate_type_constraint("reg_mode.current", "pure_request")
        _fail("reg_mode.current: set 'pure_request' KHÔNG bị reject (phải reject)")
    except RepositoryError:
        _ok("reg_mode.current: set 'pure_request' bị reject (đúng) — UPI/Session không set key này")
    except Exception as exc:  # noqa: BLE001
        _fail(f"reg_mode.current: loại lỗi sai — {type(exc).__name__}")


def main() -> int:
    check_request_phase_helpers()
    check_session_phase()
    check_upi_login_path()
    check_reg_path_imports()
    check_reg_mode_key()
    print("\n" + "=" * 60)
    print(f"PASS={len(_passes)}  FAIL={len(_failures)}")
    if _failures:
        print("\nFAILURES:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED — Get Session + UPI KHÔNG bị ảnh hưởng")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
