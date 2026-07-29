#!/usr/bin/env python3
"""Verify 3 cải tiến: block-streak early-break, gộp attempts, login_flow ở Settings tab.

Chạy: .venv/bin/python test/check_relogin_improvements.py
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJ = ROOT.parent
sys.path.insert(0, str(PROJ))
PKG = "gpt_signup_hybrid_new"

rc = 0


def ok(idx, msg):
    print(f"[PASS] {idx} — {msg}", flush=True)


def fail(idx, msg):
    global rc
    rc = 1
    print(f"[FAIL] {idx} — {msg}", flush=True)


# TC-01: db key + constraint relogin_block_streak [0,1000]
def tc01():
    from importlib import import_module
    repo = import_module(f"{PKG}.db.repositories")
    if "upi.relogin_block_streak" in repo._EXACT_KEYS:
        ok("TC-01", "upi.relogin_block_streak trong _EXACT_KEYS")
    else:
        fail("TC-01", "thiếu key")
    # max_outer_cycles constraint vẫn còn (không bị xóa nhầm)
    if "upi.max_outer_cycles" in repo._EXACT_KEYS:
        ok("TC-01", "upi.max_outer_cycles vẫn whitelisted")
    else:
        fail("TC-01", "max_outer_cycles bị mất")
    for v in (0, 12, 1000):
        try:
            repo._validate_type_constraint("upi.relogin_block_streak", v)
            ok("TC-01", f"accept {v}")
        except Exception as e:
            fail("TC-01", f"reject hợp lệ {v}: {e}")
    for v in (-1, 1001, True, 2.5):
        try:
            repo._validate_type_constraint("upi.relogin_block_streak", v)
            fail("TC-01", f"KHÔNG reject {v!r}")
        except Exception:
            ok("TC-01", f"reject {v!r}")
    # max_outer_cycles constraint vẫn hoạt động (reject 6)
    try:
        repo._validate_type_constraint("upi.max_outer_cycles", 6)
        fail("TC-01", "max_outer_cycles KHÔNG reject 6 (constraint mất?)")
    except Exception:
        ok("TC-01", "max_outer_cycles constraint còn nguyên (reject 6)")


# TC-02: manager wiring
def tc02():
    from importlib import import_module
    mgr = import_module(f"{PKG}.web.manager")
    um = mgr.UpiJobManager(max_concurrent=1)
    if um.relogin_block_streak == 12:
        ok("TC-02", "default relogin_block_streak = 12")
    else:
        fail("TC-02", f"default != 12: {um.relogin_block_streak}")
    um.set_relogin_block_streak(20)
    if um.relogin_block_streak == 20:
        ok("TC-02", "setter OK")
    else:
        fail("TC-02", "setter fail")
    for bad in (-1, 1001):
        try:
            um.set_relogin_block_streak(bad)
            fail("TC-02", f"setter KHÔNG raise {bad}")
        except ValueError:
            ok("TC-02", f"setter raise {bad}")
    um.apply_settings({"upi.relogin_block_streak": 8})
    if um.relogin_block_streak == 8:
        ok("TC-02", "apply_settings hydrate = 8")
    else:
        fail("TC-02", f"apply_settings fail: {um.relogin_block_streak}")


# TC-03: signature + field
def tc03():
    from importlib import import_module
    ur = import_module(f"{PKG}.web.upi_runner")
    sig = inspect.signature(ur.run_upi_qr_probe)
    if "relogin_block_streak" in sig.parameters:
        ok("TC-03", "run_upi_qr_probe có param relogin_block_streak")
    else:
        fail("TC-03", "thiếu param")
    if sig.parameters["relogin_block_streak"].default == 0:
        ok("TC-03", "default param = 0 (off — single-cycle không đổi)")
    else:
        fail("TC-03", "default param != 0")
    fields = {f.name for f in __import__("dataclasses").fields(ur.UpiQrResult)}
    if "relogin_requested" in fields:
        ok("TC-03", "UpiQrResult có field relogin_requested")
    else:
        fail("TC-03", "thiếu field relogin_requested")


# TC-04: source markers
def tc04():
    runner = (ROOT / "web" / "upi_runner.py").read_text(encoding="utf-8")
    manager = (ROOT / "web" / "manager.py").read_text(encoding="utf-8")
    checks = [
        (runner, "block-streak", "detector block-streak trong runner"),
        (runner, "relogin_requested = True", "set cờ relogin_requested"),
        (runner, "relogin_block_streak > 0", "gate threshold>0"),
        (manager, "self._relogin_block_streak if max_cycles > 1 else 0",
         "manager gate block_streak theo max_cycles>1"),
        (manager, "all_approve.extend", "gộp approve_attempts qua cycle"),
        (manager, 'setdefault("cycle"', "tag cycle cho attempts"),
        (manager, "result.approve_attempts = all_approve", "gán accumulated lên result"),
    ]
    for src, marker, desc in checks:
        if marker in src:
            ok("TC-04", desc)
        else:
            fail("TC-04", f"thiếu: {desc} ({marker})")


# TC-05: login_flow đã chuyển sang Settings tab (settings_panel.js), gỡ khỏi UPI
def tc05():
    idx = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
    upijs = (ROOT / "web" / "static" / "upi.js").read_text(encoding="utf-8")
    spjs = (ROOT / "web" / "static" / "settings_panel.js").read_text(encoding="utf-8")
    if "upi-login-flow" not in idx:
        ok("TC-05", "đã gỡ select login_flow khỏi UPI row")
    else:
        fail("TC-05", "vẫn còn upi-login-flow trong index.html")
    if "login-flow-select" in idx:
        ok("TC-05", "có login-flow-select trong Settings tab")
    else:
        fail("TC-05", "thiếu login-flow-select ở Settings")
    if "loginFlow" not in upijs:
        ok("TC-05", "upi.js đã bỏ wiring login_flow")
    else:
        fail("TC-05", "upi.js vẫn còn loginFlow")
    if "saveLoginFlow" in spjs and "session.login_flow" in spjs:
        ok("TC-05", "settings_panel.js wire login_flow (save + endpoint)")
    else:
        fail("TC-05", "settings_panel.js thiếu wiring login_flow")


if __name__ == "__main__":
    for i, fn in enumerate((tc01, tc02, tc03, tc04, tc05), 1):
        try:
            fn()
        except Exception as e:
            import traceback
            fail(f"TC-0{i}", f"crash: {e}\n{traceback.format_exc()}")
    print("=== DONE ===", flush=True)
    sys.exit(rc)
