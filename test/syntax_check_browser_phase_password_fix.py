"""Syntax + smoke check cho browser_phase.py sau fix password-set hardening.

Verify (theo thứ tự, log từng dòng [PASS]/[FAIL]):
  TC-01  py_compile (parse OK)
  TC-02  _detect_screen có timeout 3000ms (không phải 800ms cũ) trên /email-verification
  TC-03  _drive_signup_flow khai báo register_succeeded + force_pwd_goto_count + _FORCE_PWD_GOTO_MAX
  TC-04  password_create branch set register_succeeded=True khi status==200
  TC-05  screen=="otp" branch có guard pre-register: force goto /create-account/password
  TC-06  branch chatgpt + about_you fail-fast khi không qua register POST
  TC-07  _verify_account_session helper tồn tại + được call ở chatgpt/about_you/handle_login

Chạy: .venv/bin/python test/syntax_check_browser_phase_password_fix.py
"""
from __future__ import annotations

import py_compile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "browser_phase.py"

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
        _fail(f"TC-00 target file missing: {TARGET}")
        return 1

    src = TARGET.read_text(encoding="utf-8")

    # TC-01 py_compile
    print("[1/7] TC-01 py_compile browser_phase.py", flush=True)
    try:
        py_compile.compile(str(TARGET), doraise=True)
        _ok("TC-01 py_compile OK")
    except py_compile.PyCompileError as exc:
        _fail(f"TC-01 py_compile fail: {exc}")
        return 1

    # TC-02 detection timeout 3000ms
    print("[2/7] TC-02 _detect_screen password button timeout 3000ms", flush=True)
    needle = "if await pwd_btn.is_visible(timeout=3000):"
    if needle in src:
        _ok("TC-02 timeout 3000ms cho password button (đã tăng từ 800ms)")
    else:
        _fail(f"TC-02 không thấy needle {needle!r} — timeout chưa tăng")

    # Verify narrow vs broad: narrow /email-verification check phải dùng 3000ms,
    # broad fallback auth.openai.com vẫn được phép 800ms (đã bump từ 300ms).
    # Scan line-based: chỉ check khối từ `if "/email-verification"` đến trước
    # `# Broad check` — phải KHÔNG có timeout=800/300.
    lines = src.splitlines()
    narrow_lines = []
    in_narrow = False
    for line in lines:
        if 'if "/email-verification" in cur or "/email-otp" in cur or "/identifier"' in line:
            in_narrow = True
            narrow_lines.append(line)
            continue
        if in_narrow:
            if "# Broad check" in line:
                break
            narrow_lines.append(line)
    narrow_section = "\n".join(narrow_lines)
    if not narrow_section:
        _fail("TC-02b không locate được khối narrow check")
    elif "timeout=800)" in narrow_section or "timeout=300)" in narrow_section:
        _fail(f"TC-02b narrow /email-verification check vẫn có timeout=800/300 cũ. Section:\n{narrow_section}")
    else:
        _ok(f"TC-02b narrow /email-verification check không còn timeout cũ ({len(narrow_lines)} lines)")

    # TC-03 flags khai báo
    print("[3/7] TC-03 _drive_signup_flow flags", flush=True)
    for needle, desc in (
        ("register_succeeded = False  # True chỉ sau POST", "register_succeeded flag"),
        ("force_pwd_goto_count = 0", "force_pwd_goto_count counter"),
        ("_FORCE_PWD_GOTO_MAX = 3", "_FORCE_PWD_GOTO_MAX = 3"),
    ):
        if needle in src:
            _ok(f"TC-03 {desc}")
        else:
            _fail(f"TC-03 thiếu {desc} (needle={needle!r})")

    # TC-04 register_succeeded=True khi 200
    print("[4/7] TC-04 password_create set register_succeeded=True", flush=True)
    if "register_succeeded = True\n                log(\"[flow] register OK (HTTP 200)" in src:
        _ok("TC-04 status==200 → register_succeeded=True trước log")
    else:
        _fail("TC-04 chưa set register_succeeded=True khi register POST trả 200")

    # TC-05 OTP-before-register guard
    print("[5/7] TC-05 OTP-before-register guard", flush=True)
    for needle, desc in (
        ("if not register_attempted and \"auth.openai.com\" in page.url:", "guard condition pre-register OTP"),
        ("https://auth.openai.com/create-account/password", "force goto /create-account/password URL"),
        ("if force_pwd_goto_count < _FORCE_PWD_GOTO_MAX:", "force goto quota check"),
    ):
        if needle in src:
            _ok(f"TC-05 {desc}")
        else:
            _fail(f"TC-05 thiếu {desc} (needle={needle!r})")

    # TC-06 fail-fast chatgpt + about_you
    print("[6/7] TC-06 fail-fast no-password policy", flush=True)
    fail_fast_needle = "password chưa được set: flow"
    cnt = src.count(fail_fast_needle)
    if cnt >= 2:
        _ok(f"TC-06 fail-fast policy gắn ở ≥2 chỗ ({cnt} occurrences) — chatgpt + about_you")
    else:
        _fail(f"TC-06 fail-fast policy chỉ gắn {cnt} chỗ (cần ≥2 cho chatgpt + about_you)")

    # TC-07 _verify_account_session
    print("[7/7] TC-07 _verify_account_session helper", flush=True)
    if "async def _verify_account_session(" in src:
        _ok("TC-07 _verify_account_session khai báo")
    else:
        _fail("TC-07 _verify_account_session chưa khai báo")

    verify_calls = src.count("await _verify_account_session(ctx, page, log=log)")
    if verify_calls >= 3:
        _ok(f"TC-07 _verify_account_session được call {verify_calls} lần (≥3: chatgpt, about_you, handle_login + login chatgpt branch)")
    else:
        _fail(f"TC-07 _verify_account_session chỉ call {verify_calls} lần (cần ≥3)")

    # Verify body shape — fetch /api/auth/session JS snippet
    if "/api/auth/session" in src and "fetch('/api/auth/session'" in src:
        _ok("TC-07 fetch('/api/auth/session') có trong helper")
    else:
        _fail("TC-07 helper thiếu fetch('/api/auth/session')")

    print(f"\n=== TỔNG KẾT: {PASS} PASS, {FAIL} FAIL ===", flush=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
