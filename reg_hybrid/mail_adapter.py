"""Bridge MailProvider (async) → ``HttpOTPReader``-compatible (sync).

``chatgpt_camoufox.client.ChatGPTRelay`` gọi ``otp_reader.get_code(timeout, poll)``
trong thread con (toàn bộ flow là sync). MailProvider của repo này lại expose API
async (``poll_otp(*, recipient, started_at, timeout_seconds, poll_interval_seconds,
log)``). Adapter giữ reference tới event loop gốc rồi schedule coroutine vào loop
đó qua ``asyncio.run_coroutine_threadsafe``, block thread con cho tới khi coroutine
trả OTP hoặc raise.

Thiết kế:
  - Adapter được build trong main event loop, sau đó pass vào ChatGPTRelay chạy ở
    thread khác. ``get_code()`` dùng ``run_coroutine_threadsafe(coro, loop).result()``
    — đây là cách an toàn duy nhất để gọi coroutine từ thread con khi loop đang chạy
    ở thread cha.
  - Khi loop đã đóng (vd shutdown vội), ``get_code()`` raise TimeoutError có nội dung
    rõ ràng để Relay propagate ra ngoài.
  - Re-raise nguyên gốc các lỗi TimeoutError / ValueError / OutlookComboError —
    Relay không catch, caller (runner) sẽ map sang HybridSignupError.

Không phụ thuộc ``chatgpt_camoufox`` để dễ test (interface duck-typed: chỉ cần
``get_code(timeout, poll) -> str``).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class MailProviderOTPReader:
    """OTPReader (sync) → MailProvider (async).

    Attributes:
        mail_provider: instance implement ``poll_otp`` async.
        recipient: mailbox poll OTP (thường = request.source_email or request.email).
        loop: event loop đang chạy ở thread cha (chứa coroutine).
        started_at: datetime (UTC) đánh dấu bắt đầu poll — provider dùng để lọc mail cũ.
        poll_interval_seconds: poll interval per cycle (giây). Default 4.0 — đồng nhất
            với ``SignupRequest.otp_poll_interval_seconds``.
        default_timeout_seconds: override timeout do Relay truyền vào ``get_code()``.
            ChatGPTRelay default 120s — thiếu cho iCloud HME (mail trễ 1-2 phút).
            Set theo ``request.otp_timeout_seconds`` (mặc định 180s, worker spec
            bump lên 300s) để adapter dùng deadline từ SignupRequest, không phụ
            thuộc default cứng của Relay. 0 = giữ behavior cũ (theo Relay arg).
        log: callable nhận str message — được forward vào provider.
        max_retries: số lần thử poll lại khi gặp lỗi non-fatal (vd network blip).
            Tổng thời gian get_code() ≤ ``timeout`` được Relay truyền vào.
        timing_callback: optional callback(stage: str, t_monotonic: float) — runner
            inject để track t_otp_start / t_otp_end nhằm tính ``otp_seconds`` cho
            output SignupResult (giữ semantic đồng nhất với browser/pure_request).
    """

    mail_provider: Any
    recipient: str
    loop: asyncio.AbstractEventLoop
    started_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc).replace(microsecond=0)
    )
    poll_interval_seconds: float = 4.0
    default_timeout_seconds: float = 0.0
    log: Callable[[str], None] = field(default=lambda _msg: None)
    max_retries: int = 1
    timing_callback: Callable[[str, float], None] | None = None

    def get_code(self, timeout: float = 120.0, poll: float = 5.0) -> str:
        """Block thread con cho tới khi có OTP code 6 chữ số hoặc TimeoutError.

        Args:
            timeout: tổng thời gian tối đa cho phép poll (giây). ChatGPTRelay
                truyền 120s. Nếu ``self.default_timeout_seconds > 0`` thì
                override (lấy từ SignupRequest, đúng spec từng mail provider).
            poll: poll interval theo contract chatgpt_camoufox.HttpOTPReader.
                Adapter ưu tiên ``self.poll_interval_seconds`` (cấu hình từ request);
                ``poll`` chỉ dùng làm fallback khi cấu hình == 0.

        Raises:
            TimeoutError: provider không trả mã trong ``timeout`` giây.
            RuntimeError: event loop đã đóng / adapter không khả dụng.
            (Re-raises) các lỗi domain-specific từ provider (OutlookComboError, …).
        """
        if self.loop.is_closed():
            raise RuntimeError(
                "MailProviderOTPReader.get_code: event loop đã đóng "
                "(adapter build trước khi loop chạy?). Không thể poll OTP."
            )

        # Override timeout từ SignupRequest nếu có — Relay default 120s không
        # đủ cho iCloud HME (provider tự loop tới khi có mã hoặc hết deadline).
        effective_timeout = (
            self.default_timeout_seconds
            if self.default_timeout_seconds > 0
            else timeout
        )
        interval = self.poll_interval_seconds if self.poll_interval_seconds > 0 else poll

        # Bắn t_otp_start cho runner track otp_seconds.
        import time as _time
        t_start = _time.monotonic()
        if self.timing_callback is not None:
            try:
                self.timing_callback("otp_start", t_start)
            except Exception as exc:  # noqa: BLE001
                self.log(f"[otp-adapter] timing_callback raised (ignored): {exc}")

        self.log(
            f"[otp-adapter] start polling {self.recipient} via "
            f"{type(self.mail_provider).__name__} "
            f"(timeout={effective_timeout:.0f}s, poll={interval:.1f}s)"
        )

        coro = self.mail_provider.poll_otp(
            recipient=self.recipient,
            started_at=self.started_at,
            timeout_seconds=effective_timeout,
            poll_interval_seconds=interval,
            log=self.log,
        )
        # Run coroutine ở loop gốc, block thread con cho tới khi xong. Bridge này
        # giả định loop vẫn chạy — Relay.run() được gọi qua asyncio.to_thread, nên
        # loop cha đang await thread đó và sẽ tiếp tục chạy callbacks bình thường.
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            # Block hard timeout = ``effective_timeout + grace`` để adapter không bị
            # stuck vĩnh viễn khi provider quên raise (provider luôn raise TimeoutError
            # đúng deadline, grace 10s chỉ là safety net).
            code = future.result(timeout=effective_timeout + 10.0)
        except asyncio.TimeoutError as exc:
            # future.result(timeout) raise concurrent.futures.TimeoutError — không khớp
            # contract OTPReader. Convert sang TimeoutError builtin (ChatGPTRelay không
            # phân biệt nhưng caller có thể catch).
            future.cancel()
            raise TimeoutError(
                f"OTP polling vượt {effective_timeout:.0f}s cho {self.recipient}"
            ) from exc

        if not isinstance(code, str) or not code.strip():
            raise ValueError(
                f"MailProvider trả OTP code không hợp lệ: {code!r}"
            )

        # Bắn t_otp_end để runner compute otp_seconds = end - start.
        if self.timing_callback is not None:
            try:
                self.timing_callback("otp_end", _time.monotonic())
            except Exception as exc:  # noqa: BLE001
                self.log(f"[otp-adapter] timing_callback raised (ignored): {exc}")

        return code.strip()
