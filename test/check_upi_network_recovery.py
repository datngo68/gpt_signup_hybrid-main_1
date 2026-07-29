"""Verify helpers network recovery + classifier mới (timeout/network).

Chạy: python3 test/check_upi_network_recovery.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJ = ROOT.parent
sys.path.insert(0, str(PROJ))


def _ok(idx: str, label: str, detail: str = "") -> None:
    print(f"[PASS] {idx} — {label} :: {detail}", flush=True)


def _fail(idx: str, label: str, detail: str) -> None:
    print(f"[FAIL] {idx} — {label} :: {detail}", flush=True)


def t01_constants() -> bool:
    from gpt_signup_hybrid_new.web.upi_runner import (
        NETWORK_FAIL_DETECT,
        NETWORK_PROBE_TIMEOUT_SECONDS,
        NETWORK_RECOVERY_MAX_WAIT_SECONDS,
        NETWORK_RECOVERY_POLL_SECONDS,
    )
    assert NETWORK_FAIL_DETECT == 3, NETWORK_FAIL_DETECT
    assert NETWORK_RECOVERY_POLL_SECONDS == 5.0
    assert NETWORK_RECOVERY_MAX_WAIT_SECONDS == 600.0
    assert NETWORK_PROBE_TIMEOUT_SECONDS == 5.0
    _ok("TC-01", "constants", "fail_detect=3 poll=5s max=600s probe_to=5s")
    return True


def t02_classifier() -> bool:
    """_is_network_error vs _is_backend_exception."""
    from gpt_signup_hybrid_new.web.upi_runner import (
        _is_backend_exception,
        _is_network_error,
    )

    cases = [
        # (attempt, is_net, is_be)
        ({"http_status": None, "result": None, "error_type": "Timeout"}, True, False),
        ({"http_status": None, "result": None, "error_type": "ConnectionError"}, True, False),
        ({"http_status": 200, "result": "exception"}, False, True),
        ({"http_status": 200, "result": "approved"}, False, False),
        ({"http_status": 200, "result": "blocked"}, False, False),
        ({"http_status": 403, "result": None}, False, False),
        ({"http_status": 502, "result": None}, False, False),
    ]
    for i, (att, expect_net, expect_be) in enumerate(cases):
        if _is_network_error(att) != expect_net:
            _fail("TC-02", f"is_network_error case {i}", f"{att} expected {expect_net}")
            return False
        if _is_backend_exception(att) != expect_be:
            _fail("TC-02", f"is_backend_exception case {i}", f"{att} expected {expect_be}")
            return False
    _ok("TC-02", "classifier",
        "network_error/backend_exception nhận diện đúng 7 cases")
    return True


def t03_probe_signature() -> bool:
    """_probe_connectivity và _wait_network_recovery có signature đúng."""
    import inspect

    from gpt_signup_hybrid_new.web.upi_runner import (
        _probe_connectivity,
        _wait_network_recovery,
    )

    if not inspect.iscoroutinefunction(_probe_connectivity):
        _fail("TC-03", "probe coroutine", "not a coroutine function")
        return False
    if not inspect.iscoroutinefunction(_wait_network_recovery):
        _fail("TC-03", "recovery coroutine", "not a coroutine function")
        return False
    sig = inspect.signature(_wait_network_recovery)
    params = list(sig.parameters)
    if params != ["sess", "log"]:
        _fail("TC-03", "recovery params", f"got {params}")
        return False
    _ok("TC-03", "helper signatures", "coroutine + đúng params")
    return True


def t04_recovery_max_wait() -> bool:
    """_wait_network_recovery trả False trong thời gian hợp lý nếu mạng down.

    Mock sess.head luôn raise → buộc recovery loop poll cho tới khi vượt
    max_wait. Để chạy nhanh, monkey-patch constants xuống mức nhỏ.
    """
    from gpt_signup_hybrid_new.web import upi_runner

    class _DummyResp:
        status_code = 200

    class _SessAlwaysFail:
        async def head(self, *a, **kw):
            raise OSError("network unreachable (simulated)")

    class _SessAlwaysOK:
        async def head(self, *a, **kw):
            return _DummyResp()

    original_max = upi_runner.NETWORK_RECOVERY_MAX_WAIT_SECONDS
    original_poll = upi_runner.NETWORK_RECOVERY_POLL_SECONDS
    try:
        upi_runner.NETWORK_RECOVERY_MAX_WAIT_SECONDS = 0.5
        upi_runner.NETWORK_RECOVERY_POLL_SECONDS = 0.1

        logs: list[str] = []
        log = logs.append

        # Case 1: always fail → False sau ~0.5s
        result = asyncio.run(upi_runner._wait_network_recovery(_SessAlwaysFail(), log))
        if result is not False:
            _fail("TC-04", "recovery timeout", f"expected False got {result}")
            return False
        if not any("không recover" in m for m in logs):
            _fail("TC-04", "fail log", "thiếu log 'không recover'")
            return False

        # Case 2: always OK → True ngay lần probe đầu
        logs.clear()
        result = asyncio.run(upi_runner._wait_network_recovery(_SessAlwaysOK(), log))
        if result is not True:
            _fail("TC-04", "recovery success", f"expected True got {result}")
            return False
        if not any("recovered" in m for m in logs):
            _fail("TC-04", "ok log", "thiếu log 'recovered'")
            return False

    finally:
        upi_runner.NETWORK_RECOVERY_MAX_WAIT_SECONDS = original_max
        upi_runner.NETWORK_RECOVERY_POLL_SECONDS = original_poll

    _ok("TC-04", "_wait_network_recovery",
        "fail-then-timeout=False, ok-immediate=True, log đúng")
    return True


def main() -> int:
    cases = [t01_constants, t02_classifier, t03_probe_signature, t04_recovery_max_wait]
    failed = 0
    for i, fn in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] running {fn.__name__}...", flush=True)
        try:
            ok = fn()
        except Exception:
            import traceback
            traceback.print_exc()
            ok = False
        if not ok:
            failed += 1
    print(f"\nResult: {len(cases) - failed}/{len(cases)} passed", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
