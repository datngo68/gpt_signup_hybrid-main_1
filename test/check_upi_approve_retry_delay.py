#!/usr/bin/env python3
"""Verify wiring `upi.approve_retry_delay` từ Settings Store → runtime.

Test cases:
    [TC-01] Whitelist key `upi.approve_retry_delay` trong _EXACT_KEYS.
    [TC-02] Type constraint nhận float 5.0-60.0, reject 4.9 / 60.1 / bool / str.
    [TC-03] UpiJobManager có property + setter + apply_settings hydrate.
    [TC-04] `run_upi_qr_probe` accept tham số `approve_retry_delay` keyword.
    [TC-05] Pydantic SetUpiConfigRequest accept field, validate range.
    [TC-06] AST: route `/api/upi/config` POST có write-through key vào
            settings_writes.
    [TC-07] AST: approve loop trong upi_runner dùng `effective_approve_delay`
            (không còn dùng module APPROVE_DELAY trực tiếp ở 3 chỗ).
    [TC-08] Frontend HTML có `<input id="upi-approve-retry-delay">`.
    [TC-09] Frontend JS bind dom.approveRetryDelay + change handler.

Chạy: python3 test/check_upi_approve_retry_delay.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _ok(tc: str, label: str, detail: str = "") -> None:
    msg = f"[PASS] {tc} — {label}"
    if detail:
        msg += f" :: {detail}"
    print(msg, flush=True)


def _fail(tc: str, label: str, detail: str = "") -> None:
    msg = f"[FAIL] {tc} — {label}"
    if detail:
        msg += f" :: {detail}"
    print(msg, flush=True)


def t01_whitelist():
    print("[1/9] TC-01 — whitelist key upi.approve_retry_delay", flush=True)
    src = (ROOT / "db" / "repositories.py").read_text(encoding="utf-8")
    if '"upi.approve_retry_delay"' not in src:
        _fail("TC-01", "thiếu key trong _EXACT_KEYS")
        return False
    _ok("TC-01", "key có trong whitelist")
    return True


def t02_type_constraint():
    print("[2/9] TC-02 — type constraint range [5, 60]", flush=True)
    from db.repositories import _validate_type_constraint, RepositoryError

    # Accept
    _validate_type_constraint("upi.approve_retry_delay", 5)
    _validate_type_constraint("upi.approve_retry_delay", 5.0)
    _validate_type_constraint("upi.approve_retry_delay", 30)
    _validate_type_constraint("upi.approve_retry_delay", 60)
    _validate_type_constraint("upi.approve_retry_delay", 60.0)
    _ok("TC-02", "accept 5, 5.0, 30, 60, 60.0")

    # Reject
    rejects = [
        (4, "below floor 5"),
        (4.999, "below floor 5 (float)"),
        (61, "above ceiling 60"),
        (60.0001, "above ceiling 60 (float)"),
        (True, "bool not number"),
        ("5", "str not number"),
    ]
    for val, label in rejects:
        try:
            _validate_type_constraint("upi.approve_retry_delay", val)
        except RepositoryError:
            _ok("TC-02", f"reject {val!r} ({label})")
            continue
        _fail("TC-02", f"expected reject {val!r} ({label})")
        return False
    return True


def t03_manager_hydration():
    print("[3/9] TC-03 — UpiJobManager property + setter + apply_settings", flush=True)
    import asyncio as _asyncio
    from web.manager import UpiJobManager

    async def _run() -> bool:
        mgr = UpiJobManager()
        # Default = 5.0
        if mgr.approve_retry_delay != 5.0:
            _fail("TC-03", f"default expect 5.0, got {mgr.approve_retry_delay!r}")
            return False
        _ok("TC-03", "default approve_retry_delay = 5.0")

        # Setter accept
        mgr.set_approve_retry_delay(5)
        mgr.set_approve_retry_delay(60)
        mgr.set_approve_retry_delay(7.5)
        if mgr.approve_retry_delay != 7.5:
            _fail("TC-03", f"setter not persisted, got {mgr.approve_retry_delay!r}")
            return False
        _ok("TC-03", "setter persists")

        # Setter reject
        for bad in (4, 4.99, 61, 100):
            try:
                mgr.set_approve_retry_delay(bad)
            except ValueError:
                continue
            _fail("TC-03", f"setter accepted out-of-range {bad}")
            return False
        _ok("TC-03", "setter reject out-of-range")

        # Hydrate
        mgr2 = UpiJobManager()
        mgr2.apply_settings({"upi.approve_retry_delay": 12.0})
        if mgr2.approve_retry_delay != 12.0:
            _fail("TC-03", f"apply_settings expect 12.0, got {mgr2.approve_retry_delay!r}")
            return False
        _ok("TC-03", "apply_settings hydrate OK")
        mgr.shutdown()
        mgr2.shutdown()
        return True

    return _asyncio.run(_run())


def t04_runner_signature():
    print("[4/9] TC-04 — run_upi_qr_probe accept approve_retry_delay kwarg", flush=True)
    import inspect
    from web.upi_runner import run_upi_qr_probe

    sig = inspect.signature(run_upi_qr_probe)
    if "approve_retry_delay" not in sig.parameters:
        _fail("TC-04", "thiếu kwarg approve_retry_delay")
        return False
    p = sig.parameters["approve_retry_delay"]
    if p.kind != inspect.Parameter.KEYWORD_ONLY:
        _fail("TC-04", f"expect KEYWORD_ONLY, got {p.kind}")
        return False
    _ok("TC-04", "kwarg approve_retry_delay defined as keyword-only")
    return True


def t05_pydantic_accept():
    print("[5/9] TC-05 — SetUpiConfigRequest field validation", flush=True)
    from web.server import SetUpiConfigRequest

    req = SetUpiConfigRequest(approve_retry_delay=5)
    if req.approve_retry_delay != 5:
        _fail("TC-05", f"accept 5 expect 5, got {req.approve_retry_delay!r}")
        return False
    req = SetUpiConfigRequest(approve_retry_delay=60)
    if req.approve_retry_delay != 60:
        _fail("TC-05", f"accept 60 expect 60, got {req.approve_retry_delay!r}")
        return False
    req = SetUpiConfigRequest(approve_retry_delay=7.5)
    if req.approve_retry_delay != 7.5:
        _fail("TC-05", f"accept 7.5 expect 7.5, got {req.approve_retry_delay!r}")
        return False
    _ok("TC-05", "accept 5 / 60 / 7.5")

    # Reject
    for bad in (4, 4.99, 61, 100):
        try:
            SetUpiConfigRequest(approve_retry_delay=bad)
        except Exception:
            continue
        _fail("TC-05", f"expected reject {bad}")
        return False
    _ok("TC-05", "reject out-of-range 4 / 4.99 / 61 / 100")
    return True


def t06_endpoint_writethrough():
    print("[6/9] TC-06 — /api/upi/config POST write-through key", flush=True)
    src = (ROOT / "web" / "server.py").read_text(encoding="utf-8")
    if 'settings_writes["upi.approve_retry_delay"]' not in src:
        _fail("TC-06", "không write-through upi.approve_retry_delay vào settings_writes")
        return False
    _ok("TC-06", "endpoint write-through OK")
    return True


def t07_runner_uses_effective_delay():
    print("[7/9] TC-07 — upi_runner dùng effective_approve_delay (no APPROVE_DELAY constant)", flush=True)
    src = (ROOT / "web" / "upi_runner.py").read_text(encoding="utf-8")

    # 3 chỗ trước đây dùng APPROVE_DELAY phải đã đổi thành effective_approve_delay.
    expectations = [
        ('await asyncio.sleep(effective_approve_delay)', "approve loop sleep"),
        ('"delay", f"{effective_approve_delay:g}s"', "config log line"),
        ('delay={effective_approve_delay:g}s', "loop start log line"),
        ('approve_retry_delay: float | None = None', "kwarg signature"),
        ('effective_approve_delay: float = float(APPROVE_DELAY)', "fallback to module default when None"),
        ('5.0 <= effective_approve_delay <= 60.0', "boundary validation"),
    ]
    failed = False
    for snippet, label in expectations:
        if snippet not in src:
            _fail("TC-07", f"missing: {label}", repr(snippet)[:80])
            failed = True
        else:
            _ok("TC-07", label)

    # Module default APPROVE_DELAY phải = 5.0 (an toàn hơn 3.0 cũ)
    if "APPROVE_DELAY: float = 5.0" not in src:
        _fail("TC-07", "APPROVE_DELAY default chưa update lên 5.0")
        failed = True
    else:
        _ok("TC-07", "APPROVE_DELAY module constant = 5.0 (safe default)")
    return not failed


def t08_html_input_field():
    print("[8/9] TC-08 — HTML có input upi-approve-retry-delay", flush=True)
    src = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
    expectations = [
        ('id="upi-approve-retry-delay"', "input id"),
        ('min="5"', "min attribute"),
        ('max="60"', "max attribute"),
        ('Retry delay', "label text"),
    ]
    failed = False
    for snippet, label in expectations:
        if snippet not in src:
            _fail("TC-08", f"missing: {label}")
            failed = True
        else:
            _ok("TC-08", label)
    return not failed


def t09_js_wires():
    print("[9/9] TC-09 — JS dom + change handler + snapshot", flush=True)
    src = (ROOT / "web" / "static" / "upi.js").read_text(encoding="utf-8")
    expectations = [
        ("approveRetryDelay: $('upi-approve-retry-delay')", "DOM bind"),
        ("dom.approveRetryDelay.addEventListener", "change handler"),
        ("approve_retry_delay: approveRetryDelay", "btnRun POST body"),
        ("snap.approve_retry_delay", "snapshot read"),
        ("cfg.approve_retry_delay", "initial load read"),
    ]
    failed = False
    for snippet, label in expectations:
        if snippet not in src:
            _fail("TC-09", f"missing: {label}")
            failed = True
        else:
            _ok("TC-09", label)
    return not failed


def main():
    cases = [
        ("TC-01 whitelist key", t01_whitelist),
        ("TC-02 type constraint range", t02_type_constraint),
        ("TC-03 UpiJobManager hydration", t03_manager_hydration),
        ("TC-04 runner signature", t04_runner_signature),
        ("TC-05 pydantic accept", t05_pydantic_accept),
        ("TC-06 endpoint write-through", t06_endpoint_writethrough),
        ("TC-07 runner uses effective_approve_delay", t07_runner_uses_effective_delay),
        ("TC-08 HTML input field", t08_html_input_field),
        ("TC-09 JS wires", t09_js_wires),
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
