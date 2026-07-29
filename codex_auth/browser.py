"""Browser automation: lái auth.openai.com qua login → 2FA → consent → bắt callback code.

Dùng Camoufox (Firefox stealth) vì auth.openai.com có Cloudflare JS challenge.
Chặn redirect về http://localhost:1455/auth/callback bằng page.route (không dựng server).
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

import pyotp

from .errors import ConsentError, LoginError, PhoneVerificationRequired
from .pkce import PkceCodes
from .oauth import REDIRECT_URI, build_authorize_url

LogFn = Callable[[str], None]

# Regex match mọi request về callback localhost (port 1455 hoặc fallback 1457).
_CALLBACK_RE = re.compile(r"^http://localhost:14(?:55|57)/auth/callback")

_CALLBACK_HTML = (
    "<html><body style='font-family:sans-serif;text-align:center;padding:40px'>"
    "<h2>✓ Codex auth captured</h2><p>Bạn có thể đóng tab này.</p></body></html>"
)

# Selectors
_EMAIL_SELECTORS = (
    'input[name="email"]',
    'input[type="email"]',
    'input[inputmode="email"]',
    'input[id="email-input"]',
)
_PASSWORD_SELECTORS = ('input[type="password"]', 'input[name="password"]')
_OTP_SELECTORS = (
    'input[autocomplete="one-time-code"]',
    'input[name="code"]',
    'input[inputmode="numeric"]',
    'input[maxlength="6"]',
    'input[maxlength="7"]',
)
_SUBMIT_SELECTORS = (
    'button[type="submit"]',
    'button:has-text("Continue")',
    'button:has-text("Log in")',
    'button:has-text("Next")',
)
_CONSENT_SELECTORS = (
    'button:has-text("Authorize")',
    'button:has-text("Allow")',
    'button:has-text("Continue")',
    'button:has-text("Allow access")',
    'button[type="submit"]',
    'button:has-text("Tiếp tục")',
)


@dataclass
class CallbackResult:
    code: str
    state: str


def _parse_callback(url: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Trả (code, state, error) từ callback URL."""
    q = parse_qs(urlparse(url).query)
    return (
        (q.get("code") or [None])[0],
        (q.get("state") or [None])[0],
        (q.get("error") or [None])[0],
    )


