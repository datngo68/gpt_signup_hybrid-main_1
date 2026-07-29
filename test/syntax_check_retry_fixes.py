"""Parse syntax + verify retry logic của 2 file vừa sửa.

- request_phase.py: kiểm tra block bootstrap+register có wrap trong for-loop
  retry HTTP 409 invalid_state.
- mfa_phase.py: kiểm tra _enroll_totp_with_retry tồn tại và enable_2fa gọi
  wrapper thay vì _enroll_totp trực tiếp.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _parse(path: Path) -> ast.Module:
    src = path.read_text(encoding="utf-8")
    return ast.parse(src, filename=str(path))


def check_request_phase() -> list[str]:
    errs: list[str] = []
    p = REPO_ROOT / "request_phase.py"
    src = p.read_text(encoding="utf-8")
    try:
        ast.parse(src, filename=str(p))
    except SyntaxError as e:
        errs.append(f"[FAIL] request_phase.py syntax: {e}")
        return errs

    must_have = [
        ("max_register_attempts = 3", "retry attempts constant"),
        ("for register_attempt in range(1, max_register_attempts + 1):", "retry for-loop"),
        ('"invalid_state" in body', "409 invalid_state detection"),
        ("Re-bootstrap mới", "re-bootstrap log message"),
        ("HTTP 409 invalid_state", "409 invalid_state log"),
        ("session.close()", "old session cleanup before re-bootstrap"),
        ("break  # success → exit retry loop", "break on success"),
        ('time.sleep(1.5)', "backoff before re-bootstrap"),
    ]
    for needle, desc in must_have:
        if needle not in src:
            errs.append(f"[FAIL] request_phase.py thiếu marker: {desc!r} ({needle!r})")
        else:
            print(f"[PASS] request_phase.py: {desc}", flush=True)
    return errs


def check_mfa_phase() -> list[str]:
    errs: list[str] = []
    p = REPO_ROOT / "mfa_phase.py"
    src = p.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(p))
    except SyntaxError as e:
        errs.append(f"[FAIL] mfa_phase.py syntax: {e}")
        return errs

    # Wrapper function tồn tại?
    func_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    if "_enroll_totp_with_retry" in func_names:
        print("[PASS] mfa_phase.py: _enroll_totp_with_retry async function defined", flush=True)
    else:
        errs.append("[FAIL] mfa_phase.py thiếu _enroll_totp_with_retry()")

    # enable_2fa gọi wrapper, không gọi _enroll_totp trực tiếp
    if "_enroll_totp_with_retry(" in src:
        print("[PASS] mfa_phase.py: có call _enroll_totp_with_retry(...)", flush=True)
    else:
        errs.append("[FAIL] mfa_phase.py KHÔNG call _enroll_totp_with_retry")

    # enable_2fa không còn call _enroll_totp(... raw nữa
    # → Đếm số call _enroll_totp( ngoài body của _enroll_totp_with_retry và
    # khỏi định nghĩa hàm. Cách đơn giản: chỉ còn 1 call (trong wrapper).
    raw_calls = src.count("await _enroll_totp(")
    if raw_calls == 1:
        print(f"[PASS] mfa_phase.py: chỉ còn 1 call await _enroll_totp(...) (trong wrapper)", flush=True)
    else:
        errs.append(
            f"[FAIL] mfa_phase.py có {raw_calls} call await _enroll_totp(...) — phải = 1"
        )

    # Conflict markers vẫn tồn tại + được tham chiếu trong wrapper
    if "_ENROLL_CONFLICT_MARKERS" in src and "any(m in msg for m in _ENROLL_CONFLICT_MARKERS)" in src:
        print("[PASS] mfa_phase.py: wrapper skip retry on conflict markers", flush=True)
    else:
        errs.append("[FAIL] mfa_phase.py wrapper không check _ENROLL_CONFLICT_MARKERS")

    if "max_attempts: int = 3" in src or "max_attempts=3" in src:
        print("[PASS] mfa_phase.py: max_attempts=3 ở wrapper", flush=True)
    else:
        errs.append("[FAIL] mfa_phase.py wrapper không có max_attempts=3")

    return errs


def main() -> int:
    errs: list[str] = []
    print("=== request_phase.py ===", flush=True)
    errs.extend(check_request_phase())
    print("=== mfa_phase.py ===", flush=True)
    errs.extend(check_mfa_phase())

    if errs:
        print("\n--- FAILURES ---", flush=True)
        for e in errs:
            print(e, flush=True)
        return 1
    print("\nALL CHECKS PASSED", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
