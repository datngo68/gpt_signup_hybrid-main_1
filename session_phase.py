"""Get Session: Login ChatGPT bằng browser + password + 2FA → trả full session JSON.

Dùng browser thật vì auth.openai.com có Cloudflare JS challenge —
curl_cffi không bypass được.

Flow:
    1. Mở chatgpt.com → bootstrap NextAuth (csrf + signin/openai) → authorize URL
    2. Navigate authorize → /log-in/password
    3. Fill password → submit
    4. Nếu MFA → fill TOTP code → submit
    5. Đợi redirect chatgpt.com + session cookies
    6. Gọi /api/auth/session trong page context → return JSON
"""
from __future__ import annotations

import asyncio
import re
import shutil
import time
import uuid
from typing import Any, Callable

from _browser_retry import (
    LAUNCH_RETRY_BACKOFF as _LAUNCH_RETRY_BACKOFF,
    LAUNCH_RETRY_MAX as _LAUNCH_RETRY_MAX,
    is_driver_dead_error as _is_driver_dead_error,
    is_network_error as _is_network_error,
    parse_proxy_for_playwright as _parse_proxy,
)
from _nextauth_bootstrap import bootstrap_authorize_url
from config import ensure_runtime_dirs, load_settings, prepare_profile_dir
from totp_helper import generate_code
from user_agent_profile import CAMOUFOX_OS as _CAMOUFOX_OS


LogFn = Callable[[str], None]


class SessionError(Exception):
    """Login/session fetch failed."""


# Login errors vĩnh viễn (fatal) — KHÔNG retry để tránh login spam → lockout /
# mail-provider rate-limit. Nguồn chân lý DUY NHẤT (trước đây trùng ở
# web/manager.py + web/upi_runner.py). Dùng cho retry-gate login.
NON_RETRYABLE_LOGIN_PATTERNS: tuple[str, ...] = (
    "password verify failed",
    "mfa verify failed",
    "no mail_provider available",
    "no secret provided",
    "yêu cầu 2fa nhưng không có",
    "otp polling returned empty",
    "passwordless otp login but no mail_provider",
)


def is_fatal_login_error(exc: BaseException | str) -> bool:
    """True nếu lỗi login là fatal (không nên retry / nên xoá cache record).

    Nhận Exception hoặc message string. Fail-safe: chỉ True khi match pattern
    fatal rõ ràng — lỗi transient/mạng (không match) trả False.
    """
    msg = exc if isinstance(exc, str) else str(exc)
    lower = msg.lower()
    return any(pat in lower for pat in NON_RETRYABLE_LOGIN_PATTERNS)


def strip_cookies(session: dict[str, Any]) -> dict[str, Any]:
    """Bản copy bỏ key ``__cookies`` — flow gọi TRƯỚC khi broadcast SSE / persist
    DB jobs (R4.5). UPI giữ ``__cookies`` (cần để fill auth_sink/check_plan)."""
    if not isinstance(session, dict) or "__cookies" not in session:
        return session
    return {k: v for k, v in session.items() if k != "__cookies"}


# JS: fetch /api/auth/session trong page context chatgpt.com
_FETCH_SESSION_JS = r"""
async () => {
    const r = await fetch('/api/auth/session', {credentials: 'include'});
    if (!r.ok) throw new Error('session HTTP ' + r.status);
    return await r.json();
}
"""


