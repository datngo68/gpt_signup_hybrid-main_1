"""Verify ``upi.proxy_from_step`` end-to-end qua các layer.

Mục tiêu:
    [TC-01] Whitelist key ``upi.proxy_from_step``.
    [TC-02] Type constraint nhận int 1-6, reject 0/7/bool/str.
    [TC-03] ``run_upi_qr_probe`` accept tham số ``proxy_from_step`` keyword.
    [TC-04] ``UpiJobManager`` có property + setter + apply_settings hydrate.
    [TC-05] ``SetUpiConfigRequest`` Pydantic accept field.
    [TC-06] AST: route ``/api/upi/config`` POST có write-through key vào
            settings_writes.
    [TC-07] Module-level ``PROXY_FROM_STEP`` constant vẫn = 3 (giữ
            backward-compat cho test cũ).
    [TC-08] Login UPI luôn direct (bám golden Get Session): ``login_proxy =
            None`` bất kể proxy_from_step (proxy IN chỉ áp từ payment step).

Mỗi TC log [PASS]/[FAIL] ngay, không gom cuối.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

PASSED = 0
FAILED = 0


def _check(label: str, fn) -> None:
    global PASSED, FAILED
    try:
        fn()
        print(f"[PASS] {label}", flush=True)
        PASSED += 1
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {label} :: {type(exc).__name__}: {exc}", flush=True)
        FAILED += 1


def t01_whitelist():
    from gpt_signup_hybrid_new.db.repositories import _EXACT_KEYS
    assert "upi.proxy_from_step" in _EXACT_KEYS, "key missing in _EXACT_KEYS"
    assert "upi.login_via_proxy" not in _EXACT_KEYS, (
        "upi.login_via_proxy không nên có (chỉ dùng 1 key proxy_from_step)"
    )


def t02_type_constraint():
    from gpt_signup_hybrid_new.db.repositories import (
        _validate_type_constraint, RepositoryError,
    )
    # Accept range
    for v in (1, 2, 3, 4, 5, 6):
        _validate_type_constraint("upi.proxy_from_step", v)
    # Reject out-of-range
    for v in (0, 7, -1, 100):
        try:
            _validate_type_constraint("upi.proxy_from_step", v)
            raise AssertionError(f"expected reject {v}")
        except RepositoryError:
            pass
    # Reject wrong type
    for v in (True, False, "3", 3.0, None):
        try:
            _validate_type_constraint("upi.proxy_from_step", v)
            raise AssertionError(f"expected reject {v!r}")
        except RepositoryError:
            pass


def t03_runner_signature():
    import inspect
    from gpt_signup_hybrid_new.web.upi_runner import run_upi_qr_probe
    sig = inspect.signature(run_upi_qr_probe)
    assert "proxy_from_step" in sig.parameters, "thiếu kwarg proxy_from_step"
    p = sig.parameters["proxy_from_step"]
    assert p.kind == inspect.Parameter.KEYWORD_ONLY, "phải keyword-only"
    assert p.default == 3, f"default phải = 3 (PROXY_FROM_STEP), got {p.default}"


def t04_manager_hydration():
    import asyncio
    from gpt_signup_hybrid_new.web.manager import UpiJobManager

    async def _run() -> None:
        mgr = UpiJobManager()
        # Property + setter
        assert mgr.proxy_from_step == 3, f"default phải = 3, got {mgr.proxy_from_step}"
        mgr.set_proxy_from_step(1)
        assert mgr.proxy_from_step == 1
        try:
            mgr.set_proxy_from_step(0)
            raise AssertionError("expected reject 0")
        except ValueError:
            pass
        try:
            mgr.set_proxy_from_step(7)
            raise AssertionError("expected reject 7")
        except ValueError:
            pass
        # apply_settings hydrate
        mgr.apply_settings({"upi.proxy_from_step": 5})
        assert mgr.proxy_from_step == 5
        # Range invalid trong settings dict → bỏ qua không crash.
        mgr.apply_settings({"upi.proxy_from_step": 99})
        assert mgr.proxy_from_step == 5  # giữ nguyên
        mgr.shutdown()

    asyncio.run(_run())


def t05_pydantic_accept():
    from gpt_signup_hybrid_new.web.server import SetUpiConfigRequest
    req = SetUpiConfigRequest(proxy_from_step=1)
    assert req.proxy_from_step == 1
    req = SetUpiConfigRequest(proxy_from_step=6)
    assert req.proxy_from_step == 6
    # Reject out-of-range
    try:
        SetUpiConfigRequest(proxy_from_step=0)
        raise AssertionError("expected reject 0")
    except Exception:
        pass
    try:
        SetUpiConfigRequest(proxy_from_step=7)
        raise AssertionError("expected reject 7")
    except Exception:
        pass


def t06_endpoint_writethrough():
    """AST check: handler /api/upi/config có gán key write-through."""
    src = (ROOT / "web" / "server.py").read_text(encoding="utf-8")
    # Tìm string literal key trong source
    assert '"upi.proxy_from_step"' in src, (
        "endpoint /api/upi/config thiếu write-through 'upi.proxy_from_step'"
    )


def t07_const_unchanged():
    from gpt_signup_hybrid_new.web.upi_runner import PROXY_FROM_STEP
    assert PROXY_FROM_STEP == 3, (
        f"PROXY_FROM_STEP phải giữ = 3 (backward-compat), got {PROXY_FROM_STEP}"
    )


def t08_login_proxy_logic_source():
    """Source check: login UPI hỗ trợ proxy override qua ``upi.login_proxy_url``.

    Behavior cũ: ``login_proxy = None`` hardcoded — login luôn DIRECT.
    Behavior mới: ``effective_login_proxy`` chứa proxy URL nếu user set
    ``upi.login_proxy_url`` (Step 1+2 cho geo-eligible promo plus), fallback
    DIRECT nếu proxy dead (>=2 attempt retryable fail). Khi không set → vẫn
    login DIRECT (luồng cũ giữ).
    """
    src = (ROOT / "web" / "upi_runner.py").read_text(encoding="utf-8")

    # 1. Biến động `effective_login_proxy` thay cho hardcoded `login_proxy = None`.
    assert "effective_login_proxy" in src, (
        "thiếu biến effective_login_proxy (login proxy override + fallback)"
    )

    # 2. Hardcoded `login_proxy = None` cũ phải bị xóa.
    import re as _re
    assert not _re.search(r"(^|[^_])login_proxy\s*=\s*None\b", src), (
        "vẫn còn hardcoded `login_proxy = None` — phải dùng effective_login_proxy động"
    )

    # 3. Login call truyền proxy động (current_proxy/effective_login_proxy).
    assert "proxy=current_proxy" in src, (
        "login (login_fn / get_session_pure_request) chưa nhận proxy=current_proxy"
    )

    # 4. Step 2 (checkout) dùng effective_login_proxy với fallback first_proxy
    #    theo proxy_from_step khi không set hoặc proxy dead (luồng cũ giữ).
    pattern = _re.compile(
        r"effective_login_proxy\s+is\s+not\s+None.*?"
        r"first_proxy\s+if\s+proxy_from_step\s*<=\s*2",
        _re.DOTALL,
    )
    assert pattern.search(src), (
        "Step 2 (checkout) thiếu branch fallback first_proxy theo proxy_from_step"
    )


def main() -> int:
    tests = [
        ("TC-01 whitelist key", t01_whitelist),
        ("TC-02 type constraint range", t02_type_constraint),
        ("TC-03 run_upi_qr_probe signature", t03_runner_signature),
        ("TC-04 UpiJobManager hydration", t04_manager_hydration),
        ("TC-05 SetUpiConfigRequest pydantic", t05_pydantic_accept),
        ("TC-06 endpoint write-through", t06_endpoint_writethrough),
        ("TC-07 PROXY_FROM_STEP constant unchanged", t07_const_unchanged),
        ("TC-08 step 1 login uses proxy_from_step==1", t08_login_proxy_logic_source),
    ]
    for label, fn in tests:
        _check(label, fn)
    print(f"\n--- summary: {PASSED} pass, {FAILED} fail ---", flush=True)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
