"""Smoke test: hybrid smart OTP loop khớp pure_request.

Verify:
    [1/6] acquire_fresh_otp_sync poll → trả mã đầu khi mailbox có 1 mã.
    [2/6] poll_all_codes multi-code → trả mã đầu, nạp dư vào pending.
    [3/6] prefer_second_code=True + ≥2 mã → submit mã thứ 2 trước.
    [4/6] Resend callback gọi sau ngưỡng + reset cửa sổ.
    [5/6] HybridChatGPTRelay subclass có run() override, _otp_validate_soft,
          _resend_otp helpers.
    [6/6] tried_codes tracking — pop pending skip mã đã thử.

Chạy:
    .venv/bin/python test/smoke_hybrid_otp_loop.py
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import threading
import time
from datetime import datetime, timezone
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


class _FakeProvider:
    """Provider control qua queue codes."""

    def __init__(self, *, queue_single: list[str] | None = None,
                 queue_all: list[list[str]] | None = None):
        self._queue_single = list(queue_single or [])
        self._queue_all = list(queue_all or [])
        self.poll_count = 0
        self.poll_all_count = 0

    async def poll_otp(self, **kw) -> str:
        self.poll_count += 1
        if not self._queue_single:
            await asyncio.sleep(0.001)
            raise TimeoutError("no codes")
        return self._queue_single.pop(0)

    async def poll_all_codes(self, **kw) -> list[str]:
        self.poll_all_count += 1
        if not self._queue_all:
            return []
        return self._queue_all.pop(0)


def _make_loop_in_thread() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    """Spawn asyncio loop chạy ở thread riêng (mô phỏng main async loop)."""
    loop = asyncio.new_event_loop()
    done = threading.Event()

    def _run():
        asyncio.set_event_loop(loop)
        done.set()
        loop.run_forever()

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    done.wait()
    return loop, th


def _stop_loop(loop: asyncio.AbstractEventLoop, th: threading.Thread) -> None:
    loop.call_soon_threadsafe(loop.stop)
    th.join(timeout=2.0)
    loop.close()


def tc1_acquire_single_code() -> bool:
    from reg_hybrid.otp_loop import acquire_fresh_otp_sync

    def _run() -> None:
        loop, th = _make_loop_in_thread()
        try:
            provider = _FakeProvider(queue_single=["123456"])
            tried: set[str] = set()
            pending: list[str] = []
            code, used = acquire_fresh_otp_sync(
                mail_provider=provider, mail_loop=loop,
                recipient="test@x.com",
                started_at=datetime.now(timezone.utc),
                timeout_seconds=30.0, poll_interval_seconds=5.0,
                resend_after_seconds=60.0,
                tried_codes=tried, pending=pending, max_resends=1,
                resend_callback=lambda: None, log=lambda _m: None,
            )
            assert code == "123456", f"code wrong: {code}"
            assert used == 0, f"resend không phải gọi: {used}"
            assert pending == [], f"pending phải rỗng (1 mã): {pending}"
        finally:
            _stop_loop(loop, th)

    return _check("[1/6] acquire single code", _run)


def tc2_multi_code_fetch() -> bool:
    from reg_hybrid.otp_loop import acquire_fresh_otp_sync

    def _run() -> None:
        loop, th = _make_loop_in_thread()
        try:
            # poll_otp trả 1 mã đầu tiên, poll_all_codes trả full list.
            provider = _FakeProvider(
                queue_single=["111111"],
                queue_all=[["111111", "222222", "333333"]],
            )
            tried: set[str] = set()
            pending: list[str] = []
            code, used = acquire_fresh_otp_sync(
                mail_provider=provider, mail_loop=loop,
                recipient="test@x.com",
                started_at=datetime.now(timezone.utc),
                timeout_seconds=30.0, poll_interval_seconds=5.0,
                resend_after_seconds=60.0,
                tried_codes=tried, pending=pending, max_resends=1,
                resend_callback=lambda: None, log=lambda _m: None,
                prefer_second_code=False,
            )
            assert code == "111111", f"code đầu phải 111111: {code}"
            # 222222 + 333333 phải nạp vào pending.
            assert pending == ["222222", "333333"], (
                f"pending wrong: {pending}"
            )
            assert provider.poll_all_count == 1, (
                f"poll_all_codes phải gọi 1 lần: {provider.poll_all_count}"
            )
        finally:
            _stop_loop(loop, th)

    return _check("[2/6] multi-code fetch → pending có codes dư", _run)


def tc3_prefer_second_code() -> bool:
    from reg_hybrid.otp_loop import acquire_fresh_otp_sync

    def _run() -> None:
        loop, th = _make_loop_in_thread()
        try:
            provider = _FakeProvider(
                queue_single=["111111"],
                queue_all=[["111111", "222222"]],
            )
            tried: set[str] = set()
            pending: list[str] = []
            code, _ = acquire_fresh_otp_sync(
                mail_provider=provider, mail_loop=loop,
                recipient="test@x.com",
                started_at=datetime.now(timezone.utc),
                timeout_seconds=30.0, poll_interval_seconds=5.0,
                resend_after_seconds=60.0,
                tried_codes=tried, pending=pending, max_resends=1,
                resend_callback=lambda: None, log=lambda _m: None,
                prefer_second_code=True,
            )
            # Với prefer_second_code=True + ≥2 mã, submit mã THỨ 2 (222222) trước,
            # mã đầu (111111) giữ làm fallback.
            assert code == "222222", f"prefer_second_code phải trả 222222: {code}"
            assert pending == ["111111"], (
                f"pending phải giữ 111111 fallback: {pending}"
            )
        finally:
            _stop_loop(loop, th)

    return _check("[3/6] prefer_second_code=True trả mã thứ 2 + giữ mã đầu fallback", _run)


def tc4_resend_after_threshold() -> bool:
    """Sau ngưỡng không có mã mới → gọi resend_callback."""
    from reg_hybrid.otp_loop import acquire_fresh_otp_sync

    def _run() -> None:
        loop, th = _make_loop_in_thread()
        try:
            # Mailbox rỗng vĩnh viễn → poll_otp luôn raise TimeoutError.
            # otp_loop hardcode base = max(10.0, resend_after_seconds), threshold
            # = random[base*0.5, base]. Để test deterministic, monkey-patch
            # _initial_backoff trong WorkerMailProvider không tác động. Ta dùng
            # base ngắn nhất (10s) → threshold [5s, 10s] → resend trong ~10s.
            # poll_interval=5s → sleep 5s/vòng. Cần timeout >= 30s cho 2 resend.
            provider = _FakeProvider(queue_single=[])
            resend_calls = {"n": 0}

            def _resend():
                resend_calls["n"] += 1
                # Sau resend lần 2, push mã vào để loop thoát.
                if resend_calls["n"] >= 2:
                    provider._queue_single.append("999999")

            tried: set[str] = set()
            pending: list[str] = []
            # Patch time.sleep + random.uniform để fast-forward (không sleep thực).
            # Cách dễ nhất: stub time.sleep thành no-op, threshold cố định 1s.
            real_sleep = time.sleep

            def _fast_sleep(s: float) -> None:
                # No-op sleep: tăng monotonic clock ảo qua hack — không khả thi.
                # Thay vào đó để monotonic chạy thật nhưng sleep skip.
                pass

            # Threshold cố định 1s + max_resends=3 → test trong vài giây thực.
            with patch("reg_hybrid.otp_loop.time.sleep", _fast_sleep), \
                 patch("reg_hybrid.otp_loop.random.uniform",
                       lambda a, b: 0.5):
                # poll_otp raise TimeoutError ngay → vòng poll < 0.01s. Sleep skip
                # nên loop tick rất nhanh. waited tăng theo monotonic thật.
                # Với threshold=0.5s, mỗi 0.5-1s waited vượt ngưỡng → resend trigger.
                code, used = acquire_fresh_otp_sync(
                    mail_provider=provider, mail_loop=loop,
                    recipient="test@x.com",
                    started_at=datetime.now(timezone.utc),
                    timeout_seconds=30.0, poll_interval_seconds=5.0,
                    resend_after_seconds=0.5,
                    tried_codes=tried, pending=pending, max_resends=3,
                    resend_callback=_resend, log=lambda _m: None,
                )
            assert resend_calls["n"] >= 2, (
                f"resend_callback phải gọi ≥2 lần, got {resend_calls['n']}"
            )
            assert code == "999999", f"code cuối: {code}"
            assert used >= 2, f"resends_used wrong: {used}"
        finally:
            _stop_loop(loop, th)

    return _check("[4/6] resend callback sau ngưỡng + reset cửa sổ", _run)


def tc5_relay_shape() -> bool:
    """Verify shape của HybridChatGPTRelay subclass."""
    src = (ROOT / "reg_hybrid" / "relay.py").read_text(encoding="utf-8")
    assert "class HybridChatGPTRelay(ChatGPTRelay)" in src, (
        "HybridChatGPTRelay phải subclass ChatGPTRelay"
    )
    # Phải override run() + có helpers.
    for name in ("def run(self", "_otp_validate_soft", "_resend_otp",
                 "acquire_fresh_otp_sync", "prefer_newest_untried_otp_sync"):
        assert name in src, f"relay.py thiếu identifier: {name}"

    # Runner phải import HybridChatGPTRelay
    runner_src = (ROOT / "reg_hybrid" / "runner.py").read_text(encoding="utf-8")
    assert "from .relay import HybridChatGPTRelay" in runner_src, (
        "runner.py chưa import HybridChatGPTRelay"
    )
    assert "HybridChatGPTRelay(" in runner_src, (
        "runner.py chưa instantiate HybridChatGPTRelay"
    )
    # Runner phải forward otp_resend_after_seconds
    assert "otp_resend_after_seconds=request.otp_resend_after_seconds" in runner_src, (
        "runner.py thiếu wire otp_resend_after_seconds"
    )
    return True


def tc6_tried_codes_dedup() -> bool:
    """Pop pending phải skip mã đã trong tried_codes."""
    from reg_hybrid.otp_loop import acquire_fresh_otp_sync

    def _run() -> None:
        loop, th = _make_loop_in_thread()
        try:
            provider = _FakeProvider(queue_single=["NEW01"])
            tried: set[str] = {"OLD01", "OLD02"}
            # Pending có 2 mã cũ + 0 mã mới — sẽ skip cả 2, fallback poll mạng.
            pending: list[str] = ["OLD01", "OLD02"]
            code, _ = acquire_fresh_otp_sync(
                mail_provider=provider, mail_loop=loop,
                recipient="test@x.com",
                started_at=datetime.now(timezone.utc),
                timeout_seconds=30.0, poll_interval_seconds=5.0,
                resend_after_seconds=60.0,
                tried_codes=tried, pending=pending, max_resends=1,
                resend_callback=lambda: None, log=lambda _m: None,
            )
            assert code == "NEW01", (
                f"phải skip OLD01/OLD02 (tried) và trả NEW01: {code}"
            )
            assert pending == [], (
                f"pending phải rỗng sau pop hết: {pending}"
            )
        finally:
            _stop_loop(loop, th)

    return _check("[6/6] tried_codes dedup: skip mã đã thử trong pending", _run)


def main() -> int:
    results = [
        tc1_acquire_single_code(),
        tc2_multi_code_fetch(),
        tc3_prefer_second_code(),
        tc4_resend_after_threshold(),
        _check("[5/6] HybridChatGPTRelay shape + wire trong runner", tc5_relay_shape),
        tc6_tried_codes_dedup(),
    ]
    passed = sum(results)
    total = len(results)
    print(flush=True)
    if passed == total:
        print(f"=== HYBRID OTP LOOP PASSED ({passed}/{total}) ===", flush=True)
        return 0
    print(f"=== HYBRID OTP LOOP FAILED ({passed}/{total}) ===", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
