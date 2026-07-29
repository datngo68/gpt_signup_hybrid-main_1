#!/usr/bin/env python3
"""Test _RotatingSession TLS rotation logic.

Mock AsyncSession để fake TLS error → verify rotate impersonate chain
chrome145 → chrome142 → chrome136 + raise đúng exception khi hết chain.

Cover:
    TC-01: Request OK lần đầu → KHÔNG rotate.
    TC-02: TLS error lần đầu → rotate sang chain[1] → retry OK.
    TC-03: TLS error 2 lần liên tiếp → rotate qua cả 3 impersonate → OK lần 3.
    TC-04: TLS error 3 lần (hết chain) → propagate exception.
    TC-05: Non-TLS error (HTTP 500, NetworkError) → KHÔNG rotate, propagate ngay.
    TC-06: _is_tls_error pattern matching cover các marker.

Chạy:
    .venv/bin/python3 test/check_upi_tls_rotation.py
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from gpt_signup_hybrid_new.web import upi_runner  # noqa: E402


class _FakeAsyncSession:
    """Mock AsyncSession ghi log impersonate đã tạo + raise error theo
    sequence configured."""

    # Class-level: ghi mọi instance đã enter (tracking impersonate sequence)
    enter_log: list[str] = []
    # Class-level queue: mỗi call .post / .get / .request consume 1 entry.
    # Entry = None → trả response giả; Entry = Exception → raise.
    response_queue: list = []

    def __init__(self, impersonate: str = "chrome145", **kwargs):
        self._impersonate = impersonate
        self._entered = False

    async def __aenter__(self):
        self._entered = True
        _FakeAsyncSession.enter_log.append(self._impersonate)
        return self

    async def __aexit__(self, *exc):
        self._entered = False
        return False

    async def post(self, *args, **kwargs):
        return await self._consume_or_raise()

    async def get(self, *args, **kwargs):
        return await self._consume_or_raise()

    async def request(self, *args, **kwargs):
        return await self._consume_or_raise()

    async def _consume_or_raise(self):
        if not _FakeAsyncSession.response_queue:
            raise RuntimeError("response_queue rỗng — test setup sai")
        item = _FakeAsyncSession.response_queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _patch_async_session(monkey: bool = True):
    """Inject _FakeAsyncSession thay AsyncSession của curl_cffi.

    upi_runner._RotatingSession import AsyncSession qua _AsyncSessionClass()
    (lazy `from curl_cffi.requests import AsyncSession`). Patch sys.modules để
    trả _FakeAsyncSession khi import.
    """
    fake_mod = types.ModuleType("curl_cffi.requests")
    fake_mod.AsyncSession = _FakeAsyncSession
    sys.modules["curl_cffi.requests"] = fake_mod

    # Reset state mỗi test
    _FakeAsyncSession.enter_log = []
    _FakeAsyncSession.response_queue = []


def _restore_async_session():
    """Re-import thật để các test file kế dùng AsyncSession real (nếu có)."""
    sys.modules.pop("curl_cffi.requests", None)


_TLS_EXC = RuntimeError(
    "Failed to perform, curl: (35) TLS connect error: "
    "error:00000000:invalid library (0):OPENSSL_internal:invalid library (0)."
)


def _logs() -> list[str]:
    """Captured logs từ _RotatingSession."""
    return _captured_logs


_captured_logs: list[str] = []


def _make_log() -> "upi_runner.LogFn":
    _captured_logs.clear()
    def _log(msg: str) -> None:
        _captured_logs.append(msg)
    return _log


def tc01_no_rotation_on_success():
    _patch_async_session()
    _FakeAsyncSession.response_queue = ["ok-response"]

    async def run():
        async with upi_runner._RotatingSession(
            ("chrome145", "chrome142", "chrome136"), log=_make_log(),
        ) as sess:
            return await sess.post("https://example.com/x")

    res = asyncio.run(run())
    assert res == "ok-response"
    assert _FakeAsyncSession.enter_log == ["chrome145"], \
        f"chỉ 1 impersonate đã dùng, got {_FakeAsyncSession.enter_log}"
    print("[PASS] TC-01 request OK lần đầu → KHÔNG rotate", flush=True)


def tc02_rotate_once_on_tls_error():
    _patch_async_session()
    _FakeAsyncSession.response_queue = [_TLS_EXC, "ok-response"]

    async def run():
        async with upi_runner._RotatingSession(
            ("chrome145", "chrome142", "chrome136"), log=_make_log(),
        ) as sess:
            return await sess.post("https://example.com/x")

    res = asyncio.run(run())
    assert res == "ok-response"
    assert _FakeAsyncSession.enter_log == ["chrome145", "chrome142"], \
        f"rotate 1 lần, got {_FakeAsyncSession.enter_log}"
    assert any("tls rotate" in line and "chrome142" in line for line in _logs()), \
        f"phải log rotation, got {_logs()}"
    print("[PASS] TC-02 TLS error lần 1 → rotate chain[1] → retry OK", flush=True)


def tc03_rotate_twice_then_ok():
    _patch_async_session()
    _FakeAsyncSession.response_queue = [_TLS_EXC, _TLS_EXC, "ok-response"]

    async def run():
        async with upi_runner._RotatingSession(
            ("chrome145", "chrome142", "chrome136"), log=_make_log(),
        ) as sess:
            return await sess.post("https://example.com/x")

    res = asyncio.run(run())
    assert res == "ok-response"
    assert _FakeAsyncSession.enter_log == ["chrome145", "chrome142", "chrome136"]
    print("[PASS] TC-03 TLS error 2 lần → rotate qua cả 3 → OK lần 3", flush=True)


def tc04_exhaust_chain_propagates():
    _patch_async_session()
    _FakeAsyncSession.response_queue = [_TLS_EXC, _TLS_EXC, _TLS_EXC]

    async def run():
        async with upi_runner._RotatingSession(
            ("chrome145", "chrome142", "chrome136"), log=_make_log(),
        ) as sess:
            return await sess.post("https://example.com/x")

    raised = None
    try:
        asyncio.run(run())
    except RuntimeError as exc:
        raised = exc
    assert raised is not None, "phải raise RuntimeError khi hết chain"
    assert "OPENSSL_internal" in str(raised) or "invalid library" in str(raised)
    assert _FakeAsyncSession.enter_log == ["chrome145", "chrome142", "chrome136"]
    print("[PASS] TC-04 TLS error 3 lần (hết chain) → propagate exception", flush=True)


def tc05_non_tls_error_no_rotation():
    _patch_async_session()
    non_tls_exc = ValueError("HTTP 500: server explosion")
    _FakeAsyncSession.response_queue = [non_tls_exc]

    async def run():
        async with upi_runner._RotatingSession(
            ("chrome145", "chrome142", "chrome136"), log=_make_log(),
        ) as sess:
            return await sess.post("https://example.com/x")

    raised = None
    try:
        asyncio.run(run())
    except ValueError as exc:
        raised = exc
    assert raised is not None
    assert "HTTP 500" in str(raised)
    assert _FakeAsyncSession.enter_log == ["chrome145"], \
        f"non-TLS error → KHÔNG rotate, got {_FakeAsyncSession.enter_log}"
    print("[PASS] TC-05 non-TLS error → KHÔNG rotate, propagate ngay", flush=True)


def tc06_is_tls_error_patterns():
    """Cover các marker quan trọng — bug fix entry-point."""
    cases = [
        # (message, expected)
        ("curl: (35) TLS connect error: error:00000000:invalid library", True),
        ("curl: (56) Recv failure", True),
        ("OPENSSL_internal:invalid library (0)", True),
        ("SSLError: handshake failure", True),
        ("HTTP 500 Internal Server Error", False),
        ("ConnectionError: 10054 reset by peer", False),
        ("invalid library (0)", True),
        ("Timeout exceeded", False),
    ]
    for msg, expect in cases:
        actual = upi_runner._is_tls_error(RuntimeError(msg))
        assert actual is expect, f"{msg!r}: expected {expect}, got {actual}"
    print(f"[PASS] TC-06 _is_tls_error pattern matching ({len(cases)} cases)", flush=True)


def main():
    tests = [
        tc01_no_rotation_on_success,
        tc02_rotate_once_on_tls_error,
        tc03_rotate_twice_then_ok,
        tc04_exhaust_chain_propagates,
        tc05_non_tls_error_no_rotation,
        tc06_is_tls_error_patterns,
    ]
    failures = 0
    for tc in tests:
        try:
            tc()
        except AssertionError as exc:
            print(f"[FAIL] {tc.__name__} :: {exc}", flush=True)
            failures += 1
        except Exception as exc:  # noqa: BLE001
            print(
                f"[FAIL] {tc.__name__} :: {type(exc).__name__}: {exc}",
                flush=True,
            )
            failures += 1
        finally:
            _restore_async_session()

    print(
        f"\n{len(tests) - failures}/{len(tests)} passed"
        + (f" — {failures} failures" if failures else ""),
        flush=True,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
