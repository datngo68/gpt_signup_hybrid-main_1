"""Smoke test 3 quick-win optimization cho hybrid mode.

Test cases:
    [1/5] HybridBrowserPool.warm_up + _get_or_create_runner tồn tại, idempotent.
    [2/5] warm_up gọi runner._ensure_browser_in_thread qua dedicated thread.
    [3/5] WorkerMailProvider OTP adaptive backoff: 1s/2s/3s rồi poll_interval.
    [4/5] runner._await_cleanup_bounded đồng bộ-có-timeout, chạy off main thread.
    [5/5] wait_pending_cleanups đợi xong task đã đăng ký (no-op khi rỗng).

Chạy:
    .venv/bin/python test/check_hybrid_opt_quickwins.py
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _check(name: str, fn) -> bool:
    try:
        fn()
        print(f"[PASS] {name}", flush=True)
        return True
    except AssertionError as exc:
        print(f"[FAIL] {name} — {exc}", flush=True)
        return False
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"[FAIL] {name} — {type(exc).__name__}: {exc}", flush=True)
        return False


def tc1_pool_warm_up_methods() -> bool:
    from reg_hybrid.browser_pool import HybridBrowserPool

    def _shape() -> None:
        pool = HybridBrowserPool()
        for m in ("warm_up", "_get_or_create_runner", "acquire", "shutdown_all"):
            assert hasattr(pool, m), f"HybridBrowserPool thiếu method {m}"
        sig = inspect.signature(pool.warm_up)
        params = set(sig.parameters.keys())
        assert {"proxy", "headless", "insecure", "log"}.issubset(params), (
            f"warm_up signature sai: {params}"
        )

    return _check("[1/5] HybridBrowserPool có warm_up + helpers", _shape)


def tc2_warm_up_calls_ensure_browser() -> bool:
    """warm_up phải route qua dedicated thread + gọi _ensure_browser_in_thread."""
    from reg_hybrid.browser_pool import HybridBrowserPool, _CamoufoxRunner

    captured = {"tid": None, "main_tid": None, "called": False}

    def _stub_ensure(self) -> None:
        captured["tid"] = threading.get_ident()
        captured["called"] = True

    def _run() -> None:
        captured["main_tid"] = threading.get_ident()
        pool = HybridBrowserPool()
        with patch.object(_CamoufoxRunner, "_ensure_browser_in_thread", _stub_ensure):
            pool.warm_up(
                proxy=None, headless=True, insecure=False,
                log=lambda _m: None,
            )
        # Shutdown các runner đã tạo (close executor).
        pool.shutdown_all()
        assert captured["called"], "warm_up không gọi _ensure_browser_in_thread"
        assert captured["tid"] != captured["main_tid"], (
            f"warm_up không route qua dedicated thread "
            f"(main={captured['main_tid']} ensure={captured['tid']})"
        )

    return _check("[2/5] warm_up route _ensure_browser_in_thread qua dedicated thread", _run)


def tc3_otp_adaptive_backoff() -> bool:
    """WorkerMailProvider phải sleep 1s, 2s, 3s, rồi poll_interval cố định."""
    from mail_providers import WorkerMailProvider

    sleeps: list[float] = []

    class _AbortLoop(Exception):
        pass

    async def _stub_sleep(t: float) -> None:
        sleeps.append(t)
        # Sau 5 vòng đủ data → abort để test thoát loop.
        if len(sleeps) >= 5:
            raise _AbortLoop()

    class _FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"messages": []}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def get(self, url, headers=None):
            return _FakeResponse()

    async def _run() -> None:
        provider = WorkerMailProvider(
            logs_url="http://test", api_key="k", insecure_tls=False,
        )
        with patch("mail_providers.httpx.AsyncClient", lambda **kw: _FakeClient()), \
             patch("mail_providers.asyncio.sleep", _stub_sleep):
            from datetime import datetime, timezone
            try:
                await provider.poll_otp(
                    recipient="test@example.com",
                    started_at=datetime.now(timezone.utc),
                    timeout_seconds=600.0,
                    poll_interval_seconds=4.0,
                    log=lambda _m: None,
                )
            except (TimeoutError, _AbortLoop):
                pass  # Expected

        assert len(sleeps) >= 4, f"Số sleep call quá ít: {sleeps}"
        assert abs(sleeps[0] - 1.0) < 0.1, f"Sleep #1 phải 1s, got {sleeps[0]}"
        assert abs(sleeps[1] - 2.0) < 0.1, f"Sleep #2 phải 2s, got {sleeps[1]}"
        assert abs(sleeps[2] - 3.0) < 0.1, f"Sleep #3 phải 3s, got {sleeps[2]}"
        assert abs(sleeps[3] - 4.0) < 0.1, f"Sleep #4 phải 4s (poll_interval), got {sleeps[3]}"

    def _go() -> None:
        asyncio.run(_run())

    return _check("[3/5] OTP adaptive backoff 1s→2s→3s→poll_interval", _go)


def tc4_await_cleanup_bounded() -> bool:
    """_await_cleanup_bounded ĐỒNG BỘ-có-timeout: caller chỉ tiếp tục SAU khi
    cleanup xong (fix orphan accumulation), cleanup chạy off main thread."""
    from reg_hybrid.runner import _await_cleanup_bounded

    captured = {"called_tid": None, "main_tid": None, "done": False}

    def _stub_cleanup(session, tokens, log) -> None:
        captured["called_tid"] = threading.get_ident()
        time.sleep(0.05)  # mô phỏng I/O blocking (close browser/curl)
        captured["done"] = True

    async def _run() -> None:
        captured["main_tid"] = threading.get_ident()
        with patch("reg_hybrid.runner._cleanup", _stub_cleanup):
            t0 = time.monotonic()
            await _await_cleanup_bounded(
                object(), object(), lambda _m: None, timeout=5.0,
            )
            elapsed = time.monotonic() - t0
            # ĐỒNG BỘ: caller phải đợi cleanup xong (>= ~50ms) → ngược với
            # fire-and-forget cũ. Đây là cốt lõi fix multi-signup launch-hang.
            assert elapsed >= 0.04, (
                f"_await_cleanup_bounded KHÔNG đồng bộ (return sau "
                f"{elapsed*1000:.1f}ms) — phải đợi cleanup xong"
            )
            assert captured["done"], "cleanup chưa chạy xong khi caller tiếp tục"
            assert captured["called_tid"] is not None, "cleanup chưa được gọi"
            assert captured["called_tid"] != captured["main_tid"], (
                "cleanup phải chạy off main thread (asyncio.to_thread)"
            )

    def _go() -> None:
        asyncio.run(_run())

    return _check("[4/5] _await_cleanup_bounded đồng bộ + chạy off main thread", _go)


def tc5_wait_pending_cleanups() -> bool:
    """wait_pending_cleanups: no-op khi rỗng + đợi xong task đã đăng ký."""
    from reg_hybrid.runner import (
        _BACKGROUND_CLEANUP_TASKS,
        wait_pending_cleanups,
    )

    counter = {"n": 0}

    async def _slow_task() -> None:
        await asyncio.sleep(0.05)
        counter["n"] += 1

    async def _run() -> None:
        # 1. Rỗng → return ngay (không treo).
        t0 = time.monotonic()
        await wait_pending_cleanups(timeout=1.0)
        assert time.monotonic() - t0 < 0.2, "wait_pending_cleanups treo khi rỗng"

        # 2. Có task đăng ký → đợi xong.
        for _ in range(3):
            task = asyncio.create_task(_slow_task())
            _BACKGROUND_CLEANUP_TASKS.add(task)
            task.add_done_callback(_BACKGROUND_CLEANUP_TASKS.discard)
        assert len(_BACKGROUND_CLEANUP_TASKS) == 3, (
            f"Số task không đúng: {len(_BACKGROUND_CLEANUP_TASKS)}"
        )
        t1 = time.monotonic()
        await wait_pending_cleanups(timeout=2.0)
        elapsed = time.monotonic() - t1
        assert counter["n"] == 3, f"Chỉ {counter['n']}/3 task xong sau wait"
        assert elapsed >= 0.04, (
            f"wait_pending_cleanups return quá nhanh ({elapsed:.3f}s)"
        )

    def _go() -> None:
        asyncio.run(_run())

    return _check("[5/5] wait_pending_cleanups no-op khi rỗng + đợi task đăng ký", _go)


def main() -> int:
    results = [
        tc1_pool_warm_up_methods(),
        tc2_warm_up_calls_ensure_browser(),
        tc3_otp_adaptive_backoff(),
        tc4_await_cleanup_bounded(),
        tc5_wait_pending_cleanups(),
    ]
    passed = sum(results)
    total = len(results)
    print(flush=True)
    if passed == total:
        print(f"=== HYBRID OPT QUICKWINS PASSED ({passed}/{total}) ===", flush=True)
        return 0
    print(f"=== HYBRID OPT QUICKWINS FAILED ({passed}/{total}) ===", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
