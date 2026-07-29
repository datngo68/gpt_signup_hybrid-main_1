"""Smart OTP polling loop — sync port từ ``request_phase._acquire_fresh_otp``.

Mục đích: hybrid mode chạy ``relay.run()`` SYNC trong worker thread (qua
``asyncio.to_thread``) nhưng ``MailProvider`` chỉ có async API. Module này gói
logic poll/resend/multi-code/tried_codes giống hệt pure_request, bridge sync ↔
async qua ``asyncio.run_coroutine_threadsafe`` tới loop của caller.

Public API:
    - ``acquire_fresh_otp_sync(...)`` → tuple ``(code, resends_used)``.
    - ``prefer_newest_untried_otp_sync(...)`` → ``code`` mới nhất chưa thử.

Hành vi đồng nhất pure_request (verified test/run_hybrid_live_5emails.py):
    1. Pop ``pending`` (mã dư từ multi-code fetch trước) chưa thử.
    2. Poll mailbox chunk 15s (chunk ngắn để kịp check ngưỡng resend).
    3. Code mới → ``poll_all_codes`` để bắt mail delay → trả mã đầu, nạp dư vào
       ``pending``.
    4. Quá ngưỡng resend random ``[base*0.5, base]`` + còn quota → gọi
       ``resend_callback`` → reset cửa sổ chờ.
    5. ``prefer_second_code=True`` (lần đầu): nếu mailbox có ≥2 mã, submit mã
       THỨ 2 trước, mã đầu giữ làm fallback (iCloud worker đôi khi sort sai).

Raise ``TimeoutError`` khi hết ``timeout_seconds`` mà không có code mới.
"""
from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime, timezone
from typing import Any, Callable

from concurrent.futures import TimeoutError as _FutTimeout


