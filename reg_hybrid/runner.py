"""Orchestrator cho ``reg_mode="hybrid"``.

Pipeline (1 attempt):
    1. Build FirefoxProfile + Camoufox token generator + curl_cffi session.
    2. Build ``MailProviderOTPReader`` bridge (sync ↔ async loop hiện tại).
    3. Build ``chatgpt_camoufox.ChatGPTRelay`` với profile + session + tokens + captcha
       + Account (email/password/name/birthdate từ request).
    4. Chạy ``relay.run()`` trong ``asyncio.to_thread`` để tránh block event loop.
    5. Khi relay xong (hoặc lỗi), map ``RelayResult`` → ``SignupResult`` đồng nhất
       schema với browser/pure_request mode:
         - session_token = cookies["__Secure-next-auth.session-token"] (chunks)
         - access_token + user_id = session_json
         - account_id = cookies["_account"]
         - cookies = list dict ([{name,value,domain,path,secure}])
         - age compute từ birthdate
         - phase1_seconds = start → OTP retrieved
         - otp_seconds = OTP poll time
         - phase2_seconds = OTP retrieved → end
    6. Gọi ``on_checkpoint("otp")`` ngay sau khi OTP được fetch — caller (autoreg
       runner / web manager) dùng để gia hạn watchdog deadline.

Outer-loop retry (defense-in-depth):
    - HTTP 409 invalid_state → re-init full pipeline 1 lần (fresh session +
      device_id + sentinel state).
    - Cloudflare 403 ở csrf/signin → rotate curl_cffi impersonate
      (firefox135 → firefox133 → firefox120) trong attempt kế tiếp.
    - Lỗi terminal (combo dead, OTP timeout, validation error) → KHÔNG retry.

Lifecycle:
    - CamoufoxTokenGenerator persistent xuyên relay (1 browser/page reuse).
    - Đóng generator + session trong ``finally`` để không leak process Firefox.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from models import SignupRequest, SignupResult

from .camoufox_factory import (
    build_captcha,
    build_curl_session,
    build_firefox_profile,
    build_token_generator,
)
from .mail_adapter import MailProviderOTPReader

if TYPE_CHECKING:
    from mail_providers import MailProvider

logger = logging.getLogger(__name__)


class HybridSignupError(Exception):
    """Hybrid pipeline fail (relay raise hoặc adapter raise)."""


# Cookie name của NextAuth session — Phase 2 (browser mode) đã có logic
# tương tự ở ``http_phase._extract_session_from_handoff``. Giữ cùng nguồn tham chiếu.
_SESSION_COOKIE_BASE = "__Secure-next-auth.session-token"
_ACCOUNT_COOKIE_NAME = "_account"

# TLS impersonate rotation chain — chỉ kích hoạt khi CF 403 ở csrf/signin
# (fingerprint flag). Đổi major Firefox = UA HTTP đổi → có thể lệch Camoufox UA
# (Camoufox always Firefox 135). Chấp nhận trade-off vì rotation là last-resort.
_IMPERSONATE_FALLBACK_MAJORS: tuple[int, ...] = (135, 133, 120)

# Markers nhận biết lỗi retry-được ở outer loop (full re-init).
_INVALID_STATE_MARKERS: tuple[str, ...] = (
    "invalid_state",
    "session is no longer valid",
    "409",  # HTTP 409 generic
)
_CF_BLOCK_MARKERS: tuple[str, ...] = (
    "403 forbidden",
    "cloudflare",
    "cf-mitigated",
    "just a moment",
    "attention required",
)

# Proxy dead: connection refused / timeout / TLS handshake fail QUA proxy.
# Không retry trong hybrid (rotate firefox_major vô nghĩa khi proxy chết) —
# fail-fast để autoreg outer-loop acquire proxy mới. Re-export từ
# ``_browser_retry.NETWORK_ERROR_MARKERS`` (single source of truth) để chiến
# lược classify nhất quán với mark_dead helper ở autoreg.
from _browser_retry import NETWORK_ERROR_MARKERS as _PROXY_DEAD_MARKERS  # noqa: E402

# Sentinel SO empty / Observer chưa fire đủ events / driver-pipe chết →
# context page có thể bị throttle hoặc browser process die giữa flow. Retry
# với fresh BrowserContext (acquire mới = page mới + feeder script fresh)
# thường thoát.
_SENTINEL_OBSERVER_MARKERS: tuple[str, ...] = (
    "sessionobservertoken",  # case-insensitive match qua msg.lower()
    "observer cache empty",
    "page.evaluate failed",
    # Playwright driver pipe chết (browser process bị OS kill / anti-bot
    # detection / resource pressure giữa lúc page.evaluate). Stack trace
    # điển hình: "Page.evaluate: Connection closed while reading from the
    # driver". Retry với fresh BrowserContext (Camoufox launch mới) phục hồi
    # được vì driver pipe chỉ chết ở instance hiện tại.
    "connection closed while reading from the driver",
    "transport closed",
    "target page, context or browser has been closed",
)

# Sentinel cache TTL: token mint trong page Camoufox có timestamp ở dx-VM,
# server-side reject khi token quá tuổi. Capture HAR golden record cho thấy
# sentinel mint → submit gap ~10-30s; cho TTL rộng rãi để cover anti-ban
# variance nhưng không quá dài làm pre-mint vô nghĩa với mail HME delay 5-15
# phút. 90s đủ buffer cho human-like delay (2-4s) + create_account roundtrip
# (~1s), vẫn ép remint khi OTP poll kéo dài.
_SENTINEL_CACHE_TTL_SECONDS = 90.0


# Module-level set giữ reference task background cleanup (giữ cho public API
# ``wait_pending_cleanups`` — caller live test/autoreg có thể dùng làm safety
# net). Success path hiện cleanup ĐỒNG BỘ-có-timeout (xem ``_await_cleanup_bounded``)
# nên set này thường rỗng; vẫn giữ API để không phá caller hiện có.
_BACKGROUND_CLEANUP_TASKS: set[asyncio.Task] = set()

# Cap thời gian chờ cleanup ĐỒNG BỘ ở success path. ``tokens.close()`` đã bound
# nội bộ (teardown timeout + reaper force-kill) nên giá trị này chỉ là safety
# net ngoài cùng: đủ rộng để close + SIGTERM→SIGKILL hoàn tất, không treo caller.
_CLEANUP_WAIT_TIMEOUT_SECONDS = 45.0


async def _await_cleanup_bounded(
    session: Any, tokens: Any, log: Callable[[str], None], *, timeout: float,
) -> None:
    """Cleanup ĐỒNG BỘ-có-timeout: đợi browser/curl đóng (hoặc bị reaper kill)
    XONG trước khi caller tiếp tục.

    Fix bug "multi-signup launch-hang": trước đây success path fire-and-forget
    cleanup → signup N+1 launch Camoufox khi browser signup N CHƯA đóng →
    orphan Firefox tích lũy → cạn tài nguyên → launch sau treo. Đợi đồng bộ
    đảm bảo lifecycle browser tuần tự (đóng N → mới launch N+1). ``tokens.close()``
    đã bound + force-kill nội bộ nên chờ này luôn kết thúc.
    """
    if session is None and tokens is None:
        return
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_cleanup, session, tokens, log), timeout,
        )
    except asyncio.TimeoutError:
        # _cleanup tự bound (reaper đã force-kill browser) — không treo vô hạn.
        log(
            f"[hybrid] cleanup vượt {timeout:.0f}s — reaper đã xử lý kill browser, "
            f"tiếp tục (không block signup kế tiếp)"
        )


async def wait_pending_cleanups(timeout: float = 5.0) -> None:
    """Đợi mọi background cleanup task hoàn thành (best-effort, public API).

    Use case: live test script / autoreg muốn đảm bảo browser/context đã đóng
    sạch trước khi process exit. Success path nay cleanup đồng bộ nên set
    thường rỗng → hàm này trả ngay; giữ để caller cũ không vỡ.
    """
    if not _BACKGROUND_CLEANUP_TASKS:
        return
    pending = list(_BACKGROUND_CLEANUP_TASKS)
    try:
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout)
    except asyncio.TimeoutError:
        pass


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _compute_age(birthdate: str | None) -> int | None:
    """Compute tuổi tròn từ birthdate ISO ``YYYY-MM-DD``. None khi parse fail."""
    if not birthdate:
        return None
    try:
        y, m, d = birthdate.split("-")
        today = datetime.now(timezone.utc)
        return today.year - int(y) - ((today.month, today.day) < (int(m), int(d)))
    except (ValueError, AttributeError):
        return None


def _extract_session_token(cookies_dict: dict[str, str]) -> str | None:
    """Ghép session-token từ ``cookies_dict`` (flat name→value).

    NextAuth có thể split token thành ``...session-token``, ``...session-token.0``,
    ``...session-token.1``. Return None khi không tìm thấy mảnh nào.
    """
    if not cookies_dict:
        return None
    base = cookies_dict.get(_SESSION_COOKIE_BASE)
    if base:
        return base
    chunks: dict[str, str] = {}
    prefix = _SESSION_COOKIE_BASE + "."
    for name, value in cookies_dict.items():
        if not name.startswith(prefix):
            continue
        idx = name[len(prefix):]
        if idx:
            chunks[idx] = value
    if not chunks:
        return None
    return "".join(chunks[k] for k in sorted(chunks))


def _normalize_relay_cookies(
    relay: Any, cookies_flat: dict[str, str],
) -> list[dict[str, Any]]:
    """Map cookies từ relay (flat dict) sang list dict format chuẩn repo.

    Relay (chatgpt_camoufox) flatten cookies thành ``{name: value}`` để tiện debug;
    repo cần list ``[{name, value, domain, path, secure}]`` để inject lại curl_cffi
    ở Phase 2 hoặc persist DB. Lấy domain/path/secure từ chính ``relay._jar`` (CookieJar
    stdlib) — tránh đoán mò.
    """
    if not cookies_flat:
        return []
    jar = getattr(relay, "_jar", None)
    if jar is None:
        # Fallback: minimal record, mặc định domain chatgpt.com
        return [
            {"name": k, "value": v, "domain": ".chatgpt.com", "path": "/", "secure": True}
            for k, v in cookies_flat.items()
        ]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cookie in jar:
        if cookie.name in seen:
            continue
        seen.add(cookie.name)
        out.append({
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain or ".chatgpt.com",
            "path": cookie.path or "/",
            "secure": bool(cookie.secure),
        })
    return out


def _build_account(request: SignupRequest):
    """Build ``chatgpt_camoufox.Account`` từ request.

    ``api="manual"`` vì OTPReader đã được runtime inject (qua MailProviderOTPReader)
    — tránh ChatGPTRelay tự build HttpOTPReader/ManualOTPReader nội bộ.
    """
    from chatgpt_camoufox.chatgpt_camoufox.client import Account

    return Account(
        email=request.email,
        password=request.password or "",
        api="manual",
        name=request.name if request.name and request.name != "ChatGPT User" else None,
        birthdate=request.birthdate if request.birthdate and request.birthdate != "2000-01-01" else None,
        age=None,  # Birthdate ưu tiên hơn age — set None để Relay dùng birthdate trực tiếp.
    )


def _classify_error(exc: BaseException) -> str:
    """Classify exception → category để outer loop quyết retry:

    - ``"invalid_state"``: HTTP 409 invalid_state, OAuth session desync. Retry
      được với re-init full pipeline.
    - ``"cf_block"``: Cloudflare 403 ở csrf/signin (fingerprint flag). Retry
      với impersonate khác.
    - ``"sentinel_observer"``: sentinel SO empty / Observer chưa fire đủ DOM
      events. Retry với fresh BrowserContext (feeder script fresh từ đầu).
    - ``"proxy_dead"``: proxy connection refused / timeout / TLS fail qua
      proxy (NS_ERROR_PROXY_*, ERR_PROXY_*, curl 7/28/35/56/97). Fail-fast —
      rotate firefox_major không giúp khi proxy chết; autoreg outer-loop sẽ
      mark_dead + acquire proxy mới cho attempt kế tiếp.
    - ``"terminal"``: combo dead / OTP timeout / pydantic validation. KHÔNG retry.
    """
    msg = str(exc).lower()
    # Proxy check trước CF — `NS_ERROR_PROXY_CONNECTION_REFUSED` có khi server
    # phía sau là Cloudflare; nếu hit CF_BLOCK_MARKERS trước sẽ rotate UA vô ích.
    if any(m.lower() in msg for m in _PROXY_DEAD_MARKERS):
        return "proxy_dead"
    if any(m in msg for m in _INVALID_STATE_MARKERS):
        return "invalid_state"
    if any(m in msg for m in _CF_BLOCK_MARKERS):
        return "cf_block"
    if any(m in msg for m in _SENTINEL_OBSERVER_MARKERS):
        return "sentinel_observer"
    return "terminal"


def _build_pipeline(
    request: SignupRequest,
    *,
    firefox_major: int,
    log: Callable[[str], None],
) -> tuple[Any, Any, Any, Any, Any]:
    """Build full stack 1 attempt (profile, session, tokens, captcha, account).

    Tách thành helper để outer-loop retry build lại artefact sạch khi cần.
    """
    from chatgpt_camoufox.chatgpt_camoufox.fingerprint import profile_for_locale
    import random as _random

    # build_firefox_profile dùng defaults factory (Windows + en-US). Khi rotate
    # firefox_major, gọi profile_for_locale trực tiếp với major mong muốn.
    locale = (request.locale or "en-US").strip() or "en-US"
    profile = profile_for_locale(
        locale=locale,
        firefox_major=firefox_major,
        platform="Windows",
        rng=_random.Random(),
    )
    session = build_curl_session(request, profile=profile)
    tokens = build_token_generator(request, profile=profile)
    captcha = build_captcha(log)
    account = _build_account(request)
    return profile, session, tokens, captcha, account


def _cleanup(session: Any, tokens: Any, log: Callable[[str], None]) -> None:
    """Best-effort close curl session + Camoufox browser. KHÔNG raise."""
    if session is not None:
        try:
            session.close()
        except Exception as exc:  # noqa: BLE001
            log(f"[hybrid] curl session close failed (ignored): {exc}")
    if tokens is not None:
        try:
            tokens.close()
        except Exception as exc:  # noqa: BLE001
            log(f"[hybrid] camoufox tokens close failed (ignored): {exc}")


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


def _patch_tokens_cache(
    tokens: Any, log: Callable[[str], None],
) -> dict[str, Any]:
    """Patch ``tokens.mint_token/mint_so`` để return cached khi flow=oauth_create_account.

    Cache được fill bởi ``_spawn_premint_thread`` (chạy nền song song với OTP
    poll). Hai hàm này chia ra rõ ràng:
      - ``_patch_tokens_cache`` setup hook đọc cache (idempotent, gọi 1 lần).
      - ``_spawn_premint_thread`` spawn worker fill cache (gọi từ on_otp_poll_start
        của ``HybridChatGPTRelay`` — đúng thời điểm bắt đầu chờ OTP).

    Cache TTL: sentinel token có giới hạn thời gian (server-side timestamp ở
    dx-VM). Nếu OTP poll kéo dài quá ``_SENTINEL_CACHE_TTL_SECONDS`` (mail HME
    có thể delay 5-15 phút) → cache stale → server reject ``create_account``
    với response không có callback url. Cách fix: TTL check trong patched mint
    function — stale → mint lại fresh.

    Trả về dict ``cache`` (mutable) — caller dùng để spawn thread với cùng cache.
    """
    cache: dict[str, Any] = {
        "token": None, "so": None, "error": None,
        "minted_at": 0.0,
        "original_mint_token": tokens.mint_token,
        "original_mint_so": tokens.mint_so,
    }

    def _is_fresh() -> bool:
        return (
            cache["token"] is not None
            and (time.monotonic() - cache["minted_at"]) <= _SENTINEL_CACHE_TTL_SECONDS
        )

    def _invalidate(reason: str) -> None:
        cache["token"] = None
        cache["so"] = None
        cache["minted_at"] = 0.0
        log(f"[hybrid] sentinel cache INVALIDATED ({reason})")

    def _patched_mint_token(flow: str):
        """Return cached token nếu flow=oauth_create_account, cache hit + fresh."""
        if flow == "oauth_create_account":
            if cache["token"] is not None:
                age = time.monotonic() - cache["minted_at"]
                if age <= _SENTINEL_CACHE_TTL_SECONDS:
                    log(f"[hybrid] mint_token cache HIT (age={age:.1f}s)")
                    return cache["token"]
                _invalidate(f"token stale age={age:.1f}s > {_SENTINEL_CACHE_TTL_SECONDS:.0f}s")
        return cache["original_mint_token"](flow)

    def _patched_mint_so(flow: str):
        """Return cached so nếu flow=oauth_create_account, cache fresh."""
        if flow == "oauth_create_account":
            if cache["so"] is not None:
                age = time.monotonic() - cache["minted_at"]
                if age <= _SENTINEL_CACHE_TTL_SECONDS:
                    log(f"[hybrid] mint_so cache HIT (age={age:.1f}s)")
                    return cache["so"]
                # Token đã invalidated cùng so ở _patched_mint_token call trước.
        return cache["original_mint_so"](flow)

    tokens.mint_token = _patched_mint_token  # type: ignore[method-assign]
    tokens.mint_so = _patched_mint_so  # type: ignore[method-assign]
    return cache


def _spawn_premint_thread(
    cache: dict[str, Any], log: Callable[[str], None],
) -> None:
    """Spawn daemon thread mint sentinel #2 + SO cho flow=oauth_create_account.

    Idempotent qua flag ``cache["spawned"]`` — caller (on_otp_poll_start) chỉ
    gọi 1 lần per signup (HybridChatGPTRelay invariant).
    """
    import threading as _threading

    if cache.get("spawned"):
        return
    cache["spawned"] = True

    original_mint_token = cache["original_mint_token"]
    original_mint_so = cache["original_mint_so"]

    def _premint() -> None:
        try:
            log("[hybrid] pre-mint sentinel oauth_create_account start (background)")
            t_start = time.monotonic()
            token = original_mint_token("oauth_create_account")
            cache["token"] = token
            cache["minted_at"] = time.monotonic()
            try:
                so = original_mint_so("oauth_create_account")
                cache["so"] = so
            except Exception as exc:  # noqa: BLE001 — SO observer non-fatal
                log(f"[hybrid] pre-mint so failed (non-fatal): {exc}")
            log(
                f"[hybrid] pre-mint sentinel oauth_create_account OK "
                f"({time.monotonic() - t_start:.2f}s, TTL={_SENTINEL_CACHE_TTL_SECONDS:.0f}s)"
            )
        except Exception as exc:  # noqa: BLE001 — main fallback nếu cache miss
            cache["error"] = exc
            log(f"[hybrid] pre-mint sentinel failed (will fallback): {exc}")

    pre_thread = _threading.Thread(
        target=_premint, name="hybrid-premint", daemon=True,
    )
    pre_thread.start()


def _setup_premint_cache(
    tokens: Any, otp_reader: Any, log: Callable[[str], None],
) -> dict[str, Any]:
    """LEGACY entry point — giữ để smoke test cũ không vỡ.

    Wire pattern cũ: patch ``otp_reader.get_code`` để spawn premint thread.
    HybridChatGPTRelay đã chuyển sang ``on_otp_poll_start`` callback → KHÔNG
    còn dùng entry point này trong runtime, chỉ tồn tại cho test legacy.
    """
    import threading as _threading

    cache = _patch_tokens_cache(tokens, log)
    premint_started = _threading.Event()
    original_get_code = otp_reader.get_code

    def _patched_get_code(timeout: float = 120.0, poll: float = 5.0) -> str:
        if not premint_started.is_set():
            premint_started.set()
            _spawn_premint_thread(cache, log)
        return original_get_code(timeout=timeout, poll=poll)

    otp_reader.get_code = _patched_get_code  # type: ignore[method-assign]
    return cache


async def run_hybrid_signup(
    request: SignupRequest,
    *,
    mail_provider: "MailProvider",
    log: Callable[[str], None] = print,
    on_checkpoint: Callable[[str], None] | None = None,
) -> SignupResult:
    """Chạy 1 signup theo pipeline chatgpt_camoufox + repo's MailProvider.

    Args:
        request: SignupRequest. Field bắt buộc: email, password (hoặc Relay sẽ
            raise vì password rỗng), name, birthdate. ``proxy`` áp cho cả Camoufox
            (mint token) và curl_cffi (gửi request) — phải dùng CÙNG proxy.
        mail_provider: instance từ ``signup._build_mail_provider`` (worker /
            outlook / gmail_advanced / dongvanfb / china_icloud).
        log: callable forward log message.
        on_checkpoint: callback nhận stage name khi vượt mốc quan trọng — hiện
            chỉ gọi ``("otp")`` ngay sau Relay đọc xong OTP code (deadline grace
            của watchdog cha).

    Returns:
        SignupResult với ``success=True`` khi callback + /api/auth/session 200.
        ``success=False`` (kèm ``error``) khi pipeline fail. Output schema đồng
        nhất với browser/pure_request mode (đầy đủ phase1/phase2/otp seconds +
        account_id từ cookie ``_account``).

    Raises: KHÔNG — mọi lỗi đóng gói vào SignupResult.error để caller pipeline
        gốc (signup.run_signup) handle thống nhất.
    """
    result = SignupResult(success=False, email=request.email)
    t_total_start = time.monotonic()

    # Validate input sớm — Relay sẽ raise mơ hồ nếu password rỗng.
    if not request.password:
        result.error = (
            "hybrid: request.password is required (signup orchestrator phải "
            "gen random profile trước khi gọi)"
        )
        log(f"[hybrid] FAIL: {result.error}")
        return result

    # Recipient OTP — ưu tiên source_email (mailbox khác email form, vd HME relay
    # khi register bằng alias).
    recipient = (request.source_email or request.email).strip().lower()

    if request.har_capture:
        log("[hybrid] note: har_capture=True không có hiệu lực (hybrid không dùng Camoufox UI)")
    if request.keep_browser_open:
        log("[hybrid] note: keep_browser_open=True không có hiệu lực (hybrid Camoufox chỉ chạy oracle)")
    if request.persona and request.persona != "firefox_mac":
        log(
            f"[hybrid] note: persona={request.persona!r} bị ignore — "
            f"hybrid bám sát chatgpt_camoufox golden = Firefox 135 Windows"
        )

    log(
        f"[hybrid] init: email={request.email} recipient={recipient} "
        f"locale={request.locale or 'en-US (default)'} platform=Windows firefox=135 "
        f"proxy={'***' if request.proxy else 'direct'} headless={request.headless} "
        f"otp_timeout={request.otp_timeout_seconds:.0f}s "
        f"mfa_inline={request.mfa_inline}"
    )

    # ── Outer-loop retry (max 1 lần re-init khi gặp 409 / CF block) ───
    max_attempts = 2
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        # Chọn impersonate cho attempt này. attempt 1 = firefox135 (golden);
        # attempt 2 = firefox133 (fallback nếu CF flag JA3 firefox135 cụ thể).
        major = _IMPERSONATE_FALLBACK_MAJORS[
            min(attempt - 1, len(_IMPERSONATE_FALLBACK_MAJORS) - 1)
        ]
        if attempt > 1:
            log(
                f"[hybrid] attempt {attempt}/{max_attempts}: "
                f"rotate firefox_major={major} (last error: "
                f"{type(last_error).__name__ if last_error else '?'})"
            )

        session = None
        tokens = None
        # Timing markers — dùng list để mutate từ closure (timing_callback).
        timing: dict[str, float] = {"otp_start": 0.0, "otp_end": 0.0}

        try:
            # `_build_pipeline` launch Camoufox (Playwright sync API). PHẢI
            # chạy ngoài asyncio thread cha — Playwright sync_api raise
            # NotImplementedError nếu detect event loop ở cùng thread. Pool
            # runner cũng có dedicated thread riêng, nên thread của
            # ``asyncio.to_thread`` ở đây chỉ là transient — launch xong nó
            # release, op sau (mint_token/mint_so) route qua runner thread.
            profile, session, tokens, captcha, account = await asyncio.to_thread(
                _build_pipeline, request, firefox_major=major, log=log,
            )

            def _timing_cb(stage: str, t: float) -> None:
                timing[stage] = t
                if stage == "otp_end" and on_checkpoint is not None:
                    try:
                        on_checkpoint("otp")
                    except Exception as exc:  # noqa: BLE001
                        log(f"[hybrid] on_checkpoint raised (ignored): {exc}")

            # NOTE: pre-mint sentinel cache ĐÃ BỎ (anti-ban deferred ban fix).
            # Lý do: pre-mint gọi sentinel/req flow="oauth_create_account"
            # TRƯỚC OTP validate → server detect automation pattern → ban 1-24h.
            # Golden flow: mint sentinel ngay tại create_account() call (SAU
            # OTP validate OK). Không cần cache — mint ~2s là chấp nhận được.

            # ── HybridChatGPTRelay: subclass với smart OTP loop khớp pure_request ──
            # Phải import ở đây (không top-level) vì chatgpt_camoufox là optional
            # dependency — smoke test có thể chạy mà không cài curl_cffi/camoufox.
            from .relay import HybridChatGPTRelay
            relay = HybridChatGPTRelay(
                account=account,
                session=session,
                profile=profile,
                tokens=tokens,
                captcha=captcha,
                # Hybrid-specific OTP injection
                mail_provider=mail_provider,
                mail_loop=asyncio.get_running_loop(),
                recipient=recipient,
                otp_timeout_seconds=request.otp_timeout_seconds,
                otp_poll_interval_seconds=request.otp_poll_interval_seconds,
                otp_resend_after_seconds=request.otp_resend_after_seconds,
                # max_resends scale theo otp_timeout: cứ ~2 phút/resend, +2
                # safety margin. Mailbox HME có thể delay 5-15 phút → cần đủ
                # resend budget để cover toàn bộ deadline (tránh hết quota
                # trước deadline).
                max_resends=max(
                    3,
                    int(request.otp_timeout_seconds // 120) + 2,
                ),
                timing_callback=_timing_cb,
                on_otp_poll_start=None,  # BỎ pre-mint — anti-ban
                log=log,
            )

            # Sync flow chạy trong thread → không block event loop.
            relay_result = await asyncio.to_thread(relay.run)

            # ── Map RelayResult → SignupResult (output đồng nhất) ────
            session_json = relay_result.session_json or {}
            user_info = session_json.get("user") or {}
            cookies_flat = dict(relay_result.cookies or {})

            result.success = True
            result.access_token = session_json.get("accessToken")
            result.user_id = user_info.get("id")
            # account_id từ cookie `_account` (chatgpt.com — set khi callback OAuth).
            # Browser mode lấy field này từ Phase 2 http_phase. Hybrid extract trực
            # tiếp từ cookies flat dict để đồng nhất schema.
            result.account_id = cookies_flat.get(_ACCOUNT_COOKIE_NAME)
            result.password = request.password
            result.name = request.name
            result.age = _compute_age(request.birthdate)
            result.cookies = _normalize_relay_cookies(relay, cookies_flat)
            result.session_token = _extract_session_token(cookies_flat)

            # ── 2FA inline (CF-clean): tái dùng session curl_cffi còn sống ──
            # Browser/pure_request mode đều enroll 2FA NGAY trong context vừa pass
            # CF (browser page hoặc curl session). Hybrid trước đây silent ignore
            # → phải gọi external `enable-2fa` → mất ưu thế CF cookies sống. Fix:
            # gọi `enable_2fa_in_session` qua `to_thread` (sync curl) ngay khi
            # còn `relay.session` + `cf_clearance` fresh.
            if (
                request.mfa_inline
                and result.access_token
                and getattr(relay, "session", None) is not None
            ):
                t_mfa_start = time.monotonic()
                try:
                    from mfa_phase import MfaError, enable_2fa_in_session
                    user_agent = getattr(profile, "user_agent", None) or ""
                    two_factor = await asyncio.to_thread(
                        enable_2fa_in_session,
                        relay.session,
                        access_token=result.access_token,
                        user_agent=user_agent,
                        activate=True,
                        log=log,
                    )
                    result.two_factor = two_factor
                    log(
                        f"[hybrid] 2FA activated secret_len="
                        f"{len(two_factor.get('secret') or '')} "
                        f"factor_id={(two_factor.get('factor_id') or '')[:20]} "
                        f"mfa_elapsed={time.monotonic() - t_mfa_start:.2f}s"
                    )
                except MfaError as exc:
                    # Enroll OK + activate fail → partial_state mang secret để
                    # caller persist + retry activate-only sau (không enroll lại).
                    result.two_factor_partial = exc.partial_state
                    log(
                        f"[hybrid] 2FA failed (partial={bool(exc.partial_state)}): "
                        f"{exc}"
                    )
                except Exception as exc:  # noqa: BLE001 — best-effort, không fail reg
                    log(
                        f"[hybrid] 2FA unexpected {type(exc).__name__}: {exc} — "
                        f"reg đã success, caller phải gọi enable-2fa external"
                    )

            # Timing đồng nhất với browser/pure_request:
            #   - phase1 = bootstrap → register → OTP retrieved
            #   - otp = thời gian poll mail thực tế
            #   - phase2 = OTP validate → create_account → callback → session
            t_end = time.monotonic()
            t_otp_start = timing["otp_start"] or t_total_start
            t_otp_end = timing["otp_end"] or t_otp_start
            result.otp_seconds = max(0.0, t_otp_end - t_otp_start)
            result.phase1_seconds = max(0.0, t_otp_end - t_total_start)
            result.phase2_seconds = max(0.0, t_end - t_otp_end)

            log(
                f"[hybrid] OK email={result.email} user_id={result.user_id} "
                f"account_id={result.account_id or '<missing>'} "
                f"cookies={len(result.cookies)} "
                f"session_token={'<set>' if result.session_token else '<missing>'} "
                f"two_factor={'<active>' if result.two_factor and result.two_factor.get('activated') else ('<partial>' if result.two_factor_partial else '<none>')} "
                f"phase1={result.phase1_seconds:.2f}s otp={result.otp_seconds:.2f}s "
                f"phase2={result.phase2_seconds:.2f}s "
                f"steps={len(relay_result.steps)} attempt={attempt}"
            )
            return result

        except Exception as exc:  # noqa: BLE001 — pipeline error → classify
            last_error = exc
            category = _classify_error(exc)
            log(
                f"[hybrid] attempt {attempt}/{max_attempts} failed: "
                f"{type(exc).__name__}: {exc} (category={category})"
            )
            logger.exception(
                "hybrid pipeline attempt %d failed for %s", attempt, request.email,
            )

            # Cleanup attempt artefacts TRƯỚC khi retry — tránh process Firefox
            # treo song song. Chạy qua ``to_thread`` để cleanup blocking ops
            # (context.close, session.close) không stall event loop.
            await asyncio.to_thread(_cleanup, session, tokens, log)
            session = None
            tokens = None

            # Quyết định retry: chỉ retry invalid_state / cf_block, và chưa hết
            # attempt budget. ``proxy_dead`` fail-fast (rotate firefox_major
            # không giúp khi proxy chết) — autoreg outer-loop sẽ mark_dead +
            # acquire proxy mới cho attempt kế tiếp.
            if attempt >= max_attempts or category in ("terminal", "proxy_dead"):
                result.success = False
                result.error = f"{type(exc).__name__}: {exc}"
                # Nếu đã hết OTP timing, vẫn ghi phase1/phase2 best-effort để
                # downstream stats không nhận giá trị NaN.
                t_end = time.monotonic()
                t_otp_start = timing["otp_start"]
                t_otp_end = timing["otp_end"]
                if t_otp_end > 0 and t_otp_start > 0:
                    result.otp_seconds = max(0.0, t_otp_end - t_otp_start)
                    result.phase1_seconds = max(0.0, t_otp_end - t_total_start)
                    result.phase2_seconds = max(0.0, t_end - t_otp_end)
                else:
                    result.phase1_seconds = t_end - t_total_start
                    result.phase2_seconds = 0.0
                return result

            # Backoff trước retry — CF rate-limit thường giữ ngắn (5-15s).
            backoff = 5.0 * attempt
            log(f"[hybrid] backoff {backoff:.0f}s trước attempt {attempt + 1}")
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
        finally:
            # Cleanup chỉ khi result chưa return (artefact attempt cuối cùng) —
            # case success đã return ở trên VẪN chạy finally này (return trong
            # try kích hoạt finally). Idempotent với cleanup ở except branch
            # (session=None sau khi đóng → _await_cleanup_bounded no-op).
            #
            # Fix "multi-signup launch-hang": đợi ĐỒNG BỘ-có-timeout thay vì
            # fire-and-forget. Đảm bảo browser signup này đóng (hoặc bị reaper
            # kill) XONG trước khi caller (autoreg loop) launch signup kế tiếp →
            # không tích lũy orphan Firefox. tokens.close() đã bound nội bộ nên
            # chờ này không treo.
            await _await_cleanup_bounded(
                session, tokens, log,
                timeout=_CLEANUP_WAIT_TIMEOUT_SECONDS,
            )

    # Không reachable lý thuyết — outer-loop luôn return trong cả success/fail.
    # Guard này chỉ phòng logic break vô tình.
    result.success = False
    result.error = f"hybrid exhausted {max_attempts} attempts; last: {last_error}"
    result.phase1_seconds = time.monotonic() - t_total_start
    return result
