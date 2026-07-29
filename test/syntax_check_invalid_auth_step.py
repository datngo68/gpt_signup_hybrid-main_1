"""Syntax check fix invalid_auth_step (email đã đăng ký).

Verify:
  TC-01  browser_phase + autoreg/runner py_compile OK
  TC-02  browser_phase password_create branch detect status==400 +
         "invalid_auth_step" trong body → raise BrowserPhaseError với message
         rõ ràng "email đã được đăng ký"
  TC-03  Error message browser_phase chứa email + URL để debug
  TC-04  autoreg/runner skip retry khi error_msg chứa "invalid_auth_step"
         hoặc "đã được đăng ký" (cả nhánh ``not result.success`` lẫn
         ``except Exception``)
  TC-05  autoreg gọi _mark_email_failed sau khi detect (không retry)

Chạy: .venv/bin/python test/syntax_check_invalid_auth_step.py
"""
from __future__ import annotations

import py_compile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = (ROOT / "browser_phase.py", ROOT / "autoreg" / "runner.py")

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
    # TC-01 compile
    print("[1/5] TC-01 py_compile", flush=True)
    for t in TARGETS:
        if not t.exists():
            _fail(f"TC-01 target missing: {t}")
            continue
        try:
            py_compile.compile(str(t), doraise=True)
            _ok(f"TC-01 py_compile {t.name}")
        except py_compile.PyCompileError as exc:
            _fail(f"TC-01 py_compile {t.name} fail: {exc}")

    if FAIL:
        return 1

    bp_src = TARGETS[0].read_text(encoding="utf-8")
    ar_src = TARGETS[1].read_text(encoding="utf-8")

    # TC-02 detect status==400 + invalid_auth_step
    print("[2/5] TC-02 browser_phase detect invalid_auth_step", flush=True)
    if 'if status == 400 and "invalid_auth_step" in body_str.lower():' in bp_src:
        _ok("TC-02 detect status==400 + invalid_auth_step body")
    else:
        _fail("TC-02 chưa detect status==400 + invalid_auth_step")

    # TC-03 error message contains email + URL
    print("[3/5] TC-03 error message chi tiết", flush=True)
    needle = "email {request.email} đã được đăng ký"
    if needle in bp_src:
        _ok("TC-03 error message chứa email")
    else:
        _fail(f"TC-03 thiếu phrase {needle!r}")
    if "URL: {page.url}" in bp_src:
        _ok("TC-03 error message log URL để debug")
    else:
        _fail("TC-03 thiếu URL trong error message")

    # TC-04 autoreg skip retry — 2 chỗ
    print("[4/5] TC-04 autoreg skip retry", flush=True)
    skip_cnt = ar_src.count("any(sig in error_lower for sig in permanent_signals)")
    if skip_cnt >= 2:
        _ok(f"TC-04 autoreg skip retry detect ở {skip_cnt} chỗ (≥2: not-success + except branches)")
    else:
        _fail(f"TC-04 autoreg skip retry chỉ ở {skip_cnt} chỗ (cần ≥2)")
    # Verify permanent_signals tuple chứa cả 4 signals
    for sig in ("invalid_auth_step", "user_already_exists", "accountalreadyexistserror", "đã được đăng ký"):
        if f'"{sig}"' in ar_src:
            _ok(f"TC-04 permanent_signals chứa {sig!r}")
        else:
            _fail(f"TC-04 thiếu signal {sig!r} trong permanent_signals tuple")

    # TC-05 mark_email_failed sau detect
    print("[5/5] TC-05 mark email failed sau detect", flush=True)
    # Tìm pattern: detect permanent_signals → log → _mark_email_failed → return
    sections = ar_src.split("any(sig in error_lower for sig in permanent_signals)")
    if len(sections) >= 3:  # split tạo 3 parts cho 2 occurrences
        ok_count = 0
        for i, section in enumerate(sections[1:], 1):
            head = section[:500]
            if "_mark_email_failed(email)" in head and "return" in head:
                ok_count += 1
        if ok_count >= 2:
            _ok(f"TC-05 mark_email_failed + return ở {ok_count} chỗ")
        else:
            _fail(f"TC-05 chỉ {ok_count} chỗ có mark_email_failed + return (cần ≥2)")
    else:
        _fail("TC-05 không locate được sections để verify")

    print(f"\n=== TỔNG KẾT: {PASS} PASS, {FAIL} FAIL ===", flush=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
