"""Pure HTTP API runner for Codex OAuth authentication (No browser required).

Sử dụng:
- curl_cffi: giả lập TLS fingerprint của Chrome/Firefox để bypass basic bot detection.
- QuickJS (Node subprocess): giải Sentinel PoW challenge cho các endpoint auth của OpenAI.
- SMS Provider: thuê số và lấy OTP trực tiếp qua HTTP API.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.parse
from typing import Any, Callable, Optional

from curl_cffi import requests as curl_requests
import pyotp

from .errors import CodexAuthError, ConsentError, LoginError, PhoneVerificationRequired
from .oauth import (
    REDIRECT_URI,
    build_auth_dot_json,
    build_authorize_url,
    exchange_code_for_tokens,
    obtain_api_key,
)
from .pkce import generate_pkce, generate_state
from .sms import SmsProvider, clean_phone_vn
from sentinel_quickjs import get_sentinel_token_via_quickjs
from user_agent_profile import CHROME_145_WIN, WINDOWS_USER_AGENT

logger = logging.getLogger(__name__)
LogFn = Callable[[str], None]

_CALLBACK_RE = re.compile(r"^http://localhost:14(?:55|57)/auth/callback")


class CloudflareChallengeRequired(CodexAuthError):
    """Bị chặn bởi Cloudflare JS challenge / Turnstile tương tác, cần fallback sang browser."""


def _normalize_proxy(proxy: Optional[str]) -> Optional[str]:
    if not proxy:
        return None
    p = proxy.strip()
    if p.startswith("socks5://"):
        return "socks5h://" + p[len("socks5://"):]
    return p


def _common_headers(referer: str = "https://auth.openai.com/") -> dict[str, str]:
    headers = CHROME_145_WIN.common_headers(referer=referer)
    headers.update({
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://auth.openai.com",
        "Referer": referer,
        "User-Agent": WINDOWS_USER_AGENT,
    })
    return headers


def _extract_callback_params(url: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    code = (qs.get("code") or [None])[0]
    state = (qs.get("state") or [None])[0]
    error = (qs.get("error") or [None])[0]
    return code, state, error


async def get_codex_auth_api(
    *,
    email: str,
    password: str,
    secret: Optional[str] = None,
    sms_provider: Optional[SmsProvider] = None,
    proxy: Optional[str] = None,
    fetch_api_key: bool = True,
    overall_timeout: float = 120.0,
    phone_state: Optional[Callable[[str], None]] = None,
    log: LogFn = print,
) -> dict[str, Any]:
    """Thực hiện luồng lấy Codex auth.json hoàn toàn bằng HTTP API (không mở trình duyệt).

    Raises:
        CloudflareChallengeRequired nếu gặp Turnstile/Cloudflare cần browser.
        LoginError / PhoneVerificationRequired / ConsentError.
    """
    pkce = generate_pkce()
    state = generate_state()
    auth_url = build_authorize_url(pkce, state, redirect_uri=REDIRECT_URI)

    norm_proxy = _normalize_proxy(proxy)
    proxies = {"https": norm_proxy, "http": norm_proxy} if norm_proxy else {}

    session = curl_requests.Session(impersonate="chrome120")
    session.trust_env = False
    if proxies:
        session.proxies = proxies

    deadline = time.monotonic() + overall_timeout
    original_request = session.request

    def _remaining() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Codex API overall_timeout đã hết.")
        return remaining

    def _request(*args: Any, **kwargs: Any) -> Any:
        kwargs["timeout"] = min(kwargs.get("timeout", 30), _remaining())
        response = original_request(*args, **kwargs)
        _remaining()
        return response

    session.request = _request

    pending_rentals: set[str] = set()

    def _sync_flow_inner() -> dict[str, Any]:
        _remaining()
        log(f"[codex-api] Bắt đầu luồng pure API (proxy={'yes' if proxy else 'no'})...")

        # 1. Truy cập authorize URL
        log("[codex-api] [1/5] Khởi tạo OAuth authorize URL...")
        resp = session.get(auth_url, allow_redirects=True, timeout=30)
        if resp.status_code == 403 or "cf-mitigated" in (resp.text or "").lower() or "just a moment" in (resp.text or "").lower():
            raise CloudflareChallengeRequired("Cloudflare challenge phát hiện ở authorize landing.")

        device_id = session.cookies.get("oai-did") or str(__import__("uuid").uuid4())

        # 2. authorize/continue (Email)
        log("[codex-api] [2/5] Giải Sentinel token & gửi Email...")
        sentinel_login = get_sentinel_token_via_quickjs(
            session, device_id, flow="login", log=log, persona=CHROME_145_WIN
        )

        headers = _common_headers("https://auth.openai.com/log-in")
        if device_id:
            headers["oai-device-id"] = device_id
        if sentinel_login:
            headers["openai-sentinel-token"] = sentinel_login

        payload_email = {
            "username": {"value": email, "kind": "email"},
            "screen_hint": "login",
        }

        resp_email = session.post(
            "https://auth.openai.com/api/accounts/authorize/continue",
            headers=headers,
            json=payload_email,
            timeout=30,
        )

        if resp_email.status_code == 403 and "just a moment" in (resp_email.text or "").lower():
            raise CloudflareChallengeRequired("Cloudflare challenge phát hiện ở authorize/continue.")
        if resp_email.status_code != 200:
            raise LoginError(f"Gửi email thất bại: HTTP {resp_email.status_code} - {(resp_email.text or '')[:200]}")

        # 3. password/verify
        log("[codex-api] [3/5] Gửi Password...")
        sentinel_pw = get_sentinel_token_via_quickjs(
            session, device_id, flow="password_verify", log=log, persona=CHROME_145_WIN
        )

        headers_pw = _common_headers("https://auth.openai.com/log-in/password")
        if device_id:
            headers_pw["oai-device-id"] = device_id
        if sentinel_pw:
            headers_pw["openai-sentinel-token"] = sentinel_pw

        resp_pw = session.post(
            "https://auth.openai.com/api/accounts/password/verify",
            headers=headers_pw,
            json={"password": password},
            timeout=30,
        )

        if resp_pw.status_code != 200:
            raise LoginError(f"Xác thực mật khẩu thất bại: HTTP {resp_pw.status_code} - {(resp_pw.text or '')[:200]}")

        pw_data = resp_pw.json() if resp_pw.text else {}
        page_info = pw_data.get("page", {}) if isinstance(pw_data, dict) else {}
        page_type = str(page_info.get("type") or "").strip()
        continue_url = str(pw_data.get("continue_url") or "").strip()

        # 4. MFA verify nếu có
        if "mfa" in page_type or "mfa" in continue_url:
            if not secret:
                raise LoginError("Tài khoản yêu cầu 2FA (MFA) nhưng không có secret.")
            log("[codex-api] Phát hiện thử thách MFA 2FA, giải mã TOTP...")
            mfa_headers = _common_headers("https://auth.openai.com/mfa-challenge")
            if device_id:
                mfa_headers["oai-device-id"] = device_id

            challenge_id = ""
            m_chal = re.search(r"/mfa-challenge/([a-f0-9]+)", continue_url)
            if m_chal:
                challenge_id = m_chal.group(1)

            totp_code = pyotp.TOTP(secret).now()
            resp_mfa = session.post(
                "https://auth.openai.com/api/accounts/mfa/verify",
                headers=mfa_headers,
                json={"id": challenge_id, "type": "totp", "code": totp_code},
                timeout=30,
            )
            if resp_mfa.status_code != 200:
                raise LoginError(f"MFA verify thất bại: HTTP {resp_mfa.status_code} - {(resp_mfa.text or '')[:200]}")

            mfa_data = resp_mfa.json() if resp_mfa.text else {}
            continue_url = str(mfa_data.get("continue_url") or "").strip()

        # 5. Kiểm tra gate Add Phone (Phone verification)
        is_phone_gate = (
            page_type in ("phone_verification", "add_phone")
            or "add-phone" in continue_url
            or "phone-verification" in continue_url
        )

        if is_phone_gate:
            if phone_state:
                phone_state("required")
            if sms_provider is None:
                raise PhoneVerificationRequired(
                    "Tài khoản bị chặn ở gate verify số điện thoại (add-phone) và chưa cấu hình SMS Provider."
                )

            log("[codex-api] [4/5] 📱 Bắt đầu tự động ver số qua SMS API...")
            phone_verified = False
            for attempt in range(1, 2):
                _remaining()
                req_id, raw_phone = sms_provider.request_phone()
                pending_rentals.add(req_id)
                dial_code, local_number = clean_phone_vn(raw_phone)
                full_phone = f"{dial_code}{local_number}"
                log("[codex-api] Đã thuê số thành công (lần thử 1/1).")

                sentinel_phone = get_sentinel_token_via_quickjs(
                    session, device_id, flow="phone_verification", log=log, persona=CHROME_145_WIN
                )

                phone_headers = _common_headers("https://auth.openai.com/add-phone")
                if device_id:
                    phone_headers["oai-device-id"] = device_id
                if sentinel_phone:
                    phone_headers["openai-sentinel-token"] = sentinel_phone

                # Thử gửi code tới các endpoint thông dụng của OpenAI
                send_payload = {
                    "phone_number": full_phone,
                    "verification_method": "sms",
                }

                send_resp = None
                for ep in (
                    "https://auth.openai.com/api/accounts/phone/send-code",
                    "https://auth.openai.com/api/v1/auth/phone/send-code",
                ):
                    try:
                        send_resp = session.post(ep, headers=phone_headers, json=send_payload, timeout=25)
                        if send_resp.status_code in (200, 201):
                            break
                    except Exception:
                        pass

                if not send_resp or send_resp.status_code not in (200, 201):
                    status = send_resp.status_code if send_resp else "no-response"
                    log(f"[codex-api] Gửi code thất bại (HTTP {status}); dừng sau một rental.")
                    sms_provider.cancel(req_id)
                    raise PhoneVerificationRequired(
                        "OpenAI từ chối gửi mã; dừng sau một rental để tránh phát sinh chi phí khác."
                    )

                log("[codex-api] ✓ Đã gửi code. Đang chờ OTP từ SMS Provider...")
                # Lấy OTP
                otp_code = None
                try:
                    # Bridge async get_otp into sync thread
                    loop = asyncio.new_event_loop()
                    try:
                        otp_code = loop.run_until_complete(
                            sms_provider.get_otp(req_id, timeout=min(120.0, _remaining()), poll_interval=3.0, log=log)
                        )
                    finally:
                        loop.close()
                except Exception as exc:
                    log(f"[codex-api] Chờ OTP thất bại: {exc}")
                    raise PhoneVerificationRequired("Không nhận được OTP; dừng để tránh thuê số mới.") from None

                if not otp_code or not str(otp_code).isdigit() or len(str(otp_code)) != 6:
                    raise PhoneVerificationRequired("SMS provider trả OTP không hợp lệ")
                log("[codex-api] Đã nhận OTP; đang xác thực...")
                verify_payload = {"code": otp_code}
                verify_resp = None
                for v_ep in (
                    "https://auth.openai.com/api/accounts/phone/verify",
                    "https://auth.openai.com/api/v1/auth/phone/verify",
                ):
                    try:
                        verify_resp = session.post(v_ep, headers=phone_headers, json=verify_payload, timeout=25)
                        if verify_resp.status_code in (200, 201):
                            break
                    except Exception:
                        pass

                if verify_resp and verify_resp.status_code in (200, 201):
                    log(f"[codex-api] ✓ Xác thực số điện thoại thành công!")
                    phone_data = verify_resp.json() if verify_resp.text else {}
                    continue_url = str(phone_data.get("continue_url") or "").strip()
                    phone_verified = True
                    if phone_state:
                        phone_state("verified")
                    pending_rentals.discard(req_id)
                    break
                else:
                    v_err = (verify_resp.text or "")[:200] if verify_resp else "No response"
                    raise PhoneVerificationRequired("OTP bị từ chối hoặc request xác minh thất bại; không thuê thêm số.")

            if not phone_verified:
                raise PhoneVerificationRequired("Ver số qua API thất bại sau một rental.")

        # 6. Bắt callback redirect về http://localhost:1455/auth/callback
        log("[codex-api] [5/5] Follow redirect để bắt callback code...")
        next_url = continue_url or auth_url
        if next_url.startswith("/"):
            next_url = urllib.parse.urljoin("https://auth.openai.com", next_url)

        cb_code = None
        cb_state = None

        # Loop follow redirects (không tự redirect sang localhost để bắt location)
        current_target = next_url
        for _ in range(10):
            if _CALLBACK_RE.match(current_target):
                cb_code, cb_state, _ = _extract_callback_params(current_target)
                break

            r = session.get(current_target, allow_redirects=False, timeout=30)
            loc = r.headers.get("Location") or ""
            if not loc:
                # Nếu là 200 HTML consent page:
                if "consent" in r.url or "authorize" in r.url or "oauth" in r.url:
                    log("[codex-api] Gặp trang consent OAuth, gửi POST authorize consent...")
                    consent_headers = _common_headers(r.url)
                    resp_consent = session.post(
                        "https://auth.openai.com/api/accounts/oauth/authorize/consent",
                        headers=consent_headers,
                        json={"action": "allow"},
                        allow_redirects=False,
                        timeout=30,
                    )
                    loc = resp_consent.headers.get("Location") or ""

            if loc:
                if _CALLBACK_RE.match(loc):
                    cb_code, cb_state, _ = _extract_callback_params(loc)
                    break
                if loc.startswith("/"):
                    current_target = urllib.parse.urljoin("https://auth.openai.com", loc)
                else:
                    current_target = loc
            else:
                break

        if not cb_code:
            raise ConsentError(f"Không bắt được callback code từ API. Target URL cuối: {current_target}")

        if cb_state != state:
            raise ConsentError(f"State mismatch: gửi={state[:12]}... nhận={cb_state}")

        log(f"[codex-api] ✓ Bắt được authorization code!")
        log("[codex-api] Đổi code → access_token + refresh_token...")
        _remaining()
        tokens = exchange_code_for_tokens(cb_code, pkce, proxy=proxy)

        api_key: Optional[str] = None
        if fetch_api_key:
            api_key = obtain_api_key(tokens["id_token"], proxy=proxy)
            log(f"[codex-api] API key exchange: {'OK' if api_key else 'skip/none'}")

        _remaining()
        auth_json = build_auth_dot_json(tokens, api_key=api_key)
        log(f"[codex-api] ✓ auth.json hoàn tất — account_id={auth_json['tokens']['account_id']}")
        return auth_json

    def _sync_flow() -> dict[str, Any]:
        try:
            return _sync_flow_inner()
        finally:
            for request_id in pending_rentals:
                try:
                    sms_provider.cancel(request_id)
                except Exception:
                    pass

    try:
        return await asyncio.to_thread(_sync_flow)
    finally:
        try:
            session.close()
        except Exception:
            pass