def acquire_fresh_otp_sync(
    *,
    mail_provider: Any,
    mail_loop: asyncio.AbstractEventLoop,
    recipient: str,
    started_at: datetime,
    timeout_seconds: float,
    poll_interval_seconds: float,
    resend_after_seconds: float,
    tried_codes: set[str],
    pending: list[str],
    max_resends: int,
    resend_callback: Callable[[], None],
    log: Callable[[str], None],
    prefer_second_code: bool = False,
) -> tuple[str, int]:
    """Mirror ``request_phase._acquire_fresh_otp`` — sync version cho hybrid."""
    poll_interval = max(5.0, poll_interval_seconds)
    poll_chunk = 15.0

    def _resend_threshold() -> float:
        base = max(10.0, float(resend_after_seconds))
        return random.uniform(base * 0.5, base)

    resend_count = 0
    stale_count = 0
    cur_started = started_at
    deadline = time.monotonic() + timeout_seconds
    resend_window_start = time.monotonic()
    resend_threshold = _resend_threshold()

    while True:
        # 1. Pop pending chưa thử trước khi đụng mạng.
        while pending:
            cand = pending.pop(0)
            if cand not in tried_codes:
                return cand, resend_count

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"OTP timeout {timeout_seconds:.0f}s — không nhận được code "
                f"mới (đã resend {resend_count} lần)"
            )

        # 2. Poll 1 chunk ngắn (sync via run_coroutine_threadsafe).
        chunk = min(poll_chunk, remaining)
        coro = mail_provider.poll_otp(
            recipient=recipient, started_at=cur_started,
            timeout_seconds=chunk, poll_interval_seconds=poll_interval,
            log=log,
        )
        fut = asyncio.run_coroutine_threadsafe(coro, mail_loop)
        try:
            candidate = fut.result(timeout=chunk + 5.0)
        except (TimeoutError, _FutTimeout):
            candidate = ""
        except Exception as exc:  # noqa: BLE001 — provider error, retry
            log(f"[hybrid-otp] poll lỗi (tiếp tục): {type(exc).__name__}: {exc}")
            candidate = ""

        # 3. Code MỚI → fetch all để bắt mail delay.
        if candidate and candidate not in tried_codes:
            time.sleep(2.0)
            all_codes: list[str] = []
            if hasattr(mail_provider, "poll_all_codes"):
                coro_all = mail_provider.poll_all_codes(
                    recipient=recipient, started_at=cur_started, log=log,
                )
                fut_all = asyncio.run_coroutine_threadsafe(coro_all, mail_loop)
                try:
                    all_codes = fut_all.result(timeout=15.0)
                except Exception:  # noqa: BLE001
                    all_codes = []
            fresh = [c for c in all_codes if c not in tried_codes]
            if not fresh:
                fresh = [candidate]
            elif candidate not in fresh:
                fresh.insert(0, candidate)
            if len(fresh) > 1:
                log(f"[hybrid-otp] nhận {len(fresh)} OTP codes mới: {', '.join(fresh)}")
            # prefer_second_code: lần đầu, mã đầu có thể là code OpenAI gửi 1
            # mail "xác nhận tài khoản" cũ; mã thứ 2 mới là OTP register thực.
            if prefer_second_code and len(fresh) >= 2:
                first = fresh.pop(1)
                log(
                    f"[hybrid-otp] ưu tiên mã thứ 2 ({first}), "
                    f"giữ {fresh[0]} fallback"
                )
            else:
                first = fresh.pop(0)
            pending[:] = fresh
            return first, resend_count

        # 4. Code cũ (đã thử) lặp lại — log theo dõi.
        if candidate and candidate in tried_codes:
            stale_count += 1
            log(
                f"[hybrid-otp] poll trả code đã thử ({candidate}) → "
                f"chờ code mới (lần {stale_count})"
            )

        # 5. Chưa có code mới + đã chờ quá ngưỡng + còn quota → resend.
        waited = time.monotonic() - resend_window_start
        if resend_count < max_resends and waited >= resend_threshold:
            resend_count += 1
            log(
                f"[hybrid-otp] chờ {waited:.0f}s chưa có code mới — "
                f"resend OTP ({resend_count}/{max_resends})"
            )
            try:
                resend_callback()
            except Exception as exc:  # noqa: BLE001
                log(f"[hybrid-otp] resend lỗi (vẫn poll tiếp): {exc}")
            time.sleep(2.0)
            # Chỉ nhận mail SAU resend; reset cửa sổ + random ngưỡng mới.
            cur_started = datetime.now(timezone.utc)
            resend_window_start = time.monotonic()
            resend_threshold = _resend_threshold()
            continue

        # Chưa tới ngưỡng resend (hoặc hết quota) → chờ rồi poll lại.
        time.sleep(min(poll_interval, remaining))


def prefer_newest_untried_otp_sync(
    *,
    current: str,
    mail_provider: Any,
    mail_loop: asyncio.AbstractEventLoop,
    recipient: str,
    started_at: datetime,
    tried_codes: set[str],
    pending: list[str],
    log: Callable[[str], None],
) -> str:
    """Refresh mailbox 1 lần (non-blocking) → trả mã MỚI NHẤT chưa thử.

    Dùng ngay trước verify để bắt mã vừa về trong lúc chờ human-like delay
    (mail có thể về trong khoảng 2-4s đó). Nếu không có mã mới hơn → trả
    ``current`` nguyên.
    """
    if not hasattr(mail_provider, "poll_all_codes"):
        return current
    coro = mail_provider.poll_all_codes(
        recipient=recipient, started_at=started_at, log=log,
    )
    try:
        fut = asyncio.run_coroutine_threadsafe(coro, mail_loop)
        all_codes = fut.result(timeout=15.0)
    except Exception:  # noqa: BLE001
        all_codes = []
    fresh = [c for c in all_codes if c not in tried_codes and c != current]
    if not fresh:
        return current
    # mail_providers đã sort mới→cũ ở poll_all_codes (khi có date).
    newest = fresh[0]
    # Giữ current cũ vào pending làm fallback nếu newest fail (chưa add vào
    # tried_codes trước verify thực).
    if current and current not in tried_codes:
        pending.insert(0, current)
    # Nạp các mã dư khác vào pending.
    pending.extend(fresh[1:])
    log(f"[hybrid-otp] refresh inbox → ưu tiên mã mới {newest}")
    return newest
