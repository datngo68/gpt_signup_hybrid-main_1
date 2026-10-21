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
from .sms import SmsProvider, clean_phone_vn

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
_PHONE_SELECTORS = (
    'input[type="tel"]',
    'input[name="phone"]',
    'input[name="phoneNumber"]',
    'input[placeholder*="Phone"]',
    'input[placeholder*="Số điện thoại"]',
    'input[autocomplete="tel"]',
)
_COUNTRY_SELECTORS = (
    'button[aria-haspopup="listbox"]',
    'button[role="combobox"]',
    'div[role="combobox"]',
    'button:has-text("+")',
    'button[class*="country"]',
)
_SEND_CODE_SELECTORS = (
    'button:has-text("Send code via SMS")',
    'button:has-text("Send code")',
    'button:has-text("Send via SMS")',
    'button:has-text("Gửi mã")',
    'button[type="submit"]',
)
_WHATSAPP_DISMISS_SELECTORS = (
    'button:has-text("No, send code via SMS")',
    'button:has-text("Send via SMS instead")',
    'button:has-text("Send code via SMS")',
    'a:has-text("Send via SMS")',
    'button:has-text("Gửi qua tin nhắn")',
)
_PHONE_ERROR_KEYWORDS = (
    "already linked to the maximum number",
    "đã được liên kết với số lượng tài khoản tối đa",
    "unsupported phone number",
    "không hỗ trợ số điện thoại",
    "invalid phone number",
    "số điện thoại không hợp lệ",
    "rate limit reached",
    "too many requests",
    "vui lòng thử lại sau",
    "we could not verify",
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


async def handle_phone_verification(page: Any, sms_provider: SmsProvider, *, max_attempts: int = 1, log: LogFn = print) -> bool:
    pending: set[str] = set()

    class TrackedSms:
        def request_phone(self):
            request_id, phone = sms_provider.request_phone()
            pending.add(request_id)
            return request_id, phone

        def cancel(self, request_id):
            result = sms_provider.cancel(request_id)
            if result:
                pending.discard(request_id)
            return result

        def __getattr__(self, name):
            return getattr(sms_provider, name)

    try:
        return await _handle_phone_verification(page, TrackedSms(), max_attempts=max_attempts, log=log)
    finally:
        for request_id in tuple(pending):
            try:
                sms_provider.cancel(request_id)
            except Exception:
                pass


async def _handle_phone_verification(
    page: Any,
    sms_provider: SmsProvider,
    *,
    max_attempts: int = 1,
    log: LogFn = print,
) -> bool:
    """Tự động thuê số và xác minh SĐT tại gate add-phone."""
    for attempt in range(1, max_attempts + 1):
        log(f"[codex] [phone] Bắt đầu verify số điện thoại (lần {attempt}/{max_attempts})...")
        try:
            req_id, raw_phone = sms_provider.request_phone()
        except Exception as exc:
            log(f"[codex] [phone] Thuê số thất bại từ provider: {exc}")
            if attempt == max_attempts:
                raise
            await asyncio.sleep(3.0)
            continue

        dial_code, local_number = clean_phone_vn(raw_phone)
        log("[codex] [phone] Đã thuê số thành công.")

        # 1. Chọn mã quốc gia +84 nếu cần
        try:
            country_btn = await _first_visible(page, _COUNTRY_SELECTORS, 2000)
            if country_btn:
                btn_text = (await country_btn.inner_text()).strip()
                if "+84" not in btn_text and "Vietnam" not in btn_text:
                    await country_btn.click()
                    await asyncio.sleep(0.5)
                    vietnam_opt = page.locator('div[role="option"]:has-text("Vietnam"), li:has-text("Vietnam"), button:has-text("+84")').first
                    if await vietnam_opt.is_visible(timeout=2000):
                        await vietnam_opt.click()
                        await asyncio.sleep(0.5)
        except Exception as exc:
            log(f"[codex] [phone] Lưu ý khi chọn quốc gia: {exc}")

        # 2. Nhập số điện thoại
        phone_input = await _first_visible(page, _PHONE_SELECTORS, 4000)
        if not phone_input:
            log(f"[codex] [phone] Không tìm thấy ô nhập SĐT. URL: {page.url}")
            sms_provider.cancel(req_id)
            return False

        await phone_input.click(force=True, timeout=3000)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await asyncio.sleep(0.2)
        await phone_input.type(local_number, delay=35)
        await asyncio.sleep(0.5)

        # 3. Click gửi code
        clicked_send = await _click_first(page, _SEND_CODE_SELECTORS, 3000)
        if not clicked_send:
            log("[codex] [phone] Không bấm được nút Send code, submit form...")
            await phone_input.press("Enter")
        await asyncio.sleep(2.0)

        # Prompt WhatsApp: chọn SMS
        try:
            wa_btn = await _first_visible(page, _WHATSAPP_DISMISS_SELECTORS, 2000)
            if wa_btn:
                log("[codex] [phone] Bỏ qua WhatsApp → Chọn gửi qua SMS...")
                await wa_btn.click()
                await asyncio.sleep(1.5)
        except Exception:
            pass

        # 4. Kiểm tra lỗi số
        await asyncio.sleep(2.0)
        try:
            body_txt = (await page.inner_text("body", timeout=2000)).lower()
        except Exception:
            body_txt = ""

        has_error = False
        for kw in _PHONE_ERROR_KEYWORDS:
            if kw in body_txt:
                log(f"[codex] [phone] OpenAI từ chối số thuê ('{kw}'); dừng sau một rental.")
                sms_provider.cancel(req_id)
                has_error = True
                break

        if has_error:
            raise PhoneVerificationRequired(
                "OpenAI từ chối số thuê; dừng sau một rental để tránh phát sinh chi phí khác."
            )

        # 5. Kiểm tra màn hình OTP
        otp_input = await _first_visible(page, _OTP_SELECTORS, 6000)
        if not otp_input:
            await asyncio.sleep(3.0)
            otp_input = await _first_visible(page, _OTP_SELECTORS, 4000)

        if not otp_input:
            if "add-phone" not in page.url.lower():
                log("[codex] [phone] Trang đã chuyển hướng tiếp!")
                return False  # No OTP acceptance observed; do not claim verified.
            sms_provider.cancel(req_id)
            continue

        # 6. Chờ nhận OTP
        log("[codex] [phone] Đang đợi OTP từ SMS provider...")
        try:
            otp_code = await sms_provider.get_otp(req_id, timeout=120.0, poll_interval=3.0, log=log)
        except Exception as exc:
            log(f"[codex] [phone] Không nhận được OTP: {exc}")
            sms_provider.cancel(req_id)
            raise PhoneVerificationRequired(
                "Không nhận được OTP; dừng để tránh thuê số mới khi vẫn ở màn nhập mã."
            ) from exc

        if not otp_code or not str(otp_code).isdigit() or len(str(otp_code)) != 6:
            sms_provider.cancel(req_id)
            raise PhoneVerificationRequired("SMS provider trả OTP không hợp lệ")
        log("[codex] [phone] Đã nhận OTP. Điền vào form...")
        await otp_input.click(force=True, timeout=3000)
        await otp_input.fill("")
        await otp_input.type(otp_code, delay=50)
        await asyncio.sleep(0.5)

        await _click_first(page, _SUBMIT_SELECTORS + ('button:has-text("Verify")', 'button:has-text("Tiếp tục")'), 3000)
        await asyncio.sleep(3.0)

        if "add-phone" not in page.url.lower() and "phone" not in page.url.lower():
            log("[codex] [phone] ✓ Xác minh số điện thoại thành công!")
            return True

        await asyncio.sleep(3.0)
        if "add-phone" not in page.url.lower() and "phone" not in page.url.lower():
            log("[codex] [phone] ✓ Đã qua màn hình verify phone!")
            return True

        sms_provider.cancel(req_id)
        raise PhoneVerificationRequired("OTP bị từ chối hoặc màn xác minh chưa hoàn tất; không thuê thêm số.")

    raise PhoneVerificationRequired(f"Đã thử verify phone {max_attempts} lần thất bại.")


async def drive_oauth(
    page: Any,
    *,
    pkce: PkceCodes,
    state: str,
    email: str,
    password: str,
    secret: Optional[str],
    sms_provider: Optional[SmsProvider] = None,
    redirect_uri: str = REDIRECT_URI,
    overall_timeout: float = 150.0,
    interactive: bool = False,
    phone_state: Optional[Callable[[str], None]] = None,
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
            if phone_state:
                phone_state("required")
            if sms_provider is not None:
                log("[codex] 📱 phát hiện gate verify SĐT → bắt đầu tự động ver số qua SMS provider...")
                try:
                    verified = await handle_phone_verification(page, sms_provider=sms_provider, log=log)
                    if verified and phone_state:
                        phone_state("verified")
                    # Sau khi xong, sleep rồi tiếp tục loop để detect screen tiếp theo
                    await asyncio.sleep(2.0)
                    continue
                except Exception as exc:
                    log(f"[codex] ⚠️ tự động ver số lỗi: {exc}")
                    if not interactive:
                        raise

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
