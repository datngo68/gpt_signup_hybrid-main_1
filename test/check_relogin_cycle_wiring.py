#!/usr/bin/env python3
"""Verify wiring re-login cycle: pure helper truth-table + db constraint +
manager setting + run_upi_qr_probe giữ nguyên (không bị đụng signature).

Chạy: .venv/bin/python test/check_relogin_cycle_wiring.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJ = ROOT.parent
sys.path.insert(0, str(PROJ))
PKG = "gpt_signup_hybrid_new"

rc = 0


def ok(idx: str, msg: str) -> None:
    print(f"[PASS] {idx} — {msg}", flush=True)


def fail(idx: str, msg: str) -> None:
    global rc
    rc = 1
    print(f"[FAIL] {idx} — {msg}", flush=True)


# ── TC-01: pure helper cycle_all_blocked truth table ──────────────────
def tc01() -> None:
    from importlib import import_module
    ur = import_module(f"{PKG}.web.upi_runner")
    cab = ur.cycle_all_blocked
    cases = [
        ("empty", [], False),
        ("all blocked", [{"result": "blocked"}, {"result": "blocked"}], True),
        ("all 403", [{"http_status": 403}, {"http_status": 429}], True),
        ("mixed approved", [{"result": "blocked"}, {"ok": True, "result": "approved"}], False),
        ("has exception", [{"result": "blocked"}, {"http_status": 200, "result": "exception"}], False),
        ("has network", [{"result": "blocked"}, {"http_status": None}], False),
        ("blocked+403", [{"result": "blocked"}, {"http_status": 403}], True),
    ]
    for name, attempts, expected in cases:
        got = cab(attempts)
        if got == expected:
            ok("TC-01", f"cycle_all_blocked '{name}' = {got}")
        else:
            fail("TC-01", f"'{name}' expected {expected} got {got}")


# ── TC-02: db constraint upi.max_outer_cycles [1,5] ───────────────────
def tc02() -> None:
    from importlib import import_module
    repo = import_module(f"{PKG}.db.repositories")
    validate = repo._validate_type_constraint
    keys = repo._EXACT_KEYS
    if "upi.max_outer_cycles" in keys:
        ok("TC-02", "upi.max_outer_cycles trong _EXACT_KEYS")
    else:
        fail("TC-02", "thiếu key trong _EXACT_KEYS")
    # accept 1..5
    for v in (1, 3, 5):
        try:
            validate("upi.max_outer_cycles", v)
            ok("TC-02", f"accept {v}")
        except Exception as e:  # noqa: BLE001
            fail("TC-02", f"reject hợp lệ {v}: {e}")
    # reject 0, 6, bool
    for v in (0, 6, True, 2.5, "3"):
        try:
            validate("upi.max_outer_cycles", v)
            fail("TC-02", f"KHÔNG reject invalid {v!r}")
        except Exception:  # noqa: BLE001
            ok("TC-02", f"reject invalid {v!r}")


# ── TC-03: manager setting + clamp + apply_settings ───────────────────
def tc03() -> None:
    from importlib import import_module
    try:
        mgr = import_module(f"{PKG}.web.manager")
    except Exception as e:  # noqa: BLE001
        fail("TC-03", f"import manager fail (deps?): {type(e).__name__}: {e}")
        return
    um = mgr.UpiJobManager(max_concurrent=1)
    if um.max_outer_cycles == 1:
        ok("TC-03", "default max_outer_cycles = 1 (behavior cũ)")
    else:
        fail("TC-03", f"default != 1: {um.max_outer_cycles}")
    um.set_max_outer_cycles(3)
    if um.max_outer_cycles == 3:
        ok("TC-03", "set_max_outer_cycles(3) OK")
    else:
        fail("TC-03", "setter không cập nhật")
    for bad in (0, 6):
        try:
            um.set_max_outer_cycles(bad)
            fail("TC-03", f"setter KHÔNG raise với {bad}")
        except ValueError:
            ok("TC-03", f"setter raise ValueError với {bad}")
    um.apply_settings({"upi.max_outer_cycles": 4})
    if um.max_outer_cycles == 4:
        ok("TC-03", "apply_settings hydrate = 4")
    else:
        fail("TC-03", f"apply_settings không hydrate: {um.max_outer_cycles}")
    # helper tồn tại
    if hasattr(um, "_run_upi_cycles"):
        ok("TC-03", "_run_upi_cycles method tồn tại")
    else:
        fail("TC-03", "thiếu _run_upi_cycles")


# ── TC-04: run_upi_qr_probe signature KHÔNG đổi (không regress lõi) ────
def tc04() -> None:
    import inspect
    from importlib import import_module
    ur = import_module(f"{PKG}.web.upi_runner")
    sig = inspect.signature(ur.run_upi_qr_probe)
    params = set(sig.parameters)
    must = {"email", "password", "secret", "proxy_pool", "approve_retries",
            "qr_out_path", "log", "restart_threshold", "max_restarts",
            "proxy_from_step", "approve_retry_delay", "auth_sink"}
    missing = must - params
    if not missing:
        ok("TC-04", "run_upi_qr_probe giữ đủ param lõi (không regress)")
    else:
        fail("TC-04", f"thiếu param: {missing}")


if __name__ == "__main__":
    for i, fn in enumerate((tc01, tc02, tc03, tc04), 1):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            import traceback
            fail(f"TC-0{i}", f"crash: {e}\n{traceback.format_exc()}")
    print("=== DONE ===", flush=True)
    sys.exit(rc)
