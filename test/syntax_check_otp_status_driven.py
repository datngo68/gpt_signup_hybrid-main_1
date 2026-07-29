"""Syntax + structure check cho refactor OTP status-driven (2026-06-28).

Verify (theo thứ tự):
  TC-01  py_compile browser_phase.py
  TC-02  _submit_otp return signature 3-tuple (continue_url, source, status)
  TC-03  _submit_otp_via_api return signature 2-tuple (continue_url, status), NO raise on 4xx
  TC-04  state vars: _otp_last_status, _otp_first_poll_wait_done, _otp_force_about_you_done
  TC-05  bỏ escalation flags cũ: _otp_reclick_done / _otp_js_submit_done / _otp_api_done
  TC-06  Step A: 4xx → click Resend + re-poll ngay
  TC-07  Step B: 200 stuck → force goto /about-you sau 15s, re-poll sau 30s
  TC-08  10s wait trước poll OTP lần đầu (gate qua _otp_first_poll_wait_done)
  TC-09  status=200 + no continue_url → manual goto /about-you
  TC-10  _handle_login_after_password fail-fast khi otp_status >= 400
  TC-11  Không còn re-click submit / JS form.submit / API resubmit cùng consumed code

Chạy: .venv/bin/python test/syntax_check_otp_status_driven.py
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
        _fail(f"TC-00 target missing: {TARGET}")
        return 1

    src = TARGET.read_text(encoding="utf-8")

    # TC-01
    print("[1/11] TC-01 py_compile", flush=True)
    try:
        py_compile.compile(str(TARGET), doraise=True)
        _ok("TC-01 py_compile OK")
    except py_compile.PyCompileError as exc:
        _fail(f"TC-01 py_compile fail: {exc}")
        return 1

    # TC-02 _submit_otp signature
    print("[2/11] TC-02 _submit_otp returns 3-tuple", flush=True)
    if "-> tuple[str | None, str, int]:" in src and "async def _submit_otp(" in src:
        _ok("TC-02 _submit_otp signature includes int (status)")
    else:
        _fail("TC-02 _submit_otp signature thiếu int (status)")

    # TC-03 _submit_otp_via_api signature
    print("[3/11] TC-03 _submit_otp_via_api returns 2-tuple", flush=True)
    if "async def _submit_otp_via_api(" in src and "-> tuple[str | None, int]:" in src:
        _ok("TC-03 _submit_otp_via_api signature 2-tuple")
    else:
        _fail("TC-03 _submit_otp_via_api signature chưa update")
    # KHÔNG còn raise BrowserPhaseError trên 4xx
    if 'raise BrowserPhaseError(\n            f"OTP validate API rejected:' in src:
        _fail("TC-03b _submit_otp_via_api vẫn raise trên 4xx — cần return status")
    else:
        _ok("TC-03b _submit_otp_via_api không raise trên 4xx")

    # TC-04 new state vars
    print("[4/11] TC-04 new state vars", flush=True)
    for needle, desc in (
        ("_otp_last_status: int = 0", "_otp_last_status"),
        ("_otp_first_poll_wait_done: bool = False", "_otp_first_poll_wait_done"),
        ("_otp_force_about_you_done: bool = False", "_otp_force_about_you_done"),
    ):
        if needle in src:
            _ok(f"TC-04 {desc}")
        else:
            _fail(f"TC-04 thiếu {desc}")

    # TC-05 old escalation flags removed
    print("[5/11] TC-05 escalation flags cũ đã xoá", flush=True)
    # `_otp_reclick_done = False` style — flag assignment đầu hàm
    bad_flags = []
    for needle in ("_otp_reclick_done", "_otp_js_submit_done", "_otp_api_done"):
        if f"{needle} = False" in src:
            bad_flags.append(needle)
    if bad_flags:
        _fail(f"TC-05 vẫn còn declare flags cũ: {bad_flags}")
    else:
        _ok("TC-05 flags escalation cũ đã xoá khỏi state init")

    # TC-06 Step A: 4xx handling
    print("[6/11] TC-06 Step A 4xx wrong code → resend + re-poll", flush=True)
    if 'if otp_submitted and _otp_last_status >= 400:' in src:
        _ok("TC-06 có guard otp_submitted + _otp_last_status >= 400")
    else:
        _fail("TC-06 chưa có Step A xử lý 4xx")
    if "clicked 'Resend email' (sau wrong code)" in src:
        _ok("TC-06 click Resend khi 4xx + no pending")
    else:
        _fail("TC-06 thiếu Resend khi 4xx")

    # TC-07 Step B: 200 stuck → force goto about_you
    print("[7/11] TC-07 Step B force goto /about-you", flush=True)
    if "force goto /about-you" in src:
        _ok("TC-07 có force goto /about-you fallback")
    else:
        _fail("TC-07 chưa có force goto /about-you")
    if "_otp_wait_elapsed > 15.0 and not _otp_force_about_you_done" in src:
        _ok("TC-07 gate 15s + once-only flag")
    else:
        _fail("TC-07 thiếu gate 15s force goto")

    # TC-08 wait 10s before first poll
    print("[8/11] TC-08 wait 10s trước poll OTP lần đầu", flush=True)
    if "_otp_first_poll_wait_done = True" in src and "asyncio.sleep(10.0)" in src:
        _ok("TC-08 wait 10s + set flag once")
    else:
        _fail("TC-08 thiếu wait 10s + flag")

    # TC-09 status=200 + no continue_url → goto /about-you
    print("[9/11] TC-09 status=200 no continue_url fallback", flush=True)
    if "OTP validated HTTP 200 nhưng body thiếu continue_url" in src:
        _ok("TC-09 có log + fallback goto /about-you khi 200 no continue_url")
    else:
        _fail("TC-09 thiếu fallback cho 200 + no continue_url")

    # TC-10 _handle_login_after_password fail-fast
    print("[10/11] TC-10 _handle_login_after_password fail-fast 4xx", flush=True)
    if 'if otp_status >= 400:' in src and "login OTP rejected HTTP" in src:
        _ok("TC-10 _handle_login_after_password fail-fast trên 4xx")
    else:
        _fail("TC-10 _handle_login_after_password chưa fail-fast 4xx")

    # TC-11 No more harmful resubmit logic
    print("[11/11] TC-11 không còn resubmit logic cũ trong _drive_signup_flow", flush=True)
    drive_start = src.find("async def _drive_signup_flow(")
    drive_section = src[drive_start:src.find("async def ", drive_start + 1)]
    # Check key removed strings
    bad_patterns = [
        ("thử click submit lại", "re-click escalation cũ"),
        ("thử form.submit() qua JS", "JS form.submit escalation cũ"),
        ("thử validate qua API", "API resubmit escalation cũ"),
        ("OTP stuck >35s — re-poll", "35s stuck escalation cũ"),
    ]
    for pat, desc in bad_patterns:
        if pat in drive_section:
            _fail(f"TC-11 vẫn còn {desc}: {pat!r}")
        else:
            _ok(f"TC-11 đã xoá {desc}")

    print(f"\n=== TỔNG KẾT: {PASS} PASS, {FAIL} FAIL ===", flush=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
