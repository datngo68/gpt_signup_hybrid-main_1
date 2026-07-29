"""Pydantic models cho signup hybrid."""
from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field

from user_agent_profile import (
    CURL_IMPERSONATE_PRIMARY as _CURL_IMPERSONATE_PRIMARY,
    WINDOWS_USER_AGENT as _WINDOWS_USER_AGENT,
)


class SignupRequest(BaseModel):
    """Input cho 1 lần signup."""

    email: str = Field(..., description="Email đăng ký, phải nhận được OTP qua Worker logs API.")
    name: str = Field(default="ChatGPT User", description="Tên hiển thị (POST create_account).")
    birthdate: str = Field(default="2000-01-01", description="YYYY-MM-DD, tuổi >= 13.")
    password: str | None = Field(
        default=None,
        description="Password để register account. Nếu None, runner gen random 12 ký tự.",
    )

    # Registration mode:
    #   - "browser" : full Camoufox/Playwright UI navigation (anti-detect, heavy).
    #   - "hybrid"  : pure-HTTP curl_cffi impersonate Firefox + Camoufox chỉ mint
    #                 sentinel sdk.js tokens (recommended — nhanh + session đầy đủ).
    reg_mode: str = Field(
        default="browser",
        description=(
            "Registration mode: 'browser' (anti-detect Camoufox UI) hoặc "
            "'hybrid' (HTTP Firefox + Camoufox oracle, recommended)."
        ),
        pattern="^(browser|pure_request|hybrid)$",
    )
    source_email: str | None = Field(
        default=None,
        description="Mailbox poll OTP. Nếu None thì dùng `email`. Dùng khi smail khác email form.",
    )

    # Browser
    headless: bool = Field(default=False, description="Camoufox headless (không khuyến nghị, dễ bị flag).")
    keep_browser_open: bool = Field(
        default=False,
        description="Giữ browser mở sau khi xong (debug). Chỉ có tác dụng khi headed.",
    )
    off_font: bool = Field(default=False, description="Tắt camoufox font randomization.")
    profile_template: bool = Field(
        default=False,
        description=(
            "Clone profile template (cookies, addons). Mặc định FALSE từ "
            "anti-ban hardening 2026-06-25 (journal 260625-1224 bug B3). "
            "Cookies cũ tái dùng giữa account → CF/Sentinel cluster ban. "
            "Opt-in chỉ cho debug/research."
        ),
    )
    tls_insecure: bool = Field(
        default=False,
        description=(
            "Bỏ TLS cert verification cho browser context (chỉ dùng debug/MITM proxy). "
            "Production phải để False — bật qua env GPT_SIGNUP_INSECURE_TLS=1 hoặc CLI flag."
        ),
    )

    # Polling OTP — chọn 1 trong các provider:
    #   - iCloud v3 (icloud-cf-mail-v2 Worker, URL gắn cứng per-mailbox) — DEFAULT.
    #   - Worker logs API (icloud-cf-mail v1 style) — fallback cho mailbox cũ.
    #   - Outlook combo (Microsoft Graph) — cho mail @hotmail.com / @outlook.com.
    #   - Gmail Advanced (checkgmail.live API) — cho mail @gmail.com mua qua dịch vụ.
    #   - China iCloud (icloudapi.xyz) — viewer URL riêng cho mỗi alias HME.
    mail_provider: str = Field(
        default="icloud_v3",
        description=(
            "Provider: 'icloud_v3' (default), 'worker', 'outlook', 'dongvanfb', "
            "'gmail_advanced', hoặc 'china_icloud'."
        ),
        pattern="^(icloud_v3|worker|outlook|dongvanfb|gmail_advanced|china_icloud)$",
    )
    # Gmail Advanced config
    gmail_api_url: str | None = Field(
        default=None,
        description="API URL checkgmail.live (dùng khi mail_provider='gmail_advanced').",
    )
    # China iCloud config
    china_icloud_url: str | None = Field(
        default=None,
        description=(
            "Viewer URL mailbox icloudapi.xyz "
            "(dùng khi mail_provider='china_icloud'). Format: http(s)://.../show/<token>/<email>."
        ),
    )
    # iCloud v3 config (icloud-cf-mail-v2 Worker)
    icloud_v3_url: str | None = Field(
        default=None,
        description=(
            "Worker v2 mailbox URL (dùng khi mail_provider='icloud_v3'). "
            "Format: http(s)://.../readmail/<token>/data."
        ),
    )
    # Worker config
    email_logs_url: str = Field(
        default="https://icloud-cf-mail.n5pskgzs9g.workers.dev/logs",
        description="Worker URL trả JSON array messages cho ?mail=<recipient>.",
    )
    email_api_key: str = Field(
        default="12345678@",
        description="Bearer token cho Authorization header. Để rỗng nếu Worker không yêu cầu.",
    )
    email_insecure_tls: bool = Field(
        default=False,
        description=(
            "Bỏ verify TLS khi poll OTP từ Worker (chỉ dùng debug/local dev). "
            "Production phải để False — bật chỉ qua flag/env opt-in."
        ),
    )
    # Outlook combo config
    outlook_combo: str | None = Field(
        default=None,
        description="Combo `email|password|refresh_token|client_id` (Microsoft Graph).",
    )
    # Polling chung
    otp_timeout_seconds: float = Field(default=180.0, ge=10, description="Thời gian tối đa đợi OTP về.")
    otp_poll_interval_seconds: float = Field(default=4.0, ge=0.5)
    otp_resend_after_seconds: float = Field(
        default=90.0,
        ge=15,
        description=(
            "Đợi mail OTP bao lâu (giây) trước khi click Resend. "
            "Mail provider thực tế (iCloud HME, Outlook) có thể delay 1-2 phút — "
            "set quá thấp sẽ gây spam Resend, dễ bị OpenAI rate-limit."
        ),
    )

    # Form readiness wait
    sentinel_cookie_timeout_seconds: float = Field(
        default=30.0, ge=5,
        description="Thời gian đợi OTP form ready trên /email-verification.",
    )
    har_capture: bool = Field(
        default=False,
        description="Bật HAR capture cho Phase 1 (debug). Output: runtime/har_hybrid/<ts>.har",
    )

    # Hybrid Phase 2
    user_agent: str = Field(
        default=_WINDOWS_USER_AGENT,
        description="UA ép cho curl_cffi (phải khớp browser fingerprint Phase 1 — Windows Chrome).",
    )
    impersonate: str = Field(
        default=_CURL_IMPERSONATE_PRIMARY,
        description="curl_cffi browser impersonation key (đồng bộ với UA Chrome major).",
    )
    proxy: str | None = Field(default=None, description="HTTP/HTTPS proxy cho cả 2 phase.")

    # Locale + persona (anti-ban — journal 260625-1224 Task 1.4 + 1.6)
    # ──────────────────────────────────────────────────────────────────
    # Locale BCP-47 dùng cho:
    #   - Browser context locale (en-IN/Asia/Kolkata khi proxy India)
    #   - Random profile name pool (en-IN → tên Ấn, en-US → tên Anglo)
    # None = auto-detect theo proxy country (Task 1.4 implementation).
    locale: str | None = Field(
        default=None,
        description=(
            "Locale BCP-47 (vd 'en-IN', 'en-US'). None = auto-detect theo "
            "proxy country (cần `reg.locale_auto_geo=true`)."
        ),
    )
    timezone: str | None = Field(
        default=None,
        description="Timezone IANA (vd 'Asia/Kolkata'). None = auto theo proxy.",
    )

    # Browser persona (anti-ban Phase 7 Task 7.3)
    persona: str = Field(
        default="firefox_mac",
        description=(
            "Browser persona name. 'firefox_mac' (default — Camoufox/Firefox 135 Mac) "
            "hoặc 'chrome_win' (Chrome 145 Windows — chỉ dùng khi engine=chromium). "
            "Phải khớp với BrowserPersona registry trong user_agent_profile."
        ),
    )

    # MFA inline — enroll 2FA NGAY trong context vừa tạo account (browser page
    # hoặc curl session còn sống), tái dùng CF clearance + đúng IP. Tránh spawn
    # curl_cffi session mới sau khi context chết → bị Cloudflare 403 → mất acc.
    mfa_inline: bool = Field(
        default=True,
        description="Enroll 2FA inline trong phase signup (CF-clean). Tắt → fallback enable_2fa Phase 2 (curl_cffi).",
    )