async def _get_session_browser(
    *,
    email: str,
    password: str,
    secret: str | None = None,
    headless: bool = True,
    proxy: str | None = None,
    tls_insecure: bool = False,
    keep_browser_open: bool = False,
    log: LogFn = print,
) -> dict[str, Any]:
    """Login ChatGPT bằng browser thật → return session JSON.

    Retry: nếu driver pipe đóng sớm TRƯỚC khi submit password → relaunch.
    Sau khi đã submit password thì fail-fast để tránh nhiều lần thử login
    (rủi ro lockout / captcha challenge).

    keep_browser_open=True + headless=False → giữ browser mở sau khi xong
    (debug). User cancel job để đóng. Có tác dụng cả ở exit path lỗi
    sau-submit-password (để soi DOM/network).
    """
    debug_keep = keep_browser_open and not headless
    if tls_insecure:
        from config import warn_insecure_tls
        warn_insecure_tls("session_phase")
        log("[security] TLS verification DISABLED — debug mode")

    settings = load_settings()
    job_id = f"session_{uuid.uuid4().hex[:10]}"
    preferred_engine = (
        "camoufox"
        if (settings.browser_engine or "camoufox").lower() == "camoufox"
        else "chromium"
    )
    engine_order = [preferred_engine]
    if preferred_engine == "camoufox":
        engine_order.append("chromium")
    w, h = settings.browser_viewport_width, settings.browser_viewport_height
    viewport = {"width": w, "height": h}
    proxy_kwargs: dict[str, Any] = {}
    if proxy:
        proxy_kwargs["proxy"] = _parse_proxy(proxy)
        from browser_phase import _ensure_geoip_cache
        _ensure_geoip_cache(settings.runtime_dir, log=log)

    # ── Locale auto-detect theo proxy country (anti-ban Phase 6 Task 6.2) ──
    # Sync với browser_phase Task 1.4: proxy → ipinfo.io country → locale/tz/geo.
    # Login flow ít bị ban hơn signup nhưng vẫn cần consistency để tránh CF flag.
    resolved_locale = "en-US"
    resolved_timezone = None
    resolved_geo: tuple[float, float] | None = None
    if proxy:
        try:
            from _geo_locale import resolve_proxy_locale as _resolve_geo
            auto_locale, auto_tz, auto_geo, auto_cc = _resolve_geo(
                proxy, timeout=10.0, log=log,
            )
            resolved_locale = auto_locale
            resolved_timezone = auto_tz
            resolved_geo = auto_geo
            log(
                f"[session] proxy locale: country={auto_cc or '?'} "
                f"locale={resolved_locale} tz={resolved_timezone}"
            )
        except Exception as exc:
            log(f"[session] proxy locale auto-detect failed: {exc}")
    else:
        log(f"[session] locale=default {resolved_locale} (no proxy)")

    progress = {"password_submitted": False}

    def _profile_bundle(engine: str) -> tuple[Any, Any]:
        if engine == "camoufox":
            return (
                settings.profiles_dir / f"camoufox_{job_id}",
                settings.browser_camoufox_profile_dir,
            )
        return (
            settings.profile_dir_for(job_id),
            settings.browser_profile_template_dir,
        )

    async def _drive_session_flow(ctx: Any, page: Any) -> dict[str, Any]:
        device_id = str(uuid.uuid4())
        # auth_session_logging_id: defer gen → đọc cookie `oai-asli` sau khi
        # load chatgpt.com (sentinel SDK set cookie này). Đảm bảo query param
        # khớp cookie — anti-ban consistency. Xem journal
        # `260625-1224-reg-anti-ban-master-plan.md` (Task 1.1, bug C2).

        # Step 1: bootstrap
        log("[session] loading chatgpt.com...")
        await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")

        # Sentinel oracle — page-native sentinel-token (anti-ban):
        #   QuickJS (Node) thiếu canvas/WebGL/audio thật → so-token "zero-fingerprint"
        #   → OpenAI server flag account → deactivate async 1-24h sau.
        #   Oracle dùng Camoufox page hiện tại để chạy sdk.js native — fingerprint
        #   khớp Firefox thật. Construct ngay sau khi đã goto chatgpt.com để
        #   context có đủ cookies cho sentinel.openai.com requests.
        from sentinel_browser import (
            SentinelBrowserOracle as _SentinelOracle,
            verify_fingerprint_health as _verify_fp,
        )
        # Fingerprint health probe BEFORE creating oracle. Empty WebGL/canvas
        # → oracle's sdk.js call also returns weak token → still degraded.
        # Log-only here; operator may switch to headless=False if persistent.
        try:
            await _verify_fp(page, log=log)
        except Exception as _exc:
            log(f"[fingerprint] health probe exception: {_exc}")
        sentinel_oracle = _SentinelOracle(page=page, ctx=ctx, log=log)

        from _nextauth_bootstrap import read_oai_asli_from_ctx as _read_asli
        cookie_logging_id = await _read_asli(ctx)
        if cookie_logging_id:
            logging_id = cookie_logging_id
            log(f"[session] auth_session_logging_id={logging_id} (from oai-asli cookie)")
        else:
            logging_id = str(uuid.uuid4())
            log(f"[session] auth_session_logging_id={logging_id} (gen — cookie missing)")

        log("[session] bootstrapping NextAuth...")
        authorize_url = await bootstrap_authorize_url(
            page,
            email=email,
            device_id=device_id,
            logging_id=logging_id,
        )
        log("[session] authorize URL ready")

        # Step 2: navigate authorize → login page
        await page.goto(authorize_url, wait_until="domcontentloaded")
        await asyncio.sleep(3.0)
        log(f"[session] at: {page.url.split('?')[0]}")

        # Có thể ở /log-in (email step) → cần fill email trước
        # Hoặc ở /log-in/password → fill password luôn
        if "/log-in/password" not in page.url:
            email_input = None
            for sel in (
                'input[name="email"]',
                'input[type="email"]',
                'input[inputmode="email"]',
            ):
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=8000):
                        email_input = loc
                        break
                except Exception:
                    continue

            if email_input:
                log("[session] filling email...")
                await email_input.click(force=True, timeout=3000)
                await email_input.fill("")
                await email_input.type(email, delay=30)
                await asyncio.sleep(0.3)
                for btn_sel in (
                    'button[type="submit"]',
                    'button:has-text("Continue")',
                ):
                    try:
                        await page.click(btn_sel, timeout=3000)
                        log(f"[session] submitted email ({btn_sel})")
                        break
                    except Exception:
                        continue
                await asyncio.sleep(3.0)
                log(f"[session] after email: {page.url.split('?')[0]}")

        # Step 3: fill password
        pwd_input = None
        for sel in ('input[type="password"]', 'input[name="password"]'):
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=15000):
                    pwd_input = loc
                    break
            except Exception:
                continue

        if not pwd_input:
            raise SessionError(f"password input not found. URL: {page.url}")

        log("[session] filling password...")
        await pwd_input.click(force=True, timeout=3000)
        await pwd_input.fill("")
        await pwd_input.type(password, delay=40)
        await asyncio.sleep(0.3)

        # Submit password — sau đây không retry để tránh login spam
        for btn_sel in (
            'button[type="submit"]',
            'button:has-text("Continue")',
            'button:has-text("Log in")',
        ):
            try:
                await page.click(btn_sel, timeout=3000)
                log(f"[session] clicked {btn_sel}")
                break
            except Exception:
                continue
        progress["password_submitted"] = True

        # Step 4: poll chờ terminal state sau submit password
        # Terminal states:
        #   - MFA: URL chứa "mfa" hoặc content chứa "mfa"
        #   - Logged in: URL chứa "chatgpt.com" (không còn auth.openai.com)
        #   - Login error: trang vẫn ở /log-in/password + có error message
        _POST_PASSWORD_DEADLINE = 20.0  # đủ cho proxy chậm
        _POLL_INTERVAL = 1.0
        poll_end = time.monotonic() + _POST_PASSWORD_DEADLINE
        terminal_state: str | None = None  # "mfa" | "logged_in" | "login_error"

        while time.monotonic() < poll_end:
            await asyncio.sleep(_POLL_INTERVAL)
            current_url = page.url.lower()

            # Terminal: đã redirect về chatgpt.com (no MFA account hoặc MFA đã xong)
            if "chatgpt.com" in current_url and "auth.openai.com" not in current_url:
                terminal_state = "logged_in"
                log(f"[session] after password: redirected to chatgpt.com")
                break

            # Terminal: MFA challenge page
            if "mfa" in current_url:
                terminal_state = "mfa"
                log(f"[session] after password: MFA page detected (URL)")
                break

            # Check content cho trường hợp URL không chứa "mfa" nhưng page content có
            try:
                content_snippet = (await page.content())[:5000].lower()
            except Exception:
                content_snippet = ""

            if "mfa" in content_snippet:
                terminal_state = "mfa"
                log(f"[session] after password: MFA page detected (content)")
                break

            # Terminal: login error (vẫn ở password page + có thông báo lỗi)
            if "/log-in/password" in current_url:
                _error_selectors = (
                    '[data-testid="error-message"]',
                    '.error-message',
                    '[role="alert"]',
                )
                for err_sel in _error_selectors:
                    try:
                        if await page.locator(err_sel).first.is_visible(timeout=300):
                            terminal_state = "login_error"
                            break
                    except Exception:
                        continue
                if terminal_state == "login_error":
                    err_text = ""
                    try:
                        err_text = await page.locator(err_sel).first.inner_text(timeout=1000)
                    except Exception:
                        pass
                    raise SessionError(
                        f"login failed (password error): {err_text or 'unknown'}. "
                        f"URL: {page.url}"
                    )

        if terminal_state is None:
            # Deadline hết mà chưa đạt terminal state → check lần cuối
            current_url = page.url.lower()
            if "mfa" in current_url:
                terminal_state = "mfa"
            elif "chatgpt.com" in current_url and "auth.openai.com" not in current_url:
                terminal_state = "logged_in"
            else:
                raise SessionError(
                    f"timeout waiting for post-password redirect "
                    f"({_POST_PASSWORD_DEADLINE}s). URL: {page.url}"
                )

        log(f"[session] post-password state: {terminal_state}")

        # Handle MFA
        if terminal_state == "mfa":
            if not secret:
                raise SessionError("account yêu cầu 2FA nhưng không có secret")

            log("[session] generating TOTP...")
            code = generate_code(secret)

            otp_input = None
            for sel in (
                'input[name="code"]',
                'input[inputmode="numeric"]',
                'input[autocomplete="one-time-code"]',
                'input[maxlength="6"]',
            ):
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=8000):
                        otp_input = loc
                        break
                except Exception:
                    continue

            if not otp_input:
                raise SessionError(f"TOTP input not found. URL: {page.url}")

            await otp_input.click(force=True, timeout=3000)
            await otp_input.fill("")
            await otp_input.type(code, delay=60)
            log(f"[session] TOTP code entered")
            await asyncio.sleep(0.5)

            for btn_sel in (
                'button[type="submit"]',
                'button:has-text("Continue")',
                'button:has-text("Verify")',
            ):
                try:
                    await page.click(btn_sel, timeout=3000)
                    log(f"[session] clicked {btn_sel}")
                    break
                except Exception:
                    continue

            await asyncio.sleep(3.0)
            log(f"[session] after MFA submit: {page.url.split('?')[0]}")

        # Step 5: đợi redirect về chatgpt.com + session cookies
        deadline = time.monotonic() + 30.0
        session_ready = False
        while time.monotonic() < deadline:
            cookies = await ctx.cookies("https://chatgpt.com/")
            names = {c["name"] for c in cookies}
            has_session = (
                "__Secure-next-auth.session-token" in names
                or "__Secure-next-auth.session-token.0" in names
            )
            if has_session:
                log("[session] session cookies ready")
                session_ready = True
                break
            if "chatgpt.com" in page.url and "auth.openai.com" not in page.url:
                await asyncio.sleep(1.0)
                continue
            await asyncio.sleep(1.0)
        if not session_ready:
            raise SessionError(f"timeout waiting session cookies. URL: {page.url}")

        # Đảm bảo đang ở chatgpt.com
        if "chatgpt.com" not in page.url:
            await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
            await asyncio.sleep(2.0)

        # Step 6: fetch session JSON
        log("[session] fetching /api/auth/session...")
        session_data = await page.evaluate(_FETCH_SESSION_JS)
        if not isinstance(session_data, dict) or not session_data.get("accessToken"):
            raise SessionError(
                f"session response invalid: {str(session_data)[:200]}"
            )

        log(
            f"[session] ✓ done — user: "
            f"{session_data.get('user', {}).get('email', '?')}"
        )

        # Gắn __cookies vào session (đồng nhất với get_session_pure_request):
        # UPI cần cookies cho auth_sink/check_plan. Caller KHÔNG dùng cookies
        # (Get Session) PHẢI strip __cookies trước khi broadcast SSE / persist DB.
        try:
            session_data["__cookies"] = await ctx.cookies("https://chatgpt.com/")
        except Exception as exc:
            log(f"[session] cookie export failed: {exc}")
        return session_data

    async def _run_camoufox_once(profile_dir: Any) -> dict[str, Any]:
        from camoufox.async_api import AsyncCamoufox

        extra_config: dict = {}
        screen_kwargs: dict[str, Any] = {}

        if not settings.browser_random_screen:
            from camoufox.utils import Screen as _Screen

            chrome_h = 85
            extra_config["window.innerWidth"] = w
            extra_config["window.innerHeight"] = h
            extra_config["window.outerWidth"] = w
            extra_config["window.outerHeight"] = h + chrome_h
            extra_config["screen.width"] = w
            extra_config["screen.height"] = h + chrome_h
            extra_config["screen.availWidth"] = w
            extra_config["screen.availHeight"] = h + chrome_h
            screen_kwargs["screen"] = _Screen(
                min_width=w, max_width=w, min_height=h + chrome_h, max_height=h + chrome_h,
            )
            screen_kwargs["i_know_what_im_doing"] = True

        cf = AsyncCamoufox(
            headless=headless,
            persistent_context=True,
            user_data_dir=str(profile_dir),
            os=list(_CAMOUFOX_OS),
            viewport=viewport,
            locale=resolved_locale,
            ignore_https_errors=tls_insecure,
            geoip=bool(proxy),
            # Anti-detect hardening (Phase 9 audit) — sync với browser_phase
            block_webrtc=True,
            humanize=True,
            # main_world_eval=True (Phase 11.2): sdk.js / Sentinel SDK gọi
            # TypedArray + crypto cross-context. Firefox Xray wrapper reject
            # → page.evaluate gọi SDK fail. Bật main world cho Camoufox.
            main_world_eval=True,
            # fingerprint_preset removed (Phase 11.1 — Camoufox in this venv
            # doesn't support the kwarg; per-context add_init_script seeds
            # handle persona rotation in the sidecar pool).
            config=extra_config,
            **screen_kwargs,
            **proxy_kwargs,
        )
        ctx = await cf.__aenter__()
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            return await _drive_session_flow(ctx, page)
        finally:
            if debug_keep:
                log("[session] debug: giữ browser mở — cancel job để đóng")
            else:
                try:
                    await cf.__aexit__(None, None, None)
                except Exception:
                    pass

    async def _run_chromium_once(profile_dir: Any) -> dict[str, Any]:
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        try:
            channel = settings.browser_channel or None
            chrome_ctx_kwargs: dict[str, Any] = {
                "user_data_dir": str(profile_dir),
                "headless": headless,
                "channel": channel,
                "viewport": viewport,
                "locale": resolved_locale,
                "ignore_https_errors": tls_insecure,
            }
            if resolved_timezone:
                chrome_ctx_kwargs["timezone_id"] = resolved_timezone
            if resolved_geo:
                lat, lon = resolved_geo
                chrome_ctx_kwargs["geolocation"] = {"latitude": lat, "longitude": lon}
                chrome_ctx_kwargs["permissions"] = ["geolocation"]
            ctx = await playwright.chromium.launch_persistent_context(
                **chrome_ctx_kwargs,
                **proxy_kwargs,
            )
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                return await _drive_session_flow(ctx, page)
            finally:
                if debug_keep:
                    log("[session] debug: giữ browser mở — cancel job để đóng")
                else:
                    try:
                        await ctx.close()
                    except Exception:
                        pass
        finally:
            if not debug_keep:
                await playwright.stop()

    runners = {
        "camoufox": _run_camoufox_once,
        "chromium": _run_chromium_once,
    }
    last_exc: BaseException | None = None
    try:
        for engine_index, engine in enumerate(engine_order):
            profile_dir, template_dir = _profile_bundle(engine)
            ensure_runtime_dirs(settings, extra=(profile_dir,))
            prepare_profile_dir(
                profile_dir=profile_dir,
                template_dir=template_dir,
                use_template=settings.browser_use_profile_template,
            )
            if engine_index > 0:
                log(f"[session] fallback browser engine: {engine}")

            for attempt in range(1, _LAUNCH_RETRY_MAX + 1):
                progress["password_submitted"] = False
                try:
                    return await runners[engine](profile_dir)
                except SessionError:
                    raise
                except Exception as exc:
                    last_exc = exc
                    retryable = _is_driver_dead_error(exc) or _is_network_error(exc)
                    if not retryable:
                        raise SessionError(
                            f"browser launch/driver error: {type(exc).__name__}: {exc}"
                        ) from exc
                    if progress["password_submitted"]:
                        log(
                            f"[session] lỗi sau khi đã submit password — "
                            f"không retry để tránh login spam: "
                            f"{type(exc).__name__}: {exc}"
                        )
                        raise SessionError(
                            f"lỗi sau submit password (không retry): {exc}"
                        ) from exc
                    err_kind = "network/proxy" if _is_network_error(exc) else "driver pipe"
                    log(
                        f"[session] {err_kind} error "
                        f"(attempt {attempt}/{_LAUNCH_RETRY_MAX}): "
                        f"{type(exc).__name__}: {exc}"
                    )
                    if attempt >= _LAUNCH_RETRY_MAX:
                        break
                    shutil.rmtree(profile_dir, ignore_errors=True)
                    prepare_profile_dir(
                        profile_dir=profile_dir,
                        template_dir=template_dir,
                        use_template=settings.browser_use_profile_template,
                    )
                    await asyncio.sleep(_LAUNCH_RETRY_BACKOFF)

            if engine == "camoufox" and engine_index + 1 < len(engine_order):
                log(
                    "[session] camoufox chết sớm ở auth redirect — "
                    "thử fallback sang chromium"
                )
                continue

            if last_exc is not None and (_is_driver_dead_error(last_exc) or _is_network_error(last_exc)):
                raise SessionError(
                    f"retryable error sau {_LAUNCH_RETRY_MAX} lần thử: {last_exc}"
                ) from last_exc

        raise SessionError("browser launch failed without specific error")
    finally:
        # Debug mode + headed → giữ profile để soi (không cleanup).
        # Fail mode (raise) trên 1 engine vẫn cần dọn profile của engine đó
        # nếu không giữ browser mở.
        if not debug_keep:
            for engine in engine_order:
                profile_dir, _ = _profile_bundle(engine)
                shutil.rmtree(profile_dir, ignore_errors=True)


