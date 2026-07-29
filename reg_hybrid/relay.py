"""``HybridChatGPTRelay`` — subclass với smart OTP loop khớp pure_request.

Vì sao subclass:
    ``ChatGPTRelay.run()`` upstream (chatgpt_camoufox package) chỉ gọi
    ``otp_reader.get_code()`` 1 lần + ``otp_validate(code)`` 1 lần. Account
    mới có mailbox chỉ chứa 1 OTP — nếu mã đó sai/expired thì pipeline die.

    Pure_request mode đã có flow đầy đủ (xem ``request_phase._acquire_fresh_otp``
    + verify retry loop):
        - Resend OTP sau ngưỡng random ``[base*0.5, base]`` với base =
          ``otp_resend_after_seconds``.
        - Multi-code fetch (``poll_all_codes``) → bắt nhiều mã cùng inbox.
        - ``prefer_second_code=True`` lần đầu (worker đôi khi sort thiếu chính xác).
        - Verify retry: HTTP 401 / ``wrong_email_otp_code`` → pop mã khác trong
          pending hoặc resend → verify lại.
        - Human-like delay 2-4s trước mỗi verify (anti-fingerprint).
        - ``prefer_newest_untried_otp_sync`` refresh inbox lần cuối trước verify
          (bắt mã vừa về trong human-like delay).

    Subclass dùng ``otp_loop.acquire_fresh_otp_sync`` + ``prefer_newest_untried_otp_sync``
    bridge mail_provider async sang sync trong ``relay.run()``.
"""
from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime, timezone
from typing import Any, Callable

from chatgpt_camoufox.chatgpt_camoufox import headers as _ccx_headers
from chatgpt_camoufox.chatgpt_camoufox.client import ChatGPTRelay, RelayResult

from .otp_loop import acquire_fresh_otp_sync, prefer_newest_untried_otp_sync


# Markers nhận biết validate response = wrong-code (cần thử mã khác, không raise
# terminal). Lấy từ pattern OpenAI / Cloudflare WAF response.
_VERIFY_WRONG_CODE_MARKERS = (
    "wrong_email_otp_code",
    "wrong code",
    "invalid_code",
    "invalid code",
    "expired",
)