class BrowserHandoff(BaseModel):
    """Output Phase 1 — context để Phase 2 dùng."""

    cookies: list[dict[str, Any]] = Field(default_factory=list, description="Playwright cookies dict list.")
    state_param: str = Field(..., description="OAuth state lấy từ URL /authorize?...&state=<...>.")
    device_id: str = Field(..., description="ext-oai-did UUID (cũng là id field cho /sentinel/req).")
    auth_session_logging_id: str = Field(..., description="Logging ID từ /api/auth/signin/openai redirect URL.")
    callback_redirect_uri: str = Field(
        default="https://chatgpt.com/api/auth/callback/openai",
        description="redirect_uri của OAuth (giống nhau cho mọi run, copy từ HAR).",
    )
    callback_url: str = Field(
        ...,
        description="Full callback URL (kèm code + state) trả về từ create_account, dùng cho Phase 2.",
    )

    # Kết quả enroll 2FA inline (browser page còn sống — CF-clean). None nếu
    # mfa_inline tắt hoặc enroll fail (caller fallback enable_2fa Phase 2).
    two_factor: dict[str, Any] | None = Field(
        default=None,
        description="MFA result inline {secret, factor_id, session_id, first_code, activated, ...}.",
    )
    two_factor_partial: dict[str, Any] | None = Field(
        default=None,
        description="State {secret, factor_id, session_id} khi enroll OK nhưng activate fail — để retry activate-only.",
    )

    # Cookies Phase 2 cần dùng (helpers)
    @property
    def cookies_dict_for(self) -> dict[str, dict[str, str]]:
        """Map domain → {name: value} cho dễ inject vào curl_cffi."""
        out: dict[str, dict[str, str]] = {}
        for c in self.cookies:
            domain = (c.get("domain") or "").lstrip(".")
            out.setdefault(domain, {})[c["name"]] = c["value"]
        return out


