#!/usr/bin/env python3
"""Verify default behavior "restart on every exception" + UI exposure.

Yêu cầu user:
    "bị exception thì phải get link payment lại chạy lại luôn và ko được
     reset bộ đếm retry"

Test cases:
    [TC-01] Default `restart_threshold = 1` (mỗi exception là 1 restart).
    [TC-02] Default `max_restarts = 500` (≈ approve_retries → không cạn).
    [TC-03] `set_max_restarts` accept up to 2000 (mở rộng từ 100).
    [TC-04] `_validate_type_constraint` cho upi.approve.max_restarts accept
            tới 2000, reject 2001.
    [TC-05] Pydantic SetUpiConfigRequest.max_restarts ge=0 le=2000.
    [TC-06] AST: approve_index_total KHÔNG bị reset khi triggered_restart=True
            (giữ counter cộng dồn — design hiện tại đã đúng, verify regression
            không phá).
    [TC-07] HTML có 2 input mới: upi-restart-threshold + upi-max-restarts.
    [TC-08] JS bind dom.restartThreshold + dom.maxRestarts + change handler.
    [TC-09] /api/upi/config GET expose restart_threshold + max_restarts.

Chạy: python3 test/check_upi_restart_default_1.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _ok(tc, label, detail=""):
    msg = f"[PASS] {tc} — {label}"
    if detail:
        msg += f" :: {detail}"
    print(msg, flush=True)


def _fail(tc, label, detail=""):
    msg = f"[FAIL] {tc} — {label}"
    if detail:
        msg += f" :: {detail}"
    print(msg, flush=True)


def t01_default_restart_threshold_1():
    print("[1/9] TC-01 — default restart_threshold == 1", flush=True)
    from web.manager import _DEFAULT_UPI_RESTART_THRESHOLD, UpiJobManager
    if _DEFAULT_UPI_RESTART_THRESHOLD != 1:
        _fail("TC-01", f"default expect 1, got {_DEFAULT_UPI_RESTART_THRESHOLD}")
        return False
    _ok("TC-01", f"_DEFAULT_UPI_RESTART_THRESHOLD = {_DEFAULT_UPI_RESTART_THRESHOLD}")
    mgr = UpiJobManager()
    if mgr.restart_threshold != 1:
        _fail("TC-01", f"new manager restart_threshold expect 1, got {mgr.restart_threshold}")
        mgr.shutdown()
        return False
    _ok("TC-01", "fresh UpiJobManager().restart_threshold = 1")
    mgr.shutdown()
    return True


def t02_default_max_restarts_500():
    print("[2/9] TC-02 — default max_restarts == 500", flush=True)
    from web.manager import _DEFAULT_UPI_MAX_RESTARTS, UpiJobManager
    if _DEFAULT_UPI_MAX_RESTARTS != 500:
        _fail("TC-02", f"default expect 500, got {_DEFAULT_UPI_MAX_RESTARTS}")
        return False
    _ok("TC-02", f"_DEFAULT_UPI_MAX_RESTARTS = {_DEFAULT_UPI_MAX_RESTARTS}")
    mgr = UpiJobManager()
    if mgr.max_restarts != 500:
        _fail("TC-02", f"new manager max_restarts expect 500, got {mgr.max_restarts}")
        mgr.shutdown()
        return False
    _ok("TC-02", "fresh UpiJobManager().max_restarts = 500")
    mgr.shutdown()
    return True


def t03_setter_accept_2000():
    print("[3/9] TC-03 — set_max_restarts accept tới 2000", flush=True)
    from web.manager import UpiJobManager
    mgr = UpiJobManager()
    try:
        mgr.set_max_restarts(0)
        mgr.set_max_restarts(2000)
        _ok("TC-03", "accept 0 và 2000")
    except ValueError as exc:
        _fail("TC-03", "rejected valid", str(exc))
        mgr.shutdown()
        return False
    try:
        mgr.set_max_restarts(2001)
    except ValueError:
        _ok("TC-03", "reject 2001")
        mgr.shutdown()
        return True
    _fail("TC-03", "accepted 2001 — ceiling chưa mở")
    mgr.shutdown()
    return False


def t04_repository_validation_2000():
    print("[4/9] TC-04 — repository validate upi.approve.max_restarts ceiling 2000", flush=True)
    from db.repositories import _validate_type_constraint, RepositoryError
    _validate_type_constraint("upi.approve.max_restarts", 0)
    _validate_type_constraint("upi.approve.max_restarts", 500)
    _validate_type_constraint("upi.approve.max_restarts", 2000)
    _ok("TC-04", "accept 0, 500, 2000")
    try:
        _validate_type_constraint("upi.approve.max_restarts", 2001)
    except RepositoryError:
        _ok("TC-04", "reject 2001")
        return True
    _fail("TC-04", "accepted 2001")
    return False


def t05_pydantic_max_restarts_2000():
    print("[5/9] TC-05 — Pydantic SetUpiConfigRequest.max_restarts ge=0 le=2000", flush=True)
    from web.server import SetUpiConfigRequest
    req = SetUpiConfigRequest(max_restarts=0)
    assert req.max_restarts == 0
    req = SetUpiConfigRequest(max_restarts=2000)
    assert req.max_restarts == 2000
    _ok("TC-05", "accept 0, 2000")
    try:
        SetUpiConfigRequest(max_restarts=2001)
    except Exception:
        _ok("TC-05", "reject 2001")
        return True
    _fail("TC-05", "accepted 2001")
    return False


def t06_approve_index_not_reset_on_restart():
    print("[6/9] TC-06 — approve_index_total KHÔNG reset khi restart", flush=True)
    src = (ROOT / "web" / "upi_runner.py").read_text(encoding="utf-8")
    if "approve_index_total = 0    # cumulative qua phase — KHÔNG reset" not in src:
        _fail("TC-06", "thiếu khai báo `approve_index_total cumulative`")
        return False
    _ok("TC-06", "biến approve_index_total có comment KHÔNG reset")

    # AST scan: trong while (restart loop) KHÔNG được có `approve_index_total = 0`.
    tree = ast.parse(src)
    bad: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "approve_index_total":
                    val = node.value
                    if isinstance(val, ast.Constant) and val.value == 0:
                        bad.append(node.lineno)
    if len(bad) > 1:
        # Cho phép DUY NHẤT 1 lần init = 0 (trước restart loop).
        _fail("TC-06", f"approve_index_total = 0 xuất hiện {len(bad)} chỗ tại lines {bad} — có chỗ reset!")
        return False
    _ok("TC-06", f"approve_index_total = 0 chỉ xuất hiện 1 lần (init), tại line {bad[0] if bad else '?'}")

    # Verify trigger restart break — không reset counter sau break.
    if "approve_idx kept at" not in src:
        _fail("TC-06", "thiếu log `approve_idx kept at` xác nhận giữ counter")
        return False
    _ok("TC-06", "log `approve_idx kept at <N>/<retries>` confirm")
    return True


def t07_html_inputs():
    print("[7/9] TC-07 — HTML có upi-restart-threshold + upi-max-restarts", flush=True)
    src = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
    expectations = [
        ('id="upi-restart-threshold"', "restart-threshold input id"),
        ('id="upi-max-restarts"', "max-restarts input id"),
        ('Restart threshold', "Restart threshold label"),
        ('Max restarts', "Max restarts label"),
    ]
    failed = False
    for s, lbl in expectations:
        if s not in src:
            _fail("TC-07", f"missing: {lbl}")
            failed = True
        else:
            _ok("TC-07", lbl)
    return not failed


def t08_js_wires():
    print("[8/9] TC-08 — JS dom + change handler + payload + snapshot", flush=True)
    src = (ROOT / "web" / "static" / "upi.js").read_text(encoding="utf-8")
    expectations = [
        ("restartThreshold: $('upi-restart-threshold')", "DOM bind restartThreshold"),
        ("maxRestarts: $('upi-max-restarts')", "DOM bind maxRestarts"),
        ("dom.restartThreshold.addEventListener", "change handler restartThreshold"),
        ("dom.maxRestarts.addEventListener", "change handler maxRestarts"),
        ("restart_threshold: isNaN(restartThreshold)", "btnRun POST restart_threshold"),
        ("max_restarts: isNaN(maxRestarts)", "btnRun POST max_restarts"),
        ("snap.restart_threshold", "snapshot read restart_threshold"),
        ("snap.max_restarts", "snapshot read max_restarts"),
        ("cfg.restart_threshold", "initial load read restart_threshold"),
        ("cfg.max_restarts", "initial load read max_restarts"),
    ]
    failed = False
    for s, lbl in expectations:
        if s not in src:
            _fail("TC-08", f"missing: {lbl}")
            failed = True
        else:
            _ok("TC-08", lbl)
    return not failed


def t09_get_config_exposes():
    print("[9/9] TC-09 — GET /api/upi/config expose restart_threshold + max_restarts", flush=True)
    src = (ROOT / "web" / "server.py").read_text(encoding="utf-8")
    # Đếm số lần "restart_threshold": um.restart_threshold xuất hiện — phải >= 4
    # (SSE register, list_upi_jobs, set_upi_config response, get_upi_config).
    occ_threshold = src.count('"restart_threshold": um.restart_threshold')
    occ_max = src.count('"max_restarts": um.max_restarts')
    if occ_threshold < 4:
        _fail("TC-09", f"restart_threshold expose < 4 chỗ (got {occ_threshold})")
        return False
    if occ_max < 4:
        _fail("TC-09", f"max_restarts expose < 4 chỗ (got {occ_max})")
        return False
    _ok("TC-09", f"restart_threshold xuất hiện {occ_threshold}x; max_restarts {occ_max}x")
    return True


def main():
    cases = [
        ("TC-01 default restart_threshold=1", t01_default_restart_threshold_1),
        ("TC-02 default max_restarts=500", t02_default_max_restarts_500),
        ("TC-03 setter accept 2000", t03_setter_accept_2000),
        ("TC-04 repository validation 2000", t04_repository_validation_2000),
        ("TC-05 pydantic max_restarts 2000", t05_pydantic_max_restarts_2000),
        ("TC-06 approve_index_total no reset", t06_approve_index_not_reset_on_restart),
        ("TC-07 HTML inputs", t07_html_inputs),
        ("TC-08 JS wires", t08_js_wires),
        ("TC-09 GET config exposes", t09_get_config_exposes),
    ]
    failures = 0
    for name, fn in cases:
        try:
            ok = fn()
        except Exception as exc:  # noqa: BLE001
            _fail(name, "raised", repr(exc))
            ok = False
        if not ok:
            failures += 1
        print("", flush=True)
    total = len(cases)
    print(f"[summary] passed={total - failures}/{total}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