async def get_session(
    *,
    email: str,
    password: str,
    secret: str | None = None,
    headless: bool = True,
    proxy: str | None = None,
    tls_insecure: bool = False,
    keep_browser_open: bool = False,
    log: LogFn = print,
) -> dict[str, Any]:
    """Async: login ChatGPT → return full /api/auth/session JSON."""
    return await _get_session_browser(
        email=email,
        password=password,
        secret=secret,
        headless=headless,
        proxy=proxy,
        tls_insecure=tls_insecure,
        keep_browser_open=keep_browser_open,
        log=log,
    )


def get_session_sync(
    *,
    email: str,
    password: str,
    secret: str | None = None,
    headless: bool = True,
    proxy: str | None = None,
    tls_insecure: bool = False,
    keep_browser_open: bool = False,
    log: LogFn = print,
) -> dict[str, Any]:
    """Sync wrapper."""
    return asyncio.run(get_session(
        email=email,
        password=password,
        secret=secret,
        headless=headless,
        proxy=proxy,
        tls_insecure=tls_insecure,
        keep_browser_open=keep_browser_open,
        log=log,
    ))


# ─────────────────────────────────────────────────────────────────────
# HTTP-only session fetch (no browser) — dùng khi đã có cookies sẵn từ Phase 2.
# ─────────────────────────────────────────────────────────────────────