class SignupResult(BaseModel):
    """Output cuối: session token NextAuth + metadata."""

    success: bool
    email: str
    password: str | None = Field(default=None, description="Password đã set khi register.")
    name: str | None = Field(default=None, description="Tên hiển thị đã dùng.")
    age: int | None = Field(default=None, description="Tuổi đã dùng (compute từ birthdate).")
    user_id: str | None = None
    account_id: str | None = None
    session_token: str | None = Field(default=None, description="__Secure-next-auth.session-token JWT.")
    access_token: str | None = Field(default=None, description="Bearer JWT cho /backend-api/.")
    cookies: list[dict[str, Any]] = Field(default_factory=list, description="Cookies sau callback (chatgpt.com).")
    phase1_seconds: float = 0.0
    phase2_seconds: float = 0.0
    otp_seconds: float = 0.0
    error: str | None = None

    # MFA enroll inline (CF-clean) — set bởi phase signup khi mfa_inline=True.
    # two_factor: enroll + activate đầy đủ. two_factor_partial: enroll OK nhưng
    # activate fail → caller persist + retry activate-only (không enroll lại).
    two_factor: dict[str, Any] | None = Field(default=None, description="MFA result inline (đầy đủ).")
    two_factor_partial: dict[str, Any] | None = Field(default=None, description="MFA partial state (activate fail).")