async def _first_visible(page: Any, selectors: tuple[str, ...], timeout_ms: int) -> Any:
    """Trả locator đầu tiên visible trong danh sách selectors, hoặc None."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=timeout_ms):
                return loc
        except Exception:
            continue
    return None


async def _click_first(page: Any, selectors: tuple[str, ...], timeout_ms: int = 3000) -> bool:
    for sel in selectors:
        try:
            await page.click(sel, timeout=timeout_ms)
            return True
        except Exception:
            continue
    return False


async def _detect_screen(page: Any) -> str:
    """Phân loại màn hình hiện tại. Trả 1 trong:
    'callback' | 'add_phone' | 'otp' | 'password' | 'email' | 'consent' | 'unknown'.
    """
    url = page.url.lower()

    if _CALLBACK_RE.match(page.url):
        return "callback"

    # Gate verify phone — server-side, không bypass.
    if "add-phone" in url or "phone-verification" in url or "/phone" in url:
        return "add_phone"
    try:
        body_text = (await page.inner_text("body", timeout=1500)).lower()
    except Exception:
        body_text = ""
    if any(
        kw in body_text
        for kw in ("verify your phone", "add a phone", "phone number to continue", "enter your phone")
    ):
        return "add_phone"

    # Input-based screens (ưu tiên cao — chính xác hơn text).
    if await _first_visible(page, _OTP_SELECTORS, 600):
        return "otp"
    if await _first_visible(page, _PASSWORD_SELECTORS, 600):
        return "password"
    if await _first_visible(page, _EMAIL_SELECTORS, 600):
        return "email"

    # Consent page: ở oauth/authorize/consent mà không có input → cần click authorize.
    if any(k in url for k in ("oauth", "authorize", "consent", "/codex")):
        return "consent"

    return "unknown"


async def drive_oauth(
    page: Any,
    *,
    pkce: PkceCodes,
    state: str,
    email: str,
    password: str,
    secret: Optional[str],
    redirect_uri: str = REDIRECT_URI,
    overall_timeout: float = 150.0,
    interactive: bool = False,
    log: LogFn = print,
) -> CallbackResult:
    """Lái browser qua toàn bộ OAuth flow tới khi bắt được callback code.

    interactive=False (mặc định): tự động hoàn toàn. Gặp gate verify SĐT →
        raise PhoneVerificationRequired (KHÔNG bypass).
    interactive=True (human-in-the-loop, headed): tự điền email/password/2FA;
        nếu gặp màn cần người thật (device challenge, phone OTP hợp lệ, captcha)
        thì CHỜ user tự thao tác, code chỉ bắt callback khi user hoàn tất hợp lệ.
        Không lách gì — người dùng tự verify.

    Raises LoginError / ConsentError / PhoneVerificationRequired.
    """
    captured: dict[str, Optional[str]] = {"url": None}
    callback_event = asyncio.Event()

    async def _route_handler(route: Any) -> None:
        req_url = route.request.url
        if _CALLBACK_RE.match(req_url):
            captured["url"] = req_url
            callback_event.set()
            try:
                await route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=_CALLBACK_HTML,
                )
            except Exception:
                try:
                    await route.abort()
                except Exception:
                    pass
        else:
            await route.continue_()

    await page.route(_CALLBACK_RE, _route_handler)

    authorize_url = build_authorize_url(pkce, state, redirect_uri=redirect_uri)
    log(f"[codex] mở authorize URL: {authorize_url.split('?')[0]}?...")
    await page.goto(authorize_url, wait_until="domcontentloaded")
    await asyncio.sleep(2.0)

    deadline = time.monotonic() + overall_timeout
    done_screens = {"email": 0, "password": 0, "otp": 0, "consent": 0}
    last_screen = ""

    while time.monotonic() < deadline:
        if callback_event.is_set():
            break

        screen = await _detect_screen(page)
        if screen != last_screen:
            log(f"[codex] màn hình: {screen} :: {page.url.split('?')[0]}")
            last_screen = screen

        if screen == "callback":
            break

        if screen == "add_phone":
            if interactive:
                # Human-in-the-loop: để user tự verify SĐT hợp lệ. KHÔNG bypass.
                if last_screen != "add_phone_wait":
                    log("[codex] ⏳ gặp màn verify SĐT — vui lòng tự nhập số + OTP "
                        "để qua bước này (code sẽ tự bắt token khi xong)...")
                    last_screen = "add_phone_wait"
                await asyncio.sleep(2.0)
                continue
            raise PhoneVerificationRequired(
                "Account bị chặn ở gate verify số điện thoại (add-phone). "
                "Codex OAuth yêu cầu account đã verify phone / có workspace. "
                f"URL: {page.url}"
            )

        if screen == "email":
            done_screens["email"] += 1
            if done_screens["email"] > 3 and not interactive:
                raise LoginError(f"kẹt ở email step (loop). URL: {page.url}")
            loc = await _first_visible(page, _EMAIL_SELECTORS, 4000)
            if loc:
                log("[codex] nhập email...")
                await loc.click(force=True, timeout=3000)
                await loc.fill("")
                await loc.type(email, delay=25)
                await asyncio.sleep(0.3)
                await _click_first(page, _SUBMIT_SELECTORS)
                await asyncio.sleep(2.5)
            continue

        if screen == "password":
            done_screens["password"] += 1
            if done_screens["password"] > 3 and not interactive:
                raise LoginError(f"kẹt ở password step (loop / sai password?). URL: {page.url}")
            loc = await _first_visible(page, _PASSWORD_SELECTORS, 4000)
            if loc:
                log("[codex] nhập password...")
                await loc.click(force=True, timeout=3000)
                await loc.fill("")
                await loc.type(password, delay=35)
                await asyncio.sleep(0.3)
                await _click_first(page, _SUBMIT_SELECTORS)
                await asyncio.sleep(3.0)
            elif interactive:
                await asyncio.sleep(2.0)
            continue

        if screen == "otp":
            if not secret:
                if interactive:
                    if last_screen != "otp_wait":
                        log("[codex] ⏳ màn 2FA nhưng không có secret — chờ user tự nhập code...")
                        last_screen = "otp_wait"
                    await asyncio.sleep(2.0)
                    continue
                raise LoginError("account yêu cầu 2FA nhưng không có secret (TOTP).")
            done_screens["otp"] += 1
            if done_screens["otp"] > 3 and not interactive:
                raise LoginError(f"kẹt ở 2FA step (sai secret / code reject). URL: {page.url}")
            loc = await _first_visible(page, _OTP_SELECTORS, 4000)
            if loc:
                code = pyotp.TOTP(secret).now()
                log("[codex] nhập TOTP 2FA...")
                await loc.click(force=True, timeout=3000)
                await loc.fill("")
                await loc.type(code, delay=50)
                await asyncio.sleep(0.4)
                await _click_first(page, _SUBMIT_SELECTORS + ('button:has-text("Verify")',))
                await asyncio.sleep(3.0)
            continue

        if screen == "consent":
            done_screens["consent"] += 1
            if done_screens["consent"] > 4 and not interactive:
                raise ConsentError(f"kẹt ở consent step (không bấm được authorize). URL: {page.url}")
            log("[codex] consent → click authorize...")
            clicked = await _click_first(page, _CONSENT_SELECTORS)
            if not clicked:
                log("[codex] consent: chưa thấy nút authorize, đợi...")
            await asyncio.sleep(2.5)
            continue

        # unknown — đợi navigation tự diễn ra
        await asyncio.sleep(1.5)

    if not callback_event.is_set() and not captured["url"]:
        raise ConsentError(
            f"timeout {overall_timeout}s — không bắt được callback. URL cuối: {page.url}"
        )

    code, cb_state, error = _parse_callback(captured["url"] or "")
    if error:
        raise ConsentError(f"callback trả error={error}. URL: {captured['url']}")
    if cb_state != state:
        raise ConsentError(f"state mismatch: gửi={state[:12]}... nhận={cb_state}")
    if not code:
        raise ConsentError(f"callback thiếu code. URL: {captured['url']}")

    log("[codex] ✓ bắt được authorization code")
    return CallbackResult(code=code, state=cb_state)