def _cookies_to_header(cookies: Any) -> str:
    """Convert cookies (list[dict] | dict | None) → "name=value; name=value" string.

    Hỗ trợ 2 format:
      - list[dict]: Playwright/SignupResult format [{"name":..., "value":..., "domain":...}, ...]
        → chỉ giữ cookies thuộc domain chatgpt.com (hoặc rỗng).
      - dict: {name: value} flat.
    """
    if not cookies:
        return ""
    pairs: list[str] = []
    if isinstance(cookies, list):
        for c in cookies:
            if not isinstance(c, dict):
                continue
            name = c.get("name")
            value = c.get("value")
            if not name or value is None:
                continue
            domain = (c.get("domain") or "").lstrip(".").lower()
            # Chỉ giữ cookies dùng được cho chatgpt.com
            if domain and "chatgpt.com" not in domain:
                continue
            pairs.append(f"{name}={value}")
    elif isinstance(cookies, dict):
        for name, value in cookies.items():
            if value is None:
                continue
            pairs.append(f"{name}={value}")
    return "; ".join(pairs)


async def fetch_session_via_http(
    *,
    cookies: Any,
    proxy: str | None = None,
    timeout: float = 30.0,
    impersonate: str | None = None,
) -> dict[str, Any]:
    """GET https://chatgpt.com/api/auth/session bằng curl_cffi với cookies có sẵn.

    Args:
        cookies: list[dict] (Playwright format) hoặc dict {name: value}.
        proxy: HTTP/HTTPS proxy URL.
        timeout: Request timeout (seconds).
        impersonate: curl_cffi browser impersonation key. None → dùng
            ``CURL_IMPERSONATE_PRIMARY`` từ user_agent_profile (đồng bộ với UA
            persona, tránh mismatch TLS fingerprint).

    Returns:
        Full session JSON (dict) với accessToken không rỗng.

    Raises:
        SessionError: HTTP non-200, JSON parse fail, hoặc accessToken thiếu/rỗng.
    """
    from curl_cffi.requests import AsyncSession
    from user_agent_profile import (
        CURL_IMPERSONATE_PRIMARY,
        SEC_CH_UA,
        SEC_CH_UA_MOBILE,
        SEC_CH_UA_PLATFORM,
        WINDOWS_USER_AGENT,
    )

    if impersonate is None:
        impersonate = CURL_IMPERSONATE_PRIMARY

    cookie_header = _cookies_to_header(cookies)
    if not cookie_header:
        raise SessionError("không có cookie chatgpt.com để fetch session")

    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {
        "Cookie": cookie_header,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://chatgpt.com/",
        "User-Agent": WINDOWS_USER_AGENT,
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": SEC_CH_UA_PLATFORM,
    }

    async with AsyncSession(impersonate=impersonate, proxies=proxies) as sess:
        try:
            resp = await sess.get(
                "https://chatgpt.com/api/auth/session",
                headers=headers,
                timeout=timeout,
            )
        except Exception as exc:
            raise SessionError(f"network error: {exc}") from exc

    if resp.status_code != 200:
        body = (resp.text or "")[:200]
        raise SessionError(f"HTTP {resp.status_code}: {body}")

    try:
        data = resp.json()
    except Exception as exc:
        raise SessionError(f"JSON parse fail: {exc}") from exc

    if not isinstance(data, dict):
        raise SessionError(f"response không phải JSON object: {type(data).__name__}")

    token = data.get("accessToken")
    if not isinstance(token, str) or not token.strip():
        raise SessionError("accessToken thiếu hoặc rỗng")

    return data


# JWT/access-token có prefix "eyJ". Scrub khỏi error message trước khi raise:
# check_plan log lỗi qua _job_log → broadcast SSE tới mọi client, nên token
# tuyệt đối không được lọt vào chuỗi lỗi.
_JWT_TOKEN_RE = re.compile(r"eyJ[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)*")


def _scrub_jwt(text: str) -> str:
    return _JWT_TOKEN_RE.sub("eyJ…[REDACTED]", text)


def _parse_entitlement_plan(data: dict[str, Any]) -> dict[str, Any]:
    """Parse entitlement block từ /backend-api/accounts/check/v4 → plan dict.

    Pure (không network) nên dễ unit-test. Shape thực tế đã verify:
        accounts.default.entitlement.{subscription_plan, has_active_subscription,
                                      expires_at, subscription_id}

    ``subscription_plan`` (vd ``chatgptplusplan``) → label gọn: bỏ prefix
    ``chatgpt`` + suffix ``plan`` → ``plus`` / ``free`` / ``team`` / ``pro`` …

    ``is_plus`` strict Plus-only: chỉ True khi subscription active VÀ label là
    đúng ``plus`` — Pro/Team/Enterprise active KHÔNG tính là Plus (badge dành
    riêng nhãn Plus; auto-poll chỉ dừng khi thấy Plus thật).

    Mọi shape thiếu/sai → trả blank (không raise) để caller fail-soft.
    """
    blank = {"plan": None, "is_plus": False, "has_active_subscription": False, "expires": None}
    if not isinstance(data, dict):
        return blank
    accounts = data.get("accounts")
    if not isinstance(accounts, dict) or not accounts:
        return blank
    acct = accounts.get("default")
    if not isinstance(acct, dict):
        # Không có key "default" → lấy account đầu tiên là dict.
        acct = next((v for v in accounts.values() if isinstance(v, dict)), None)
    if not isinstance(acct, dict):
        return blank
    ent = acct.get("entitlement")
    if not isinstance(ent, dict):
        return blank

    raw_plan = ent.get("subscription_plan")
    label: str | None = None
    if isinstance(raw_plan, str) and raw_plan.strip():
        s = raw_plan.strip().lower()
        if s.startswith("chatgpt"):
            s = s[len("chatgpt"):]
        if s.endswith("plan"):
            s = s[: -len("plan")]
        label = s or None

    has_active = bool(ent.get("has_active_subscription"))
    return {
        "plan": label,
        "is_plus": has_active and label == "plus",
        "has_active_subscription": has_active,
        "expires": ent.get("expires_at"),
    }


async def fetch_account_entitlement(
    *,
    access_token: str,
    cookies: Any = None,
    proxy: str | None = None,
    timeout: float = 20.0,
    impersonate: str | None = None,
) -> dict[str, Any]:
    """GET /backend-api/accounts/check/v4 → đọc entitlement plan LIVE.

    Khác ``fetch_session_via_http`` (đọc ``/api/auth/session`` cache, lag so với
    subscription thật): endpoint này đọc entitlement trực tiếp từ backend nên
    phản ánh upgrade Plus ngay cả khi accessToken được mint *trước* lúc upgrade.

    Auth = Bearer accessToken với **header recipe backend-api đầy đủ** (Origin +
    x-openai-target-path/route + OAI-Language + UA/sec-ch-ua persona). Recipe
    tối giản (chỉ Bearer + UA) bị Cloudflare chặn 403 — đã verify thực tế.

    Args:
        access_token: JWT accessToken (login token đủ để auth Bearer).
        cookies: optional, gắn thêm Cookie header (verify cho thấy KHÔNG cần,
            Bearer-only đã 200; giữ optional để dự phòng).
        proxy: HTTP/HTTPS proxy URL (proxy đã mint token); None = IP trần (đã
            verify no-proxy vẫn 200, không bắt buộc route qua proxy).
        timeout: request timeout (giây). Mặc định 20s (KHÁC ``fetch_session_via_http``
            mặc định 30s — đừng nhầm).
        impersonate: curl_cffi impersonate key. None → persona primary.

    Returns:
        dict ``{plan, is_plus, has_active_subscription, expires}`` qua
        ``_parse_entitlement_plan``.

    Raises:
        SessionError: token rỗng, network error, non-200, hoặc JSON parse fail.
            Error message KHÔNG kèm response body (endpoint identity/oauth có thể
            echo token vào body) và đã scrub mọi chuỗi prefix ``eyJ``.
    """
    from curl_cffi.requests import AsyncSession
    from user_agent_profile import (
        CURL_IMPERSONATE_PRIMARY,
        SEC_CH_UA,
        SEC_CH_UA_MOBILE,
        SEC_CH_UA_PLATFORM,
        WINDOWS_USER_AGENT,
    )

    if not isinstance(access_token, str) or not access_token.strip():
        raise SessionError("access_token thiếu hoặc rỗng")

    if impersonate is None:
        impersonate = CURL_IMPERSONATE_PRIMARY

    url = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
    target = "/backend-api/accounts/check/v4-2023-04-27"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "*/*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "User-Agent": WINDOWS_USER_AGENT,
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": SEC_CH_UA_PLATFORM,
        "x-openai-target-path": target,
        "x-openai-target-route": target,
        "OAI-Language": "en-IN",
    }
    cookie_header = _cookies_to_header(cookies) if cookies else ""
    if cookie_header:
        headers["Cookie"] = cookie_header

    proxies = {"http": proxy, "https": proxy} if proxy else None
    async with AsyncSession(impersonate=impersonate, proxies=proxies) as sess:
        try:
            resp = await sess.get(url, headers=headers, timeout=timeout)
        except Exception as exc:
            raise SessionError(f"network error: {_scrub_jwt(str(exc))}") from exc

    # Chỉ kèm status code, KHÔNG response body — body có thể echo lại token.
    if resp.status_code != 200:
        raise SessionError(f"HTTP {resp.status_code}")

    try:
        data = resp.json()
    except Exception as exc:
        raise SessionError(f"JSON parse fail: {_scrub_jwt(str(exc))}") from exc

    return _parse_entitlement_plan(data)


