"""Mail Mode Registry — extension point cho mail provider dispatch.

Mỗi MailModeSpec khai báo:
- parse_line: parse 1 dòng input → ParsedLine
- build_request: build SignupRequest từ parsed + config
- config_schema: mô tả trường config (UI render + persist localStorage)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from mail_providers import (
    ChinaICloudParseError,
    ChinaICloudProvider,
    GmailAdvancedParseError,
    GmailAdvancedProvider,
    IcloudV3ParseError,
    IcloudV3Provider,
    OutlookCombo,
    OutlookComboError,
)
from models import SignupRequest


# ─── Errors ───────────────────────────────────────────────────────────


class MailModeParseError(Exception):
    """Parse line fail cho 1 mail mode."""


class GmailAdvancedModeParseError(MailModeParseError):
    """Parse line fail cho Gmail Advanced mode."""


# ─── Data types ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class ParsedLine:
    email: str
    raw: str


@dataclass(frozen=True)
class MailModeSpec:
    id: str
    label: str
    input_placeholder: str
    input_help: str
    config_schema: list[dict[str, Any]]
    parse_line: Callable[[str], ParsedLine]
    build_request: Callable[..., SignupRequest]


# ─── Outlook mode ─────────────────────────────────────────────────────


def _parse_outlook_line(line: str) -> ParsedLine:
    combo = OutlookCombo.parse(line)
    return ParsedLine(email=combo.email, raw=line)


def _build_outlook_request(
    parsed: ParsedLine,
    *,
    worker_config: dict[str, str] | None = None,
    password: str | None = None,
    headless: bool = True,
    keep_browser_open: bool = False,
    proxy: str | None = None,
    reg_mode: str = "browser",
) -> SignupRequest:
    from config import env_insecure_tls
    return SignupRequest(
        email=parsed.email,
        mail_provider="outlook",
        outlook_combo=parsed.raw,
        headless=headless,
        keep_browser_open=keep_browser_open,
        password=password,
        proxy=proxy,
        tls_insecure=env_insecure_tls(),
        reg_mode=reg_mode,
    )


OUTLOOK_MODE = MailModeSpec(
    id="outlook",
    label="Hotmail (combo)",
    input_placeholder="email|password|refresh_token|client_id",
    input_help="Mỗi dòng 1 combo Outlook 4 phần. Cascade: DongVanFB primary, Microsoft Graph fallback.",
    config_schema=[],
    parse_line=_parse_outlook_line,
    build_request=_build_outlook_request,
)


# ─── Worker mode (iCloud) ────────────────────────────────────────────


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _parse_worker_line(line: str) -> ParsedLine:
    email = line.strip()
    if not _EMAIL_RE.match(email):
        raise MailModeParseError(f"invalid icloud email: {line[:80]}")
    return ParsedLine(email=email, raw=line)


def _build_worker_request(
    parsed: ParsedLine,
    *,
    worker_config: dict[str, str] | None = None,
    password: str | None = None,
    headless: bool = True,
    keep_browser_open: bool = False,
    proxy: str | None = None,
    reg_mode: str = "browser",
) -> SignupRequest:
    cfg = worker_config or {}
    # insecure_tls chỉ bật qua opt-in: env GPT_SIGNUP_INSECURE_TLS=1 hoặc
    # worker_config["insecure_tls"]. Default = secure.
    from config import env_insecure_tls
    raw_flag = str(cfg.get("insecure_tls", "")).strip().lower()
    cfg_insecure = raw_flag in ("1", "true", "yes", "on")
    insecure = env_insecure_tls() or cfg_insecure
    return SignupRequest(
        email=parsed.email,
        mail_provider="worker",
        email_logs_url=cfg.get("logs_url", "https://icloud-cf-mail.n5pskgzs9g.workers.dev/logs"),
        email_api_key=cfg.get("api_key", ""),
        email_insecure_tls=insecure,
        # iCloud HME relay (Apple forward HME→inbox) trễ tới vài phút trước khi
        # mail vào inbox để worker IMAP IDLE bắt được. 300s cho đủ biên để vớt
        # mã của chính phiên hiện tại thay vì timeout sớm.
        otp_timeout_seconds=300.0,
        otp_poll_interval_seconds=5.0,
        # iCloud HME relay có thể trễ 100-200s. Resend sớm chỉ vô hiệu mã đang bay
        # rồi phải chờ mail mới (lại 100-200s) → đuổi vô tận + dễ bị rate-limit.
        # Chờ 120s mới cho phép resend; resend tối đa 1 lần (xử lý ở browser/request phase).
        otp_resend_after_seconds=120.0,
        headless=headless,
        keep_browser_open=keep_browser_open,
        password=password,
        proxy=proxy,
        tls_insecure=insecure,
        reg_mode=reg_mode,
    )


WORKER_MODE = MailModeSpec(
    id="worker",
    label="iCloud Mail (Worker API)",
    input_placeholder="user@icloud.com",
    input_help="Mỗi dòng 1 email iCloud nhận OTP qua Worker.",
    config_schema=[
        {
            "key": "logs_url",
            "label": "Worker API URL",
            "type": "text",
            "default": "https://icloud-cf-mail.n5pskgzs9g.workers.dev/logs",
            "required": True,
            "validate_prefix": ["http://", "https://"],
        },
        {
            "key": "api_key",
            "label": "VIEW_TOKEN",
            "type": "text",
            "default": "12345678@",
            "required": False,
        },
    ],
    parse_line=_parse_worker_line,
    build_request=_build_worker_request,
)


# ─── Gmail Advanced mode ──────────────────────────────────────────────


def _parse_gmail_advanced_line(line: str) -> ParsedLine:
    """Parse line `email|api_url` hoặc chỉ `api_url` cho Gmail Advanced."""
    try:
        email, api_url = GmailAdvancedProvider.parse_line(line)
    except GmailAdvancedParseError as exc:
        raise MailModeParseError(str(exc)) from exc
    # Nếu URL-only → email rỗng, dùng placeholder (sẽ resolve từ API pre_check)
    display_email = email if email else f"(pending) {api_url[:50]}..."
    return ParsedLine(email=display_email, raw=line)


def _build_gmail_advanced_request(
    parsed: ParsedLine,
    *,
    worker_config: dict[str, str] | None = None,
    password: str | None = None,
    headless: bool = True,
    keep_browser_open: bool = False,
    proxy: str | None = None,
    reg_mode: str = "browser",
) -> SignupRequest:
    raw = parsed.raw.strip()
    # Detect format: URL-only hoặc email|url
    if raw.startswith(("http://", "https://")):
        api_url = raw
        email = ""  # sẽ fill từ pre_check
    else:
        parts = raw.split("|", 1)
        email = parts[0].strip()
        api_url = parts[1].strip() if len(parts) == 2 else ""

    # Nếu email rỗng → dùng placeholder, pre_check sẽ resolve
    signup_email = email if email else "pending@gmail-advanced.local"
    from config import env_insecure_tls
    return SignupRequest(
        email=signup_email,
        mail_provider="gmail_advanced",
        gmail_api_url=api_url,
        otp_timeout_seconds=300.0,
        otp_poll_interval_seconds=5.0,
        headless=headless,
        keep_browser_open=keep_browser_open,
        password=password,
        proxy=proxy,
        tls_insecure=env_insecure_tls(),
        reg_mode=reg_mode,
    )


GMAIL_ADVANCED_MODE = MailModeSpec(
    id="gmail_advanced",
    label="Gmail Advanced (API)",
    input_placeholder="https://checkgmail.live/otp/2605201652376818498?t=...\nbrandonspencer7424@gmail.com|https://checkgmail.live/otp/...",
    input_help="Mỗi dòng: api_url hoặc email|api_url. Pre-check mail_status=live trước khi chạy.",
    config_schema=[],
    parse_line=_parse_gmail_advanced_line,
    build_request=_build_gmail_advanced_request,
)


# ─── China iCloud mode (icloudapi.xyz) ────────────────────────────────


def _parse_china_icloud_line(line: str) -> ParsedLine:
    """Parse line `email----url` cho China iCloud."""
    try:
        email, _api_url = ChinaICloudProvider.parse_line(line)
    except ChinaICloudParseError as exc:
        raise MailModeParseError(str(exc)) from exc
    return ParsedLine(email=email, raw=line)


def _build_china_icloud_request(
    parsed: ParsedLine,
    *,
    worker_config: dict[str, str] | None = None,
    password: str | None = None,
    headless: bool = True,
    keep_browser_open: bool = False,
    proxy: str | None = None,
    reg_mode: str = "browser",
) -> SignupRequest:
    email, api_url = ChinaICloudProvider.parse_line(parsed.raw)
    from config import env_insecure_tls
    return SignupRequest(
        email=email,
        mail_provider="china_icloud",
        china_icloud_url=api_url,
        # Mailbox HME relay có thể trễ vài phút — giữ cùng budget với Worker iCloud.
        otp_timeout_seconds=300.0,
        otp_poll_interval_seconds=5.0,
        otp_resend_after_seconds=120.0,
        headless=headless,
        keep_browser_open=keep_browser_open,
        password=password,
        proxy=proxy,
        tls_insecure=env_insecure_tls(),
        reg_mode=reg_mode,
    )


CHINA_ICLOUD_MODE = MailModeSpec(
    id="china_icloud",
    label="China iCloud (icloudapi.xyz)",
    input_placeholder=(
        "user+alias@icloud.com----http://icloudapi.xyz/show/<token>/<email_url_encoded>"
    ),
    input_help=(
        "Mỗi dòng 1 cặp `email----url` (separator 4 dấu gạch). "
        "URL là viewer mailbox riêng cho email đó."
    ),
    config_schema=[],
    parse_line=_parse_china_icloud_line,
    build_request=_build_china_icloud_request,
)


# ─── iCloud v3 mode (icloud-cf-mail-v2 Worker, URL per-mailbox) ───────


def _parse_icloud_v3_line(line: str) -> ParsedLine:
    """Parse `email|api_url` cho iCloud v3."""
    try:
        email, _api_url = IcloudV3Provider.parse_line(line)
    except IcloudV3ParseError as exc:
        raise MailModeParseError(str(exc)) from exc
    return ParsedLine(email=email, raw=line)


def _build_icloud_v3_request(
    parsed: ParsedLine,
    *,
    worker_config: dict[str, str] | None = None,
    password: str | None = None,
    headless: bool = True,
    keep_browser_open: bool = False,
    proxy: str | None = None,
    reg_mode: str = "browser",
) -> SignupRequest:
    email, api_url = IcloudV3Provider.parse_line(parsed.raw)
    from config import env_insecure_tls
    return SignupRequest(
        email=email,
        mail_provider="icloud_v3",
        icloud_v3_url=api_url,
        # HME relay (Apple forward → Worker v2) có thể trễ vài phút. Giữ cùng
        # budget với Worker v1 / China iCloud để tránh timeout sớm.
        otp_timeout_seconds=300.0,
        otp_poll_interval_seconds=5.0,
        # Resend sớm chỉ vô hiệu mã đang bay rồi phải chờ mail mới → đuổi vô tận.
        otp_resend_after_seconds=120.0,
        headless=headless,
        keep_browser_open=keep_browser_open,
        password=password,
        proxy=proxy,
        tls_insecure=env_insecure_tls(),
        reg_mode=reg_mode,
    )


ICLOUD_V3_MODE = MailModeSpec(
    id="icloud_v3",
    label="iCloud v3 (Worker v2, URL per-mailbox)",
    input_placeholder=(
        "petunia-boar-3d+hblx3n@icloud.com|"
        "https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/<token>/data"
    ),
    input_help=(
        "Mỗi dòng 1 cặp `email|url` (separator `|`). "
        "URL là endpoint mailbox riêng do Worker v2 cấp."
    ),
    config_schema=[],
    parse_line=_parse_icloud_v3_line,
    build_request=_build_icloud_v3_request,
)


# ─── DongVanFB Outlook mode (legacy, ẨN khỏi UI) ──────────────────────
#
# Trước đây DongVanFB là 1 mode riêng trên UI. Đã gộp vào "outlook" mode (cascade
# DongVanFB primary → Microsoft Graph fallback) để user không phải chọn 2 lần
# cho cùng 1 combo. Spec này chỉ giữ lại để:
#   - Job DB cũ có `mail_mode='dongvanfb'` vẫn parse + build được khi resume.
#   - Test direct DongVanFB (test/smoke_dongvanfb_direct.py) vẫn hoạt động.
# KHÔNG đăng ký vào `_REGISTRY` công khai → endpoint /api/mail-modes không
# trả về → UI không hiển thị nữa.


def _parse_dongvanfb_line(line: str) -> ParsedLine:
    combo = OutlookCombo.parse(line)
    return ParsedLine(email=combo.email, raw=line)


def _build_dongvanfb_request(
    parsed: ParsedLine,
    *,
    worker_config: dict[str, str] | None = None,
    password: str | None = None,
    headless: bool = True,
    keep_browser_open: bool = False,
    proxy: str | None = None,
    reg_mode: str = "browser",
) -> SignupRequest:
    from config import env_insecure_tls
    return SignupRequest(
        email=parsed.email,
        mail_provider="dongvanfb",
        outlook_combo=parsed.raw,
        headless=headless,
        keep_browser_open=keep_browser_open,
        password=password,
        proxy=proxy,
        tls_insecure=env_insecure_tls(),
        reg_mode=reg_mode,
    )


DONGVANFB_MODE = MailModeSpec(
    id="dongvanfb",
    label="Hotmail (DongVanFB API, legacy)",
    input_placeholder="email|password|refresh_token|client_id",
    input_help="Legacy mode — chỉ resolve job DB cũ. UI dùng 'outlook' (cascade).",
    config_schema=[],
    parse_line=_parse_dongvanfb_line,
    build_request=_build_dongvanfb_request,
)


# ─── Registry ─────────────────────────────────────────────────────────

# Public registry — show trên UI qua endpoint /api/mail-modes.
# Thứ tự dict = thứ tự render trên UI dropdown. icloud_v3 đặt đầu vì là default.
# DongVanFB không xuất hiện ở đây vì đã gộp vào 'outlook' (cascade).
_REGISTRY: dict[str, MailModeSpec] = {
    ICLOUD_V3_MODE.id: ICLOUD_V3_MODE,
    OUTLOOK_MODE.id: OUTLOOK_MODE,
    WORKER_MODE.id: WORKER_MODE,
    GMAIL_ADVANCED_MODE.id: GMAIL_ADVANCED_MODE,
    CHINA_ICLOUD_MODE.id: CHINA_ICLOUD_MODE,
}

# Lookup registry — dùng cho `get_spec(mail_mode)` ở backend khi resume job DB
# cũ có mail_mode='dongvanfb' hoặc test direct DongVanFB.
_LOOKUP_REGISTRY: dict[str, MailModeSpec] = {
    **_REGISTRY,
    DONGVANFB_MODE.id: DONGVANFB_MODE,
}


def get_registry() -> dict[str, MailModeSpec]:
    """Public registry (UI). Không bao gồm legacy modes."""
    return _REGISTRY


def get_spec(mail_mode: str) -> MailModeSpec:
    """Lookup spec theo id (kể cả legacy). Raise KeyError nếu không tồn tại."""
    return _LOOKUP_REGISTRY[mail_mode]


def serialize_for_api() -> list[dict[str, Any]]:
    """Trả list dict cho endpoint GET /api/mail-modes — chỉ public modes."""
    return [
        {
            "id": spec.id,
            "label": spec.label,
            "input_placeholder": spec.input_placeholder,
            "input_help": spec.input_help,
            "config_schema": spec.config_schema,
        }
        for spec in _REGISTRY.values()
    ]
