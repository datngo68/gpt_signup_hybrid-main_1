"""Orchestrator: Phase 1 (browser) → poll OTP → Phase 2 (HTTP) → SignupResult.

Registration modes:
  - "browser" (default): Camoufox/Playwright browser Phase 1 + HTTP Phase 2
  - "hybrid": curl_cffi Firefox impersonate + Camoufox sentinel oracle (reg_hybrid)
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable

from browser_phase import BrowserPhaseError, run_browser_phase
from config import load_settings, runtime_session_dir
from http_phase import HttpPhaseError, run_http_phase
from mail_providers import (
    MailProvider,
    OutlookComboError,
    OutlookProviderUnavailable,
    build_provider_china_icloud,
    build_provider_dongvanfb,
    build_provider_gmail_advanced,
    build_provider_icloud_v3,
    build_provider_outlook,
    build_provider_worker,
)
from models import SignupRequest, SignupResult
from random_profile import random_profile, random_profile_for_locale
from request_phase import RequestPhaseError

if TYPE_CHECKING:
    from db.repositories import ComboRepository


# ─────────────────────────────────────────────────────────────────────
# Watchdog gia hạn theo checkpoint OTP — dùng chung cho mọi caller của
# run_signup (web manager, autoreg runner). Mục tiêu: KHÔNG bao giờ kill một
# job ngay sau khi đã lấy được OTP (lãng phí email + code).
# ─────────────────────────────────────────────────────────────────────

# Sàn tối thiểu cho base_timeout của 1 signup job: phải phủ trọn otp_timeout
# (mặc định 300s cho iCloud HME) + biên setup, nếu không job có thể bị kill ngay
# TRONG lúc chờ mail hợp lệ.
SIGNUP_BASE_TIMEOUT_FLOOR = 360.0
# Sau khi đã lấy được OTP, đảm bảo còn tối thiểu ngần này giây để hoàn tất
# about_you + phase2 + lấy session. Lớn hơn _POST_OTP_GRACE_SECONDS của
# browser_phase để deadline NỘI BỘ của flow chạm trước (báo lỗi sạch) thay vì
# bị watchdog hủy cứng.
SIGNUP_POST_OTP_GRACE = 180.0


# ─────────────────────────────────────────────────────────────────────
# Persona cookie filtering (anti-ban Phase 6 Task 6.1)
# ─────────────────────────────────────────────────────────────────────
#
# Cookies persist sau signup → re-login lần sau (`get_session`) inject lại
# để server thấy "device cũ", không treat fresh device.
#
# Whitelist cookies QUAN TRỌNG (giữ identity + minimize size):
#   - oai-did               : ext-oai-did, device identifier
#   - oaicom-stable-id      : stable per-device UUID (Datadog/sentinel cross-check)
#   - oai-asli              : auth_session_logging_id source
#   - cf_clearance          : Cloudflare bot-management clearance
#   - __cflb / __cf_bm      : Cloudflare load balancer + bot management (TTL ngắn)
#   - _cfuvid               : Cloudflare unique visitor ID
#
# KHÔNG persist:
#   - oai-sc, oai-client-auth-session, login_session, hydra_redirect,
#     auth-session-minimized*, oai-login-csrf_dev_*  (server-side state,
#     re-issued mỗi session, persist KHÔNG có ý nghĩa).
#   - __Secure-next-auth.session-token  (full session — security risk persist
#     plain text trong DB, login flow tự issue mới).

_PERSONA_COOKIE_NAMES = frozenset({
    "oai-did",
    "oaicom-stable-id",
    "oai-asli",
    "cf_clearance",
    "__cflb",
    "__cf_bm",
    "_cfuvid",
})


def _filter_persona_cookies(all_cookies: list[dict]) -> list[dict]:
    """Filter cookies → chỉ giữ persona-relevant subset cho persist DB.

    Args:
        all_cookies: list cookie dicts (Camoufox/Playwright format).

    Returns:
        Filtered list — chỉ cookies trong ``_PERSONA_COOKIE_NAMES``.
    """
    return [
        c for c in (all_cookies or [])
        if c.get("name") in _PERSONA_COOKIE_NAMES and c.get("value")
    ]


def make_otp_grace_checkpoint(
    deadline_holder: list[float], grace: float, loop
) -> Callable[[str], None]:
    """Callback(stage) bump deadline_holder[0] = max(hiện tại, now + grace).

    Thread-safe (chỉ gán 1 float + đọc monotonic clock) → an toàn khi gọi từ
    worker thread của pure_request mode.
    """
    def _cp(stage: str = "otp") -> None:
        new_dl = loop.time() + grace
        if new_dl > deadline_holder[0]:
            deadline_holder[0] = new_dl
    return _cp


async def await_with_extendable_deadline(
    coro: Awaitable, deadline_holder: list[float], *, loop=None
):
    """Await coro với deadline có thể gia hạn động (deadline_holder[0]).

    Raise asyncio.TimeoutError khi vượt deadline (cùng loại exception như
    asyncio.wait_for → tương thích handler timeout sẵn có ở caller).
    Luôn hủy task gọn khi timeout hoặc bị cancel từ ngoài (shutdown).
    """
    loop = loop or asyncio.get_event_loop()
    task = asyncio.ensure_future(coro)
    try:
        while True:
            remaining = deadline_holder[0] - loop.time()
            if remaining <= 0:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                raise asyncio.TimeoutError
            done, _ = await asyncio.wait({task}, timeout=remaining)
            if task in done:
                return task.result()
    except asyncio.CancelledError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        raise


def _build_mail_provider(
    request: SignupRequest,
    *,
    settings,
    combo_repo: "ComboRepository | None" = None,
) -> MailProvider:
    """Chọn provider theo request.mail_provider.

    NOTE: OTP polling cố tình KHÔNG đi qua ``request.proxy``. Proxy chỉ áp cho
    browser register (Phase 1) + curl_cffi /api/auth/session (Phase 2). Mail
    provider luôn poll direct để tránh proxy datacenter bị Microsoft / mail
    API chặn, và để fingerprint mail-poll không bị ràng vào IP exit register.
    """
    if request.mail_provider == "outlook":
        if not request.outlook_combo:
            raise ValueError("mail_provider='outlook' yêu cầu --outlook-combo")
        return build_provider_outlook(
            combo=request.outlook_combo,
            state_dir=settings.runtime_dir / "outlook_state",
            proxy=None,
            combo_repo=combo_repo,
        )
    if request.mail_provider == "dongvanfb":
        if not request.outlook_combo:
            raise ValueError("mail_provider='dongvanfb' yêu cầu --outlook-combo")
        return build_provider_dongvanfb(
            combo=request.outlook_combo,
            proxy=None,
        )
    if request.mail_provider == "gmail_advanced":
        if not request.gmail_api_url:
            raise ValueError("mail_provider='gmail_advanced' yêu cầu gmail_api_url")
        provider_email = request.email
        if provider_email == "pending@gmail-advanced.local":
            provider_email = ""
        return build_provider_gmail_advanced(
            email=provider_email,
            api_url=request.gmail_api_url,
        )
    if request.mail_provider == "china_icloud":
        if not request.china_icloud_url:
            raise ValueError("mail_provider='china_icloud' yêu cầu china_icloud_url")
        return build_provider_china_icloud(
            email=request.email,
            api_url=request.china_icloud_url,
            proxy=None,
        )
    if request.mail_provider == "icloud_v3":
        if not request.icloud_v3_url:
            raise ValueError("mail_provider='icloud_v3' yêu cầu icloud_v3_url")
        return build_provider_icloud_v3(
            email=request.email,
            api_url=request.icloud_v3_url,
            proxy=None,
        )
    if request.mail_provider == "worker":
        return build_provider_worker(
            logs_url=request.email_logs_url,
            api_key=request.email_api_key,
            insecure_tls=request.email_insecure_tls,
        )
    raise ValueError(f"unknown mail_provider: {request.mail_provider}")


async def run_signup(
    request: SignupRequest,
    *,
    log=print,
    combo_repo: "ComboRepository | None" = None,
    on_checkpoint=None,
) -> SignupResult:
    """Chạy signup, return SignupResult.

    on_checkpoint: callback(stage:str) — gọi khi vượt mốc quan trọng (đã lấy
        được OTP) để watchdog bên ngoài gia hạn deadline, tránh kill job ngay
        sau khi đã có OTP.

    Routing:
      - reg_mode="hybrid" → curl_cffi Firefox impersonate + Camoufox sentinel oracle
      - reg_mode="browser" (default) → Camoufox/Playwright Phase 1 + HTTP Phase 2
    """
    settings = load_settings()

    t_total_start = time.monotonic()
    result = SignupResult(success=False, email=request.email)

    try:
        # ── Random profile nếu chưa set (anti-ban: tên khớp locale) ──
        if not request.password or request.name == "ChatGPT User" or request.birthdate == "2000-01-01":
            # Locale ưu tiên: request.locale (CLI explicit) → None (Task 1.4
            # sẽ auto-detect theo proxy ở browser_phase). None ở đây → default
            # US pool (safe — tránh tên Ấn khi proxy chưa biết).
            profile = random_profile_for_locale(request.locale)
            if not request.password:
                request = request.model_copy(update={"password": profile["password"]})
            if request.name == "ChatGPT User":
                request = request.model_copy(update={"name": profile["name"]})
            if request.birthdate == "2000-01-01":
                request = request.model_copy(update={"birthdate": profile["birthdate"]})
            log(f"[signup] profile: name={request.name} age={profile['age']} locale={request.locale or 'default'}")

        # ── Build mail provider (shared for both modes) ───────────
        provider = _build_mail_provider(request, settings=settings, combo_repo=combo_repo)

        # ── Pre-check cho Gmail Advanced ──────────────────────────
        if hasattr(provider, "pre_check"):
            try:
                await provider.pre_check(log=log)
            finally:
                if provider.email and provider.email != request.email:
                    request = request.model_copy(update={"email": provider.email})
                    result.email = provider.email
                    log(f"[signup] email updated from API: {request.email}")
            if not provider.email or provider.email == "pending@gmail-advanced.local":
                raise ValueError(
                    "Gmail Advanced: API không trả email, không thể tiếp tục signup"
                )

        # ═══════════════════════════════════════════════════════════
        # Fail-fast reg_mode: pure_request đã bị gỡ khỏi đăng ký. KHÔNG để
        # reg_mode lạ rơi ngầm vào nhánh browser (AGENTS.md: không fallback che lỗi).
        # ═══════════════════════════════════════════════════════════
        if request.reg_mode not in ("browser", "hybrid"):
            raise ValueError(
                f"reg_mode không hợp lệ cho đăng ký: {request.reg_mode!r} — "
                f"chỉ 'browser' hoặc 'hybrid' (pure_request đã bị gỡ)"
            )

        # ═══════════════════════════════════════════════════════════
        # MODE: hybrid — chatgpt_camoufox pipeline (curl_cffi Firefox + Camoufox oracle)
        # ═══════════════════════════════════════════════════════════
        if request.reg_mode == "hybrid":
            from reg_hybrid import run_hybrid_signup

            log(f"[signup] mode=hybrid → curl_cffi Firefox + Camoufox sentinel oracle "
                f"(email={request.email})")
            result = await run_hybrid_signup(
                request=request,
                mail_provider=provider,
                log=log,
                on_checkpoint=on_checkpoint,
            )
            if not result.email:
                result.email = request.email

        # ═══════════════════════════════════════════════════════════
        # MODE: browser — Camoufox/Playwright Phase 1 + HTTP Phase 2
        # ═══════════════════════════════════════════════════════════
        else:
            # ── Phase 1: browser → poll OTP → submit OTP → /about-you ──
            t_p1 = time.monotonic()
            log(f"[signup] phase 1: browser → email-verification → submit OTP → /about-you (email={request.email})")
            otp_started_at = datetime.now(timezone.utc).replace(microsecond=0)

            handoff, otp_seconds = await run_browser_phase(
                request=request,
                settings=settings,
                mail_provider=provider,
                otp_started_at=otp_started_at,
                log=log,
                on_checkpoint=on_checkpoint,
            )
            result.phase1_seconds = time.monotonic() - t_p1
            result.otp_seconds = otp_seconds
            log(f"[signup] phase 1 done in {result.phase1_seconds:.2f}s (OTP {otp_seconds:.2f}s)")

            # ── Phase 2: HTTP extract session + access_token ──
            t_p2 = time.monotonic()
            log(f"[signup] phase 2: HTTP extract session + access_token")
            phase2_result = await run_http_phase(
                request=request, handoff=handoff, log=log,
            )
            result.phase2_seconds = time.monotonic() - t_p2
            log(f"[signup] phase 2 done in {result.phase2_seconds:.2f}s")

            result.success = True
            result.session_token = phase2_result["session_token"]
            result.access_token = phase2_result.get("access_token")
            result.user_id = phase2_result.get("user_id")
            result.account_id = phase2_result.get("account_id")
            result.cookies = phase2_result["cookies"]
            result.password = request.password
            result.name = request.name
            # 2FA inline (enroll trong browser page — CF-clean)
            result.two_factor = handoff.two_factor
            result.two_factor_partial = handoff.two_factor_partial
            # Compute age
            try:
                y, m, d = request.birthdate.split("-")
                from datetime import datetime as _dt
                today = _dt.utcnow()
                result.age = today.year - int(y) - ((today.month, today.day) < (int(m), int(d)))
            except Exception:
                pass

            # ── Persona cookies persist (anti-ban Phase 6 Task 6.1) ──
            # Save subset cookies persona-aware vào DB (oai-did, oaicom-stable-id,
            # oai-asli, etc.) để re-login lần sau (`get_session`) inject lại
            # → server thấy "device cũ", không treat fresh device.
            #
            # Best-effort: KHÔNG fail signup nếu save fail (account đã create).
            if combo_repo is not None and request.outlook_combo and result.cookies:
                try:
                    persona_cookies = _filter_persona_cookies(result.cookies)
                    if persona_cookies:
                        combo_repo.set_persona_cookies(request.email, persona_cookies)
                        log(
                            f"[signup] persisted {len(persona_cookies)} persona cookies "
                            f"({sorted(c['name'] for c in persona_cookies)[:6]}...)"
                        )
                except Exception as exc:
                    log(f"[signup] persona_cookies save failed (non-fatal): {exc}")

    except (BrowserPhaseError, HttpPhaseError, RequestPhaseError, TimeoutError, ValueError, OutlookComboError, OutlookProviderUnavailable) as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        log(f"[signup] FAILED: {result.error}")
    except Exception as exc:  # pragma: no cover — unexpected
        result.error = f"unexpected {type(exc).__name__}: {exc}"
        log(f"[signup] UNEXPECTED FAILURE: {result.error}")
        raise
    finally:
        log(f"[signup] total {time.monotonic() - t_total_start:.2f}s")

    return result