# ─────────────────────────────────────────────────────────────────────
# Pure-request login (no browser) — full protocol for existing accounts
# ─────────────────────────────────────────────────────────────────────


def _resolve_login_flow(explicit: str | None) -> str:
    """Resolve flow login cho ``get_session_pure_request``.

    Ưu tiên: ``explicit`` (caller truyền) > Settings Store ``session.login_flow``
    > default ``"anti409"`` (flow dev — warm + pre-set oai-did +
    auth_session_logging_id + sentinel ``password_verify`` + assume-password,
    chống HTTP 409 invalid_state). DB chưa mở/lỗi → fallback default, KHÔNG raise.

    ``"legacy"`` = flow cũ (``_step_auth_url`` + authorize/continue fallback +
    sentinel ``login``) — dò được passwordless/OTP khi landing không rõ.
    """
    if explicit in ("legacy", "anti409"):
        return explicit
    try:
        from db import get_engine, get_settings_repo
        val = get_settings_repo(get_engine()).get("session.login_flow")
        if val in ("legacy", "anti409"):
            return val
    except Exception:  # noqa: BLE001 — DB chưa sẵn sàng → dùng default
        pass
    return "anti409"


async def get_session_pure_request(
    *,
    email: str,
    password: str,
    secret: str | None = None,
    proxy: str | None = None,
    mail_provider: Any = None,
    login_flow: str | None = None,
    log: LogFn = print,
) -> dict[str, Any]:
    """Login ChatGPT via pure HTTP requests → return /api/auth/session JSON.

    Protocol flow (no browser):
      1. chatgpt.com CSRF + signin/openai → authorize URL
      2. OAuth init → device_id
      3. Sentinel token
      4. authorize/continue (email)
      5. IF passwordless → OTP verify (requires mail_provider)
         IF password → password/verify
      6. MFA verify (if needed, using TOTP secret)
      7. Follow redirect chain → callback
      8. Consume callback → session_token
      9. GET /api/auth/session → full JSON
    """
    from request_phase import (
        _create_session,
        _step_csrf,
        _step_auth_url,
        _get_sentinel_token,
        _get_sentinel_token_async,
        _common_headers,
        _step_authorize_continue,
        _step_follow_redirects,
        _consume_callback,
        _get_session_tokens,
        _step_resend_otp,
        _step_verify_otp,
        _is_rotatable_error,
        _IMPERSONATE_CANDIDATES,
        RequestPhaseError,
        USER_AGENT,
    )
    from user_agent_profile import (
        SEC_CH_UA as _SEC_CH_UA,
        SEC_CH_UA_MOBILE as _SEC_CH_UA_MOBILE,
        SEC_CH_UA_PLATFORM as _SEC_CH_UA_PLATFORM,
    )
    from totp_helper import generate_code as _generate_totp
    from urllib.parse import urljoin
    import asyncio as _asyncio
    from datetime import datetime, timezone

    def _run_async_poll(provider, recipient, started_at, log) -> str:
        """Poll OTP from async mail provider inside a sync thread (new event loop)."""
        loop = _asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                provider.poll_otp(
                    recipient=recipient,
                    started_at=started_at,
                    timeout_seconds=180.0,
                    poll_interval_seconds=4.0,
                    log=log,
                )
            )
        finally:
            loop.close()

    def _sync() -> dict[str, Any]:
        flow_mode = _resolve_login_flow(login_flow)
        log(f"[session-req] login_flow={flow_mode}")
        # ─────────────────────────────────────────────────────────────
        # Bootstrap helper: CSRF + signin/openai + GET /authorize.
        # `use_login_hint=True` → server có thể auto-redirect thẳng tới
        # /log-in/password (fast path khi account đã tồn tại + có password).
        # `use_login_hint=False` → state machine ở mức "đợi email submission",
        # cần authorize/continue để rẽ flow (slow path / fallback).
        # ─────────────────────────────────────────────────────────────
        def _do_bootstrap(*, use_login_hint: bool) -> tuple[Any, str, str, str]:
            """Returns (session, device_id, auth_url, landing_url).

            Có TLS fingerprint rotation. Raise nếu bootstrap fail trên mọi
            impersonate candidate.
            """
            last_exc: BaseException | None = None
            for idx, imp in enumerate(_IMPERSONATE_CANDIDATES):
                sess = _create_session(proxy=proxy, impersonate=imp)
                try:
                    if idx > 0:
                        log(f"[session-req] TLS rotation: retrying with impersonate={imp}")
                    did = str(__import__('uuid').uuid4())
                    if flow_mode == "anti409":
                        # ── Anti-detection (flow dev) ──────────────────────────
                        # Pre-set oai-did + warm chatgpt.com + signin/openai kèm
                        # auth_session_logging_id → server thấy device_id/auth
                        # session consistent ngay từ đầu, tránh /password/verify
                        # reject "invalid_state". Xem journal 260617-1755.
                        from urllib.parse import urlencode as _urlencode
                        try:
                            sess.cookies.set("oai-did", did, domain="chatgpt.com")
                        except Exception:
                            pass

                        # Inject `_dd_s` Datadog cookie (anti-ban Phase 6 Task 6.3 + bug M3).
                        # Browser thật luôn có khi load chatgpt.com — pure_request không
                        # có Datadog SDK chạy nên phải inject manual.
                        try:
                            from _datadog_session import inject_dd_s as _inject_dd_s
                            _inject_dd_s(sess, domain=".chatgpt.com", rum=0, log=log)
                        except Exception as _dd_exc:
                            log(f"[session-req] _dd_s inject failed (continue): {_dd_exc}")

                        _warm_ua_headers = {
                            "User-Agent": USER_AGENT,
                            "sec-ch-ua": _SEC_CH_UA,
                            "sec-ch-ua-mobile": _SEC_CH_UA_MOBILE,
                            "sec-ch-ua-platform": _SEC_CH_UA_PLATFORM,
                            "Accept-Language": "en-US,en;q=0.9",
                        }
                        try:
                            sess.get(
                                "https://chatgpt.com/",
                                headers={
                                    **_warm_ua_headers,
                                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                                    "Sec-Fetch-Mode": "navigate",
                                    "Sec-Fetch-Dest": "document",
                                    "Upgrade-Insecure-Requests": "1",
                                },
                                timeout=20,
                                allow_redirects=True,
                            )
                            sess.get(
                                "https://chatgpt.com/backend-anon/accounts/check/v4-2023-04-27",
                                params={"timezone_offset_min": -420},
                                headers={
                                    **_warm_ua_headers,
                                    "Accept": "*/*",
                                    "Referer": "https://chatgpt.com/",
                                    "oai-device-id": did,
                                },
                                timeout=20,
                            )
                            sess.get(
                                "https://chatgpt.com/api/auth/providers",
                                headers={
                                    **_warm_ua_headers,
                                    "Accept": "*/*",
                                    "Referer": "https://chatgpt.com/",
                                },
                                timeout=20,
                            )
                        except Exception as _w_exc:
                            log(f"[session-req] warming non-fatal: {_w_exc}")
                        # Server có thể re-issue oai-did sau warming → đọc lại làm
                        # device_id chuẩn (cookie ↔ ext-oai-did query phải MATCH).
                        did = sess.cookies.get("oai-did", "") or did
                        csrf = _step_csrf(sess, log)
                        # auth_session_logging_id: đọc cookie `oai-asli` đã set
                        # sau warming. Browser thật luôn dùng cookie này làm
                        # query param. Code cũ gen UUID khác cookie → server
                        # cross-check fail. Xem journal `260625-1224` Task 1.1.
                        from _nextauth_bootstrap import read_oai_asli_from_session as _read_asli_sync
                        _asli = _read_asli_sync(sess) or str(__import__('uuid').uuid4())
                        _au_params = {
                            "prompt": "login",
                            "ext-passkey-client-capabilities": "11111",
                            "ext-oai-did": did,
                            "auth_session_logging_id": _asli,
                            "screen_hint": "login_or_signup",
                        }
                        if use_login_hint:
                            _au_params["login_hint"] = email
                        _au_headers = _common_headers("https://chatgpt.com/auth/login")
                        _au_headers["Content-Type"] = "application/x-www-form-urlencoded"
                        _au_resp = sess.post(
                            "https://chatgpt.com/api/auth/signin/openai?" + _urlencode(_au_params),
                            headers=_au_headers,
                            data={
                                "csrfToken": csrf,
                                "callbackUrl": "https://chatgpt.com/",
                                "json": "true",
                            },
                            timeout=30,
                        )
                        if _au_resp.status_code != 200:
                            raise SessionError(f"signin/openai failed: HTTP {_au_resp.status_code}")
                        au = (_au_resp.json() or {}).get("url", "")
                        if not au:
                            raise SessionError("signin/openai: no URL in response")
                        log(f"[session-req] auth URL: {au[:80]}...")
                    else:
                        # ── Legacy flow main: dùng _step_auth_url helper ──────
                        csrf = _step_csrf(sess, log)
                        au = _step_auth_url(
                            sess, csrf, log,
                            device_id=did,
                            login_hint=email if use_login_hint else "",
                        )
                    # OAuth init: GET authorize MIMIC top-level navigation của browser
                    # (sec-fetch-* + upgrade-insecure-requests + referer chatgpt.com/).
                    # GET authorize KHÔNG có sec-fetch-mode=navigate sẽ bị xử như
                    # XHR và trả thẳng SPA 200 thay vì 302 sang /log-in/password
                    # (xác minh qua HAR thật, RID 424).
                    r = sess.get(
                        au,
                        headers={
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                            "Accept-Language": "en-US,en;q=0.9",
                            "Accept-Encoding": "gzip, deflate, br, zstd",
                            "Referer": "https://chatgpt.com/",
                            "Connection": "keep-alive",
                            "Upgrade-Insecure-Requests": "1",
                            "Sec-Fetch-Dest": "document",
                            "Sec-Fetch-Mode": "navigate",
                            "Sec-Fetch-Site": "cross-site",
                            "Sec-Fetch-User": "?1",
                            "User-Agent": USER_AGENT,
                            "sec-ch-ua": _SEC_CH_UA,
                            "sec-ch-ua-mobile": _SEC_CH_UA_MOBILE,
                            "sec-ch-ua-platform": _SEC_CH_UA_PLATFORM,
                        },
                        timeout=30,
                        allow_redirects=True,
                    )
                    land = str(getattr(r, "url", "") or au)
                    did = sess.cookies.get("oai-did", "") or did
                    return sess, did, au, land
                except Exception as e:
                    last_exc = e
                    try:
                        sess.close()
                    except Exception:
                        pass
                    # Rotate impersonate khi: TLS handshake fail HOẶC CF 403
                    # flag JA3 (cùng impersonate retry vô ích — phải đổi
                    # fingerprint Chrome trong _IMPERSONATE_CANDIDATES).
                    if _is_rotatable_error(e) and idx < len(_IMPERSONATE_CANDIDATES) - 1:
                        log(
                            f"[session-req] fingerprint rotation: "
                            f"{type(e).__name__} → next impersonate"
                        )
                        continue
                    # Convert RequestPhaseError → SessionError tại boundary để
                    # caller (upi_runner) chỉ cần catch SessionError. Trước fix
                    # này, RequestPhaseError từ _step_auth_url leak qua loop login
                    # retry và thành unhandled exception (không match
                    # `except SessionError`).
                    if isinstance(e, RequestPhaseError):
                        raise SessionError(f"bootstrap failed: {e}") from e
                    raise
            if last_exc:
                if isinstance(last_exc, RequestPhaseError):
                    raise SessionError(f"bootstrap failed: {last_exc}") from last_exc
                raise last_exc
            raise SessionError("bootstrap failed (no auth_url)")

        def _detect_flow_from_landing(land_url: str) -> str:
            """Map landing URL → 'password' | 'otp' | '' (undetermined)."""
            if "/log-in/password" in land_url:
                return "password"
            if "/email-verification" in land_url:
                return "otp"
            # MFA challenge ngụ ý đã qua password (cookie còn sống) → branch password.
            if "/mfa-challenge" in land_url:
                return "password"
            # Passkey enrollment → account đã authed, skip passkey lấy callback.
            if "/login-enroll-passkey" in land_url:
                return "password"
            return ""

        # ─────────────────────────────────────────────────────────────
        # Fast path: bootstrap WITH login_hint. Nếu account tồn tại + có
        # password, server thường redirect thẳng tới /log-in/password →
        # skip authorize/continue (tránh bug HTTP 409 invalid_state khi
        # state machine đã pre-set bởi login_hint).
        # ─────────────────────────────────────────────────────────────
        session, device_id, auth_url, landing = _do_bootstrap(use_login_hint=True)
        log(f"[session-req] landing: {landing[:90]!r}")

        page_type = ""
        continue_url = ""
        flow = _detect_flow_from_landing(landing)

        # ─────────────────────────────────────────────────────────────
        # Fallback: landing không xác định → re-bootstrap KHÔNG login_hint
        # để state machine ở "đợi email submission", rồi gọi authorize/continue
        # clean. Đây là lý do bug "invalid_state HTTP 409" — gọi
        # authorize/continue với email khi server đã pre-set login_hint sẽ
        # bị reject vì state đã ở step sau.
        # ─────────────────────────────────────────────────────────────
        if not flow and flow_mode == "anti409":
            # anti409: KHÔNG dùng authorize/continue (nguồn HTTP 409 invalid_state).
            # Input combo luôn có password → assume password; /password/verify tự
            # reject 4xx rõ ràng nếu state sai (rõ hơn 409). passwordless mà landing
            # không lộ /email-verification sẽ fail ở đây — dùng flow "legacy" cho mix
            # account passwordless.
            log(
                "[session-req] landing không nhận diện (anti409) — assume password "
                "+ thử /api/accounts/password/verify trực tiếp"
            )
            flow = "password"

        if not flow:
            log("[session-req] landing không xác định — re-bootstrap KHÔNG login_hint để gọi authorize/continue clean...")
            try:
                session.close()
            except Exception:
                pass
            session, device_id, auth_url, landing = _do_bootstrap(use_login_hint=False)
            log(f"[session-req] retry landing: {landing[:90]!r}")
            flow = _detect_flow_from_landing(landing)

            if not flow:
                # State machine giờ clean (no login_hint preset) → authorize/continue
                # an toàn để drive flow. Vẫn catch 409 invalid_state để báo lỗi rõ.
                log("[session-req] resolve qua authorize/continue (no login_hint)...")
                # pure_request mode: no browser oracle → use sync QuickJS path.
                # ``_sync`` is sync (wrapped via ``asyncio.to_thread``), so we
                # MUST NOT ``await`` here. The ``_async`` variant is only for
                # browser-mode session where ``sentinel_oracle`` exists.
                ac_sentinel = _get_sentinel_token(
                    session, device_id, "login", log,
                )
                try:
                    ac_data = _step_authorize_continue(
                        session, email, ac_sentinel,
                        screen_hint="login",
                        referer="https://auth.openai.com/log-in",
                        device_id=device_id,
                        log=log,
                    )
                except RequestPhaseError as exc:
                    msg = str(exc)
                    if "HTTP 409" in msg and "invalid_state" in msg:
                        raise SessionError(
                            "authorize/continue bị OpenAI từ chối với HTTP 409 invalid_state. "
                            "State machine không đồng bộ — có thể do proxy chậm khiến state hết hạn "
                            "hoặc account đang ở flow đặc biệt (passwordless/blocked). "
                            f"Detail: {msg}"
                        ) from exc
                    raise SessionError(f"authorize/continue failed: {msg}") from exc
                ac_page = ac_data.get("page", {}) if isinstance(ac_data, dict) else {}
                page_type = (ac_page.get("type") or "").strip()
                continue_url = (ac_data.get("continue_url") or "").strip()
                log(f"[session-req] authorize/continue → page_type={page_type!r} continue_url={continue_url[:80]!r}")
                if page_type == "login_password" or "/log-in/password" in continue_url:
                    flow = "password"
                elif page_type in ("email_otp_verification", "email_verification") or "/email-verification" in continue_url:
                    flow = "otp"
                else:
                    raise SessionError(
                        f"unexpected login state: page_type={page_type!r} landing={landing[:80]!r}"
                    )

        try:

            # ── Branch A: password login ──
            if flow == "password":
                log("[session-req] password login flow...")
                if flow_mode == "anti409":
                    # Warm sentinel.openai.com sdk.js + frame.html với Referer
                    # /log-in/password → server nhận token là từ password verify
                    # flow (giảm reject token yếu).
                    try:
                        session.get(
                            "https://sentinel.openai.com/backend-api/sentinel/sdk.js",
                            headers={"Referer": "https://auth.openai.com/log-in/password"},
                            timeout=15,
                        )
                        session.get(
                            "https://sentinel.openai.com/backend-api/sentinel/frame.html",
                            params={"sv": "20260219f9f6"},
                            headers={"Referer": "https://auth.openai.com/log-in/password"},
                            timeout=15,
                        )
                    except Exception as _warm_exc:
                        log(f"[session-req] sentinel warming non-fatal: {_warm_exc}")
                headers = _common_headers("https://auth.openai.com/log-in/password")
                headers["Content-Type"] = "application/json"
                if device_id:
                    headers["oai-device-id"] = device_id
                # anti409: sentinel flow="password_verify" (reference) — server map
                # flow → required action; "login" có thể trả token không hợp lệ cho
                # endpoint password/verify. legacy giữ "login".
                _sentinel_flow = "password_verify" if flow_mode == "anti409" else "login"
                # pure_request mode: no browser oracle → sync QuickJS.
                sentinel_pw = _get_sentinel_token(
                    session, device_id, _sentinel_flow, log,
                )
                if sentinel_pw:
                    headers["openai-sentinel-token"] = sentinel_pw

                resp = session.post(
                    "https://auth.openai.com/api/accounts/password/verify",
                    headers=headers,
                    json={"password": password},
                    timeout=30,
                )
                if resp.status_code != 200:
                    body = (resp.text or "")[:300]
                    raise SessionError(f"password verify failed: HTTP {resp.status_code} - {body}")
                log("[session-req] password verified")
                pw_data = resp.json() if resp is not None else {}
                page_type = ((pw_data.get("page") or {}).get("type") or "").strip()
                continue_url = (pw_data.get("continue_url") or "").strip()
                log(f"[session-req] post-password → page_type={page_type!r} continue_url={continue_url[:80]!r}")

            # ── Branch B: passwordless OTP login ──
            elif flow == "otp":
                if mail_provider is None:
                    raise SessionError(
                        "account uses passwordless OTP login but no mail_provider available. "
                        "Use an Outlook/mail combo so OTP can be polled, or use browser mode."
                    )
                log("[session-req] passwordless OTP login flow...")
                # Trigger OTP send
                otp_send_headers = _common_headers("https://auth.openai.com/email-verification")
                if device_id:
                    otp_send_headers["oai-device-id"] = device_id
                session.get(
                    "https://auth.openai.com/api/accounts/email-otp/send",
                    headers=otp_send_headers,
                    timeout=30,
                )
                log("[session-req] OTP sent, polling mail provider...")

                # Poll OTP via mail provider (sync bridge)
                otp_started = datetime.now(timezone.utc)
                otp_code = _run_async_poll(
                    mail_provider, email, otp_started, log,
                )
                if not otp_code:
                    raise SessionError("OTP polling returned empty code")

                otp_resp = _step_verify_otp(session, otp_code, device_id, log)
                page_type = ((otp_resp.get("page") or {}).get("type") or "").strip()
                continue_url = (otp_resp.get("continue_url") or "").strip()
                log(f"[session-req] post-OTP → page_type={page_type!r} continue_url={continue_url[:80]!r}")

            # Step 6: MFA (if needed)
            if "mfa" in page_type or "mfa" in (continue_url or ""):
                if not secret:
                    raise SessionError("account requires 2FA but no secret provided")
                log("[session-req] MFA challenge detected...")

                # Extract challenge ID from continue_url
                # e.g. https://auth.openai.com/mfa-challenge/6a2f85296d588191...
                import re as _re
                challenge_id = ""
                _m = _re.search(r"/mfa-challenge/([a-f0-9]+)", continue_url or "")
                if _m:
                    challenge_id = _m.group(1)

                if not challenge_id:
                    raise SessionError(f"MFA challenge ID not found in continue_url: {continue_url[:100]}")

                mfa_headers = _common_headers("https://auth.openai.com/mfa-challenge")
                mfa_headers["Content-Type"] = "application/json"
                if device_id:
                    mfa_headers["oai-device-id"] = device_id

                # Step 6a: Issue challenge
                log("[session-req] issuing MFA challenge...")
                resp = session.post(
                    "https://auth.openai.com/api/accounts/mfa/issue_challenge",
                    headers=mfa_headers,
                    json={"id": challenge_id, "type": "totp", "force_fresh_challenge": False},
                    timeout=30,
                )
                if resp.status_code != 200:
                    log(f"[session-req] issue_challenge returned {resp.status_code}: {(resp.text or '')[:200]}")
                    # Non-fatal: some accounts may not need issue_challenge

                # Step 6b: Verify TOTP
                code = _generate_totp(secret)
                log("[session-req] verifying TOTP...")
                resp = session.post(
                    "https://auth.openai.com/api/accounts/mfa/verify",
                    headers=mfa_headers,
                    json={"id": challenge_id, "type": "totp", "code": code},
                    timeout=30,
                )
                if resp.status_code != 200:
                    body = (resp.text or "")[:300]
                    raise SessionError(f"MFA verify failed: HTTP {resp.status_code} - {body}")

                mfa_data = resp.json() if resp is not None else {}
                log("[session-req] MFA verified!")
                continue_url = (mfa_data.get("continue_url") or "").strip()

            # Normalize continue_url
            if continue_url and continue_url.startswith("/"):
                continue_url = urljoin("https://auth.openai.com", continue_url)

            # ── Helpers cho callback consume + verify session cookie ──
            # `_consume_callback` của request_phase trả bool (cookie đã set chưa)
            # nhưng caller cũ ignore → khi cookie chưa set kịp do server chậm /
                # callback code đã expire, /api/auth/session sẽ trả response chỉ
            # chứa WARNING_BANNER (unauthenticated). Retry + verify rõ ràng.
            def _has_session_cookie() -> bool:
                """NextAuth session-token có thể bị split thành .0/.1 khi quá dài."""
                for name in (
                    "__Secure-next-auth.session-token",
                    "__Secure-next-auth.session-token.0",
                ):
                    if session.cookies.get(name):
                        return True
                return False

            def _consume_callback_verified(callback_url: str, *, max_attempts: int = 3, delay: float = 1.0) -> bool:
                """Consume callback + verify session-token cookie. Retry nếu chậm."""
                if not callback_url or "code=" not in callback_url:
                    return False
                for attempt in range(1, max_attempts + 1):
                    ok = _consume_callback(session, callback_url, log)
                    if _has_session_cookie():
                        log(f"[session-req] consume_callback verified (attempt {attempt}/{max_attempts}) consumed={ok}")
                        return True
                    if attempt < max_attempts:
                        log(
                            f"[session-req] consume_callback chưa set session cookie "
                            f"(attempt {attempt}/{max_attempts}) — retry sau {delay:g}s..."
                        )
                        time.sleep(delay)
                log(
                    f"[session-req] consume_callback FAIL — session cookie KHÔNG set "
                    f"sau {max_attempts} lần"
                )
                return False

            # Step 7-8: Follow redirects + consume callback
            # If continue_url points to auth.openai.com (not a callback), we need to
            # reauthorize to get the actual callback with code parameter.
            if continue_url and "auth.openai.com" in continue_url and "code=" not in continue_url:
                log("[session-req] continue_url is auth page, attempting reauthorize for callback...")
                try:
                    csrf2 = _step_csrf(session, log)
                    auth_url2 = _step_auth_url(session, csrf2, log)
                    # Follow authorize URL (should redirect to callback since we're now authenticated)
                    callback_url, _ = _step_follow_redirects(session, auth_url2, log)
                    if callback_url:
                        _consume_callback_verified(callback_url)
                    else:
                        log("[session-req] reauthorize: callback URL KHÔNG tìm thấy trong redirect chain")
                except Exception as e:
                    log(f"[session-req] reauthorize attempt failed: {e}")

            elif continue_url:
                _t_cb = time.monotonic()
                callback_url, final_url = _step_follow_redirects(session, continue_url, log)
                log(
                    f"[session-req] follow_redirects {time.monotonic() - _t_cb:.2f}s "
                    f"callback={'found' if callback_url else 'missing'}"
                )
                if not callback_url:
                    raise SessionError(
                        f"login completed but no callback URL found in redirect chain. "
                        f"final_url={final_url[:120]!r}"
                    )
                _t_cc = time.monotonic()
                if not _consume_callback_verified(callback_url):
                    raise SessionError(
                        "callback consumed nhưng session-token cookie KHÔNG được set. "
                        "Nguyên nhân khả dĩ: callback code đã expire (login tới callback "
                        "quá chậm), Cloudflare reject NextAuth set-cookie, hoặc proxy "
                        "strip cookie. Tăng số lần retry hoặc đổi proxy."
                    )
                log(f"[session-req] consume_callback {time.monotonic() - _t_cc:.2f}s")

            elif not continue_url:
                # Try reauthorize (session cookie may already be set)
                log("[session-req] no continue_url, attempting reauthorize...")
                try:
                    csrf2 = _step_csrf(session, log)
                    auth_url2 = _step_auth_url(session, csrf2, log)
                    resp = session.get(
                        auth_url2,
                        headers={
                            "Accept": "text/html,*/*",
                            "Accept-Language": "en-US,en;q=0.9",
                            "Referer": "https://chatgpt.com/",
                            "User-Agent": USER_AGENT,
                            "sec-ch-ua": _SEC_CH_UA,
                            "sec-ch-ua-mobile": _SEC_CH_UA_MOBILE,
                            "sec-ch-ua-platform": _SEC_CH_UA_PLATFORM,
                        },
                        timeout=30,
                        allow_redirects=False,
                    )
                    loc = (resp.headers.get("Location") or "").strip()
                    if loc:
                        redir_url = loc if loc.startswith("http") else urljoin(auth_url2, loc)
                        callback_url, _ = _step_follow_redirects(session, redir_url, log)
                        if callback_url:
                            _consume_callback_verified(callback_url)
                except Exception as e:
                    log(f"[session-req] reauthorize failed: {e}")

            # Pre-check: nếu session cookie vẫn chưa có ở đây, /api/auth/session
            # CHẮC CHẮN sẽ trả response WARNING_BANNER (unauthenticated). Fail fast
            # với message rõ ràng thay vì để user thấy raw banner JSON.
            if not _has_session_cookie():
                raise SessionError(
                    "login flow completed nhưng cookie __Secure-next-auth.session-token "
                    "không được set. /api/auth/session sẽ trả WARNING_BANNER "
                    "(unauthenticated). Nguyên nhân khả dĩ: callback code expire, "
                    "Cloudflare reject, redirect chain bị cắt, hoặc proxy strip cookie."
                )

            # Step 9: Get FULL /api/auth/session JSON (same as browser mode)
            _t_sess = time.monotonic()
            sess_headers = _common_headers("https://chatgpt.com/")
            sess_resp = session.get(
                "https://chatgpt.com/api/auth/session",
                headers=sess_headers,
                timeout=30,
            )
            log(f"[session-req] GET /api/auth/session {time.monotonic() - _t_sess:.2f}s")
            if sess_resp.status_code != 200:
                raise SessionError(
                    f"/api/auth/session failed: HTTP {sess_resp.status_code} - {(sess_resp.text or '')[:200]}"
                )
            try:
                session_data = sess_resp.json()
            except Exception as e:
                raise SessionError(f"/api/auth/session JSON parse failed: {e}")

            if not isinstance(session_data, dict) or not session_data.get("accessToken"):
                # Detect "warning-only" response: server trả banner cảnh báo
                # nhưng KHÔNG có session payload → user vẫn unauthenticated.
                keys = sorted(session_data.keys()) if isinstance(session_data, dict) else []
                only_warning = (
                    isinstance(session_data, dict)
                    and "WARNING_BANNER" in session_data
                    and not any(k in session_data for k in ("user", "accessToken", "expires"))
                )
                if only_warning:
                    raise SessionError(
                        "login chưa thực sự authenticated — /api/auth/session chỉ trả "
                        "WARNING_BANNER, không có user/accessToken/expires. "
                        "Session cookie đã set nhưng NextAuth không recognize "
                        "(có thể cookie sai domain/path, hoặc Cloudflare strip cookie "
                        "ở request /api/auth/session). "
                        f"keys={keys}"
                    )
                raise SessionError(
                    f"login completed but /api/auth/session has no accessToken: {str(session_data)[:200]}"
                )

            user_email = (session_data.get("user", {}) or {}).get("email", "") or email
            log(f"[session-req] ✓ done — user: {user_email}")
            # Capture cookies cho hybrid flow (caller có thể inject vào browser).
            # curl_cffi expose cookies qua jar (.cookies) — list các Cookie object.
            try:
                cookies_export: list[dict[str, Any]] = []
                for ck in session.cookies.jar:
                    cookies_export.append({
                        "name": ck.name,
                        "value": ck.value,
                        "domain": ck.domain,
                        "path": ck.path or "/",
                        "secure": bool(ck.secure),
                        # httpOnly không lộ qua API curl_cffi → mặc định True cho
                        # cookie auth (an toàn hơn).
                        "httpOnly": ck.name.startswith("__Host-") or ck.name.startswith("__Secure-"),
                        "sameSite": "Lax",
                        "expires": ck.expires if ck.expires else -1,
                    })
                session_data["__cookies"] = cookies_export
            except Exception as exc:
                log(f"[session-req] cookie export failed: {exc}")
            return session_data
        finally:
            try:
                session.close()
            except Exception:
                pass

    return await asyncio.to_thread(_sync)