class HybridChatGPTRelay(ChatGPTRelay):
    """Override ``run()`` để khớp flow OTP của pure_request.

    Constructor args mới (so với ChatGPTRelay):
        mail_provider: async ``MailProvider`` (poll_otp + optional poll_all_codes).
        mail_loop: asyncio loop của caller (caller chạy ``relay.run`` qua
            ``asyncio.to_thread`` → loop vẫn sống ở thread cha).
        recipient: mailbox poll OTP (lower-cased email).
        otp_timeout_seconds / otp_poll_interval_seconds / otp_resend_after_seconds:
            forward từ ``SignupRequest``.
        max_resends: quota resend OTP cả phase (default 3, giống pure_request).
        timing_callback: optional ``(stage:str, t_monotonic:float) -> None`` —
            runner inject để log otp_seconds.
        on_otp_poll_start: optional ``() -> None`` — runner inject để spawn
            pre-mint sentinel #2 + SO thread ngay khi vào OTP loop.
        log: callable (kế thừa ``ChatGPTRelay.log`` không có, dùng riêng).

    Vẫn kế thừa: account, profile, session, tokens, captcha, device_id, …
    """

    def __init__(
        self,
        *args: Any,
        mail_provider: Any,
        mail_loop: asyncio.AbstractEventLoop,
        recipient: str,
        otp_timeout_seconds: float,
        otp_poll_interval_seconds: float,
        otp_resend_after_seconds: float,
        max_resends: int = 3,
        timing_callback: Callable[[str, float], None] | None = None,
        on_otp_poll_start: Callable[[], None] | None = None,
        log: Callable[[str], None] = print,
        **kwargs: Any,
    ) -> None:
        # ChatGPTRelay constructor không nhận otp_reader=None mặc định → vẫn
        # phải pass ``otp_reader``. Truyền stub vì subclass không dùng.
        kwargs.setdefault("otp_reader", _NullOTPReader())
        super().__init__(*args, **kwargs)
        self._mail_provider = mail_provider
        self._mail_loop = mail_loop
        self._recipient = recipient
        self._otp_timeout = float(otp_timeout_seconds)
        self._otp_poll_interval = float(otp_poll_interval_seconds)
        self._otp_resend_after = float(otp_resend_after_seconds)
        self._max_resends = int(max_resends)
        self._timing_callback = timing_callback
        self._on_otp_poll_start = on_otp_poll_start
        self._hybrid_log = log

    # ── Override run() ────────────────────────────────────────────────

    def run(self) -> RelayResult:
        """Skeleton golden ``ChatGPTRelay.run()`` + đúng 1 điểm khác có chủ đích.

        Mọi bước NGOÀI OTP gọi NGUYÊN method golden kế thừa (get_csrf/signin/
        authorize/register/otp_send/create_account/callback/get_session) —
        không thêm side-effect. Delta hybrid vs golden chỉ còn:
            (1) Block OTP ``_acquire_and_validate_otp()`` — smart OTP loop thay
                cho golden ``otp_reader.get_code() + otp_validate(code)``.
        """
        # ── golden: csrf → signin → authorize → register → otp_send ──
        csrf = self.get_csrf()
        authorize_url = self.signin(csrf)
        self.authorize(authorize_url)
        self.register()
        self.otp_send()

        # ── Δ: smart OTP acquisition/verify loop (thay get_code+validate) ──
        self._acquire_and_validate_otp()

        # ── golden: create_account → callback → get_session ──
        # create_account gọi method golden kế thừa ĐÚNG 1 LẦN/session. Nếu raise
        # (sentinel reject / age / rate-limit) → để propagate lên
        # run_hybrid_signup outer-loop classify như golden, KHÔNG re-POST.
        callback_url = self.create_account()
        self.callback(callback_url)
        session_json = self.get_session()
        cookies = self._dump_cookies()
        return RelayResult(
            session_json=session_json,
            device_id=self.device_id,
            cookies=cookies,
            steps=list(self.steps),
        )

    # ── OTP block (delta có chủ đích duy nhất so với golden) ──────────

    def _acquire_and_validate_otp(self) -> None:
        """Smart OTP loop khớp pure_request — thay golden 1-shot get_code+validate.

        Gồm: timing_callback otp_start/otp_end, acquire mã đầu (prefer_second_code),
        verify-retry (pop pending / resend), human-like delay 2-4s mỗi verify,
        refresh inbox lần cuối (prefer_newest_untried). Side-effect HTTP duy nhất:
        ``/email-otp/send`` (resend) + ``/email-otp/validate`` (verify) — đúng
        endpoint golden, chỉ khác ở SỐ LẦN gọi (resend/retry).

        KHÔNG pre-mint sentinel cho create_account (anti-ban): pre-mint gọi
        sentinel/req flow="oauth_create_account" TRƯỚC khi OTP validate → server
        thấy client prep create_account trước lifecycle → deferred ban signal.
        Golden mint sentinel CHỈ khi create_account được gọi (SAU OTP validate
        OK). on_otp_poll_start callback bị skip — không spawn pre-mint thread.
        """
        otp_started_at = datetime.now(timezone.utc).replace(microsecond=0)
        t_otp_start = time.monotonic()
        if self._timing_callback is not None:
            try:
                self._timing_callback("otp_start", t_otp_start)
            except Exception as exc:  # noqa: BLE001
                self._hybrid_log(
                    f"[hybrid-relay] timing_callback otp_start raised: {exc}"
                )

        # ── Smart OTP loop khớp pure_request ──
        tried_codes: set[str] = set()
        pending: list[str] = []
        resends_used = 0

        # 1. Lấy mã đầu (ưu tiên mã thứ 2 nếu mailbox có ≥2).
        otp_code, used = acquire_fresh_otp_sync(
            mail_provider=self._mail_provider,
            mail_loop=self._mail_loop,
            recipient=self._recipient,
            started_at=otp_started_at,
            timeout_seconds=self._otp_timeout,
            poll_interval_seconds=self._otp_poll_interval,
            resend_after_seconds=self._otp_resend_after,
            tried_codes=tried_codes,
            pending=pending,
            max_resends=max(0, self._max_resends - resends_used),
            resend_callback=self._resend_otp,
            log=self._hybrid_log,
            prefer_second_code=True,
        )
        resends_used += used

        # 2. Verify retry: mã sai → lấy mã khác (pop pending hoặc resend).
        max_verify_attempts = 1 + self._max_resends
        verified = False
        for v_attempt in range(1, max_verify_attempts + 1):
            # Human-like delay 2-4s random trước mỗi verify.
            delay = random.uniform(2.0, 4.0)
            self._hybrid_log(
                f"[hybrid-relay] chờ {delay:.1f}s trước verify OTP (human-like)"
            )
            time.sleep(delay)

            # Refresh mailbox lần cuối: ưu tiên mã mới nhất chưa thử.
            otp_code = prefer_newest_untried_otp_sync(
                current=otp_code, mail_provider=self._mail_provider,
                mail_loop=self._mail_loop, recipient=self._recipient,
                started_at=otp_started_at, tried_codes=tried_codes,
                pending=pending, log=self._hybrid_log,
            )
            tried_codes.add(otp_code)

            ok, status, body = self._otp_validate_soft(otp_code)
            if ok:
                verified = True
                break

            body_lower = (body or "").lower()
            is_wrong = (
                status == 401
                or any(m in body_lower for m in _VERIFY_WRONG_CODE_MARKERS)
            )
            if not is_wrong:
                raise RuntimeError(
                    f"OTP verify HTTP {status}: {body[:200]}"
                )
            if v_attempt >= max_verify_attempts:
                raise RuntimeError(
                    f"OTP verify vẫn sai sau {max_verify_attempts} lần "
                    f"(HTTP {status}) — code stale/không hợp lệ"
                )

            self._hybrid_log(
                f"[hybrid-relay] OTP sai (lần {v_attempt}/{max_verify_attempts}) "
                f"→ lấy code mới"
            )
            otp_code, used = acquire_fresh_otp_sync(
                mail_provider=self._mail_provider,
                mail_loop=self._mail_loop,
                recipient=self._recipient,
                started_at=otp_started_at,
                timeout_seconds=self._otp_timeout,
                poll_interval_seconds=self._otp_poll_interval,
                resend_after_seconds=self._otp_resend_after,
                tried_codes=tried_codes,
                pending=pending,
                max_resends=max(0, self._max_resends - resends_used),
                resend_callback=self._resend_otp,
                log=self._hybrid_log,
            )
            resends_used += used

        if not verified:
            raise RuntimeError("OTP verify thất bại")

        t_otp_end = time.monotonic()
        if self._timing_callback is not None:
            try:
                self._timing_callback("otp_end", t_otp_end)
            except Exception as exc:  # noqa: BLE001
                self._hybrid_log(
                    f"[hybrid-relay] timing_callback otp_end raised: {exc}"
                )

    # ── Helpers ───────────────────────────────────────────────────────

    def _otp_validate_soft(self, code: str) -> tuple[bool, int, str]:
        """Validate OTP — KHÔNG raise. Trả ``(ok, status, body)``.

        ChatGPTRelay.otp_validate raise khi HTTP != 200 vì assumes 1-shot.
        Subclass cần soft-check để retry với mã khác.
        """
        url = "https://auth.openai.com/api/accounts/email-otp/validate"
        try:
            r = self._post(
                url,
                json={"code": code},
                headers=_ccx_headers.otp_validate(self.profile),
            )
            body = getattr(r, "text", "") or ""
            return (r.status_code == 200, r.status_code, body)
        except Exception as exc:  # noqa: BLE001 — transport error
            self._hybrid_log(f"[hybrid-relay] otp_validate transport: {exc}")
            return (False, 0, str(exc))

    def _resend_otp(self) -> None:
        """Resend OTP: GET /email-otp/send (giống golden otp_send).

        Golden flow (HAR capture): browser navigate GET /email-otp/send với
        navigation headers. ``/resend`` endpoint không tồn tại trong capture —
        chỉ ``/send`` lặp lại. Dùng đúng method + headers golden để server
        không flag bất thường.
        """
        try:
            self.otp_send()
            self._hybrid_log("[hybrid-relay] OTP resent via /send (golden path)")
        except Exception as exc:  # noqa: BLE001
            self._hybrid_log(f"[hybrid-relay] /send resend error: {exc}")


class _NullOTPReader:
    """Stub OTPReader để pass type-check của ``ChatGPTRelay.__init__``.

    HybridChatGPTRelay OVERRIDE ``run()`` không gọi ``otp_reader.get_code()``,
    nên reader này KHÔNG được dùng. Nếu code path lạ chạy vào → raise ngay
    để fail-fast (tránh silent skip OTP).
    """

    def get_code(self, *_a: Any, **_kw: Any) -> str:
        raise RuntimeError(
            "_NullOTPReader.get_code: HybridChatGPTRelay phải override run() — "
            "không dùng otp_reader. Đây là dấu hiệu code path lạ."
        )
