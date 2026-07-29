"""UPI QR runner — reusable async function lấy QR cho 1 ChatGPT Plus IN account.

Tách từ ``test/probe_upi_qr.py`` để ``UpiJobManager`` (web UI) gọi cho từng
account. Logic giống probe nhưng:
    - Không in stdout / không tạo artifact JSON.
    - Trả dict result + log qua callback.
    - Hardcoded các knob theo yêu cầu UI:
        promo=True, proxy_from_step=3, do_confirm=True, do_approve=True,
        approve_delay=3.0, approve_proxy_batch=10,
        approve_backend_exception_consecutive=0  (DISABLED — 0 nghĩa là không
            bao giờ fatal-break vì backend_exception flaky; loop chỉ dừng
            khi approved hoặc hết approve_retries),
        confirm_variants=("qr_code", "empty", "flow_qr", "intent")
    - Configurable: approve_retries, restart_threshold, max_restarts
        (caller truyền vào — đọc từ Settings Store).

Public:
    run_upi_qr_probe(...)           — entry point per-job
    UpiQrResult                     — dataclass kết quả
    UpiQrError                      — fatal error
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from time import monotonic
from typing import Any, Awaitable, Callable

from user_agent_profile import (
    CURL_IMPERSONATE_PRIMARY as _IMPERSONATE,
)

# [FIX-CHROME142-FIRST-2026-06-20] — local override chain, ưu tiên chrome142
# Lý do: CF Bot Management soi TLS hello của chrome145 (mới hơn) chặt hơn
# chrome142 → approve loop dính 403 hit đầu mỗi proxy mới. Đặt 142 đầu chain
# để _RotatingSession khởi tạo BoringSSL context với fingerprint 142, fallback
# 145/136 khi TLS error.
#
# Scope: CHỈ trong upi_runner. KHÔNG đổi `CURL_IMPERSONATE_PRIMARY` toàn cục
# vì sẽ ảnh hưởng signup/MFA/session_phase (đã verify chrome145 OK ở các flow
# đó — đổi global = rủi ro side-effect rộng).
#
# REVERT: xoá block này + restore import:
#     from user_agent_profile import (
#         CURL_IMPERSONATE_PRIMARY as _IMPERSONATE,
#         CURL_IMPERSONATE_CANDIDATES as _IMPERSONATE_CHAIN,
#     )
_IMPERSONATE_CHAIN: tuple[str, ...] = ("chrome142", "chrome145", "chrome136")

from .proxy_format import materialize_proxy
from .proxy_format import sanitize_proxy_text as _sanitize_proxy  # canonical (DRY, F-E)


# ─── TLS error detection + rotating session ──────────────────────────
# curl-impersonate (BoringSSL fork) thỉnh thoảng raise OPENSSL_internal:invalid
# library sau khi 1 request lỗi network/proxy reset — internal SSL state bị
# corrupt, mọi request kế trên cùng session đều fail. Fix: rotate impersonate
# (đồng nghĩa fresh BoringSSL context) khi gặp pattern lỗi này.
#
# Pattern đồng bộ với request_phase._is_tls_error (chrome145 → chrome142 →
# chrome136 fallback chain từ user_agent_profile).

_TLS_ERROR_MARKERS: tuple[str, ...] = (
    "curl: (35)", "tls connect error", "openssl_internal", "sslerror",
    "curl: (56)", "curl: (7)", "ssl_error", "handshake",
    "invalid library",
)


def _is_tls_error(exc: BaseException) -> bool:
    """Detect curl_cffi TLS handshake / BoringSSL state errors → worth
    rotating impersonate. Match pattern theo request_phase + thêm
    'invalid library' (BoringSSL state corrupt sau lỗi proxy)."""
    msg = str(exc).lower()
    return any(m in msg for m in _TLS_ERROR_MARKERS)


# Số lần tối đa recreate inner session (clear corrupt BoringSSL context) cho 1
# request trước khi propagate exception. Bounded để proxy chết THẬT không loop
# vô tận — approve loop sẽ tự advance proxy khi request raise. Recovery này chỉ
# nhắm transient TLS state corruption, không phải proxy down.
_TLS_RECREATE_MAX_PER_CALL: int = 4
_TLS_RECREATE_BACKOFF: tuple[float, ...] = (0.5, 1.0, 2.0, 3.0)


def _safe_materialize(line: str | None) -> str | None:
    """materialize_proxy nhưng nuốt ValueError (format rác) → None (direct).

    DEPRECATED behavior — silent fallback đã gây "chập chờn ăn proxy" khi
    line[0] có whitespace/format lỗi: trả None → flow chạy DIRECT mà user
    không biết. Caller giờ nên dùng `_materialize_or_log_warning(line, log)`
    để có log chẩn đoán rõ ràng. Helper cũ giữ lại cho backward-compat
    (ngoài UPI runner).
    """
    if not line:
        return None
    try:
        return materialize_proxy(line)
    except ValueError:
        return None


def _materialize_or_log_warning(line: str | None, log: "LogFn") -> str | None:
    """Materialize 1 raw proxy line → URL concrete; KHÔNG silent direct.

    Behavior:
      - line rỗng/None/whitespace-only → return None (caller hiểu = direct).
        KHÔNG log (tránh spam "empty proxy line" với pool có entry đệm trắng).
      - line format lỗi → log WARNING với chi tiết format error + return None
        (KHÔNG raise — caller `run_upi_qr_probe` quyết định fail-fast hay không
        ở 1 chỗ duy nhất, để diagnostic + error path nhất quán).
      - line OK → return URL concrete (đã materialize {SID} nếu có).

    Lý do tách khỏi `_safe_materialize`: silent fallback là dạng "fallback
    che lỗi" vi phạm fail-fast principle. Function này log rõ để debug
    "tại sao flow chạy direct dù pool có entry".
    """
    # Coerce + strip để check empty một cách nhất quán với materialize_proxy
    # (nó cũng strip trước parse). Whitespace-only = empty → silent return None.
    stripped = (line or "").strip()
    if not stripped:
        return None
    try:
        return materialize_proxy(stripped)
    except ValueError as exc:
        # Mask để không leak credential trong log error.
        masked = _mask_proxy(stripped)
        log(_fmt_step(
            "proxy", "format-error", "warn",
            f"line {masked!r} parse fail: {exc} → SILENT DIRECT (FIX line trong "
            "Settings tab > Proxy Pool, format đúng host:port[:user[:pass]])",
        ))
        return None

# Hardcoded knobs — fix cứng theo spec UI (không expose ra Settings).
PROMO: bool = True
PROXY_FROM_STEP: int = 3
DO_CONFIRM: bool = True
DO_APPROVE: bool = True
APPROVE_DELAY: float = 5.0
APPROVE_PROXY_BATCH: int = 10
# Số lần `result=exception http=200` LIÊN TIẾP để fatal-break approve loop.
# Default = 0 → DISABLED: backend_exception KHÔNG bao giờ làm dừng sớm; loop
# chỉ dừng khi `approved=True` hoặc hết `approve_retries` user cấu hình.
# Lý do: Stripe approve có thể trả exception flaky cả trăm lần liên tiếp rồi
# tự khỏi — checkout đã pass (có cs_live_...), không có lý do gì hủy session
# vì server-side hiccup. Logic advance proxy ở dưới vẫn hoạt động độc lập với
# threshold này, nên vẫn skip qua proxy đang bị Stripe throttle bình thường.
# Đặt > 0 chỉ khi cần kill switch chống loop vô tận trong môi trường test.
APPROVE_BACKEND_EXCEPTION_CONSECUTIVE: int = 0
# Default per-call cho restart loop. Caller (UpiJobManager) sẽ override từ
# Settings Store nên không nên dựa vào module constants này ở runtime.
#   restart_threshold = số `result=exception` LIÊN TIẾP trước khi restart
#       checkout session. 0 = disabled (giữ behavior cũ, không restart).
#   max_restarts      = số lần restart tối đa trong 1 job. Hết quota mà vẫn
#       dính exception → fatal break.
# Mục tiêu: khi Stripe approve stuck `exception` cả trăm cái liên tục, tạo
# checkout session mới (giữ login + giữ retry budget cộng dồn) để "wash"
# state phía Stripe thay vì đốt retry budget vô ích.
APPROVE_RESTART_THRESHOLD: int = 30
APPROVE_MAX_RESTARTS: int = 3
# Network outage detection — tránh đốt retry budget khi mất mạng/DNS down.
# Khi N timeout/connection-error LIÊN TIẾP → pause approve loop, poll
# connectivity tới `chatgpt.com` mỗi POLL_SECONDS, max wait MAX_WAIT_SECONDS.
# Recovery → reset network counter, tiếp tục. Hết max wait → fatal break.
# Trong lúc pause, KHÔNG tăng `approve_index_total` (không đốt budget).
NETWORK_FAIL_DETECT: int = 3
NETWORK_RECOVERY_POLL_SECONDS: float = 5.0
NETWORK_RECOVERY_MAX_WAIT_SECONDS: float = 600.0
NETWORK_PROBE_TIMEOUT_SECONDS: float = 5.0
CONFIRM_VARIANTS: tuple[str, ...] = ("qr_code", "empty", "flow_qr", "intent")

LogFn = Callable[[str], None]


# ─────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────


@dataclass
class UpiQrResult:
    """Kết quả 1 lần probe — đủ để render UI (có QR file + summary)."""

    ok: bool
    email: str
    amount: int = 0
    return_url: str = ""
    checkout_session: str = ""
    qr_path: str | None = None       # absolute path tới PNG (None nếu render fail)
    qr_source: str | None = None     # "stripe_image" | "upi_uri" | "hosted_html"
    qr_source_url: str | None = None
    qr_reason: str | None = None     # nếu không có QR
    qr_expires_at: int | None = None  # unix seconds — QR hết hạn lúc này (None nếu không rõ)
    payment_link: str | None = None  # hosted_instructions_url (payments.stripe.com/upi/instructions/...)
    has_upi_uri: bool = False
    has_qr_image_url: bool = False
    confirm_attempts: list[dict[str, Any]] = field(default_factory=list)
    approve_attempts: list[dict[str, Any]] = field(default_factory=list)
    page_refresh_attempts: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    backend_exception_count: int = 0
    restart_count: int = 0
    elapsed_seconds: float = 0.0
    # True khi cycle bị break sớm do block-streak (N approve attempt IP/edge-reject
    # liên tiếp) → outer loop (UpiJobManager) re-login + checkout/IP mới. Chỉ set
    # khi relogin_block_streak>0. KHÔNG vào to_dict (tín hiệu nội bộ outer-loop).
    relogin_requested: bool = field(default=False, repr=False)
    # Auth artifacts để re-check session sau khi QR hết hạn (in-memory, KHÔNG
    # đưa vào to_dict() — caller giữ riêng, không leak ra JSON SSE/snapshot).
    access_token: str | None = field(default=None, repr=False)
    session_cookies: list[dict[str, Any]] | None = field(default=None, repr=False)
    # Concrete proxy URL dùng cho login Step1 (= IP token-export replay, F-A).
    # repr=False + KHÔNG vào to_dict() → không leak ra SSE/UI snapshot.
    proxy_used: str | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "email": self.email,
            "amount": self.amount,
            "return_url": self.return_url,
            "checkout_session": self.checkout_session,
            "qr_path": self.qr_path,
            "qr_source": self.qr_source,
            "qr_source_url": self.qr_source_url,
            "qr_reason": self.qr_reason,
            "qr_expires_at": self.qr_expires_at,
            "payment_link": self.payment_link,
            "has_upi_uri": self.has_upi_uri,
            "has_qr_image_url": self.has_qr_image_url,
            "confirm_attempts": self.confirm_attempts,
            "approve_attempts": self.approve_attempts,
            "page_refresh_attempts": self.page_refresh_attempts,
            "error": self.error,
            "backend_exception_count": self.backend_exception_count,
            "restart_count": self.restart_count,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
        }


class UpiQrError(Exception):
    """Fatal error trong flow probe (login fail, no free offer, approve threshold...)."""


# ─────────────────────────────────────────────────────────────────────
# Constants & helpers (giữ nguyên semantics từ probe)
# ─────────────────────────────────────────────────────────────────────

_MATCH_TERMS = (
    "qr",
    "upi",
    "intent",
    "collect",
    "vpa",
    "next_action",
    "hosted_instructions",
    "image_url",
    "display_qr",
)
_SENSITIVE_PATH_TERMS = (
    "access",
    "authorization",
    "client_secret",
    "cookie",
    "key",
    "password",
    "secret",
    "token",
)


def _mask_email(email: str) -> str:
    local, sep, domain = email.partition("@")
    if not sep:
        return "***"
    if len(local) <= 3:
        return f"{local[:1]}***@{domain}"
    return f"{local[:3]}***{local[-2:]}@{domain}"


def _mask_proxy(proxy: str | None) -> str:
    """Delegate canonical proxy_format.mask_proxy (DRY — hết circular)."""
    from .proxy_format import mask_proxy
    return mask_proxy(proxy)


def _proxy_dict(proxy: str | None) -> dict[str, str] | None:
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _proxy_for_step(proxy: str | None, *, from_step: int, step: int) -> dict[str, str] | None:
    if proxy and step >= from_step:
        return _proxy_dict(proxy)
    return None


def _proxy_url_for_retry(
    proxies: list[str],
    *,
    from_step: int,
    step: int,
    attempt: int,
    per_proxy_attempts: int,
) -> str | None:
    if step < from_step or not proxies:
        return None
    proxy_index = ((attempt - 1) // per_proxy_attempts) % len(proxies)
    return proxies[proxy_index]


def _is_sensitive_path(path: str) -> bool:
    lower = path.lower()
    return any(term in lower for term in _SENSITIVE_PATH_TERMS)


def _short_value(value: Any, path: str) -> Any:
    if _is_sensitive_path(path):
        return "[redacted]"
    if not isinstance(value, str):
        return value
    if len(value) <= 500:
        return value
    return f"{value[:260]}...{value[-120:]}"


def _find_matches(value: Any, *, source: str, path: str = "$") -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = f"{path}.{key}"
            key_lower = str(key).lower()
            if any(term in key_lower for term in _MATCH_TERMS):
                matches.append({
                    "source": source,
                    "path": child_path,
                    "kind": "key",
                    "value": _short_value(item, child_path),
                })
            matches.extend(_find_matches(item, source=source, path=child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            matches.extend(_find_matches(item, source=source, path=f"{path}[{index}]"))
    elif isinstance(value, str):
        value_lower = value.lower()
        if any(term in value_lower for term in _MATCH_TERMS):
            matches.append({
                "source": source,
                "path": path,
                "kind": "value",
                "value": _short_value(value, path),
            })
    return matches


def _find_upi_uri(matches: list[dict[str, Any]]) -> str | None:
    for match in matches:
        value = match.get("value")
        if isinstance(value, str) and value.lower().startswith("upi://"):
            return value
    return None


def _find_qr_image_url(matches: list[dict[str, Any]]) -> str | None:
    for match in matches:
        value = match.get("value")
        path = str(match.get("path") or "").lower()
        if (
            isinstance(value, str)
            and value.startswith("https://")
            and "qr" in path
            and (value.endswith(".png") or value.endswith(".svg") or "qr" in value.lower())
        ):
            return value
    return None


def _find_qr_expires_at(matches: list[dict[str, Any]]) -> int | None:
    """Tìm `expires_at` (unix seconds) của QR trong next_action.

    Stripe trả object ``qr_code: {expires_at, image_url_png, image_url_svg}``
    trong ``next_action.upi_handle_redirect_or_display_qr_code``. ``_find_matches``
    bắt key ``qr_code`` (match term "qr") với value là cả dict → đọc trực tiếp.
    """
    for match in matches:
        value = match.get("value")
        if not isinstance(value, dict):
            continue
        expires_at = value.get("expires_at")
        if (
            isinstance(expires_at, int)
            and not isinstance(expires_at, bool)
            and expires_at > 0
            and ("image_url_png" in value or "image_url_svg" in value)
        ):
            return expires_at
    return None


def _find_hosted_instructions_url(matches: list[dict[str, Any]]) -> str | None:
    """Tìm `hosted_instructions_url` của UPI (payments.stripe.com/upi/instructions/...).

    Stripe trả nó trong ``next_action.upi_handle_redirect_or_display_qr_code.
    hosted_instructions_url`` — là trang hướng dẫn thanh toán UPI hosted (mở ra
    hiện QR + nút mở app UPI). Đây là "payment link" đúng dạng user cần.

    ``_find_matches`` bắt được nó 2 cách:
      1. Key ``hosted_instructions_url`` (match term "hosted_instructions") →
         value = URL string trực tiếp.
      2. Key ``next_action`` (match term "next_action") → value = cả dict, đào
         nested ``upi_handle_redirect_or_display_qr_code.hosted_instructions_url``.
    Ưu tiên (1); fallback (2) để chắc ăn nếu shape đổi.
    """
    _PREFIX = "https://payments.stripe.com/upi/instructions/"
    # (1) direct string value.
    for match in matches:
        value = match.get("value")
        if isinstance(value, str) and value.startswith(_PREFIX):
            return value
    # (2) nested trong dict value (vd next_action / qr block).
    for match in matches:
        value = match.get("value")
        if not isinstance(value, dict):
            continue
        block = value.get("upi_handle_redirect_or_display_qr_code")
        if isinstance(block, dict):
            url = block.get("hosted_instructions_url")
            if isinstance(url, str) and url.startswith(_PREFIX):
                return url
        url = value.get("hosted_instructions_url")
        if isinstance(url, str) and url.startswith(_PREFIX):
            return url
    return None


class _PayloadMetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.payload_message: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta":
            return
        values = {key.lower(): value for key, value in attrs if value is not None}
        if values.get("id") == "payload":
            self.payload_message = values.get("data-message")


def _extract_hosted_instruction_upi_uri(html_text: str) -> str | None:
    parser = _PayloadMetaParser()
    parser.feed(html_text)
    message = parser.payload_message
    if not message:
        return None
    padded = message + ("=" * (-len(message) % 4))
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except Exception:
        return None
    uri = payload.get("mobile_auth_url") if isinstance(payload, dict) else None
    return uri if isinstance(uri, str) and uri.startswith("upi:") else None


def _redact_error(error: Any) -> Any:
    if not isinstance(error, dict):
        return str(error)[:500]
    allowed = {}
    for key in ("type", "code", "decline_code", "message", "param", "payment_intent"):
        if key in error:
            allowed[key] = _short_value(error.get(key), f"error.{key}")
    return allowed


def _upi_payload_for_variant(variant: str) -> dict[str, Any]:
    if variant == "flow_qr":
        return {"flow": "qr_code"}
    if variant == "qr_code":
        return {"qr_code": {}}
    if variant == "intent":
        return {"intent": "qr_code"}
    return {}


def _stripe_return_url(session_id: str) -> str:
    return f"https://checkout.stripe.com/c/pay/{session_id}"


def _extract_amount(init_data: dict[str, Any]) -> int:
    elements_options = init_data.get("elements_options")
    if isinstance(elements_options, dict) and isinstance(elements_options.get("amount"), int):
        return elements_options["amount"]
    total_summary = init_data.get("total_summary")
    if isinstance(total_summary, dict):
        for key in ("due", "total"):
            value = total_summary.get(key)
            if isinstance(value, int):
                return value
    invoice = init_data.get("invoice")
    if isinstance(invoice, dict):
        for key in ("amount_due", "total"):
            value = invoice.get(key)
            if isinstance(value, int):
                return value
    value = init_data.get("amount_total")
    return value if isinstance(value, int) else 0


def _render_qr_png(payload: str, out_path: Path) -> None:
    """Render UPI URI thành PNG. Raise nếu qrcode chưa cài."""
    import qrcode  # type: ignore[import-untyped]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    image = qrcode.make(payload)
    image.save(out_path)


def _fmt_svg_num(v: float) -> str:
    """Format số trong SVG attribute: bỏ trailing .0, else 2 chữ số thập phân."""
    if v == int(v):
        return str(int(v))
    return f"{v:.2f}"


def _overlay_email_on_qr(qr_path: Path, email: str) -> None:
    """Vẽ email masked vào dưới ảnh QR (in-place).

    Email mask dùng cùng logic với ``telegram_notifier._mask_email`` để 2 nguồn
    hiển thị (Telegram caption + ảnh QR) đồng nhất.

    Hỗ trợ PNG (PIL extend canvas) và SVG (parse XML, append ``<rect>``+``<text>``).
    Detect format qua magic bytes — KHÔNG tin extension (Stripe có thể trả HTML
    với URL ``.svg``, runner re-render PNG nhưng giữ extension cũ).

    Raise nếu format không nhận diện được hoặc PIL/XML lỗi. Caller chịu trách
    nhiệm catch + log nhẹ — overlay là enhancement, không block toàn bộ QR.
    """
    from .telegram_notifier import _mask_email

    masked = _mask_email(email)
    head = qr_path.read_bytes()[:32]
    stripped = head.lstrip()
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        _overlay_email_on_png(qr_path, masked)
    elif stripped.startswith(b"<?xml") or stripped.startswith(b"<svg"):
        _overlay_email_on_svg(qr_path, masked)
    else:
        raise ValueError(
            f"unsupported QR image format (magic={head[:8]!r})"
        )


def _overlay_email_on_png(qr_path: Path, masked: str) -> None:
    """PIL: tạo canvas trắng cao hơn, paste QR + vẽ text masked center bottom."""
    from PIL import Image, ImageDraw, ImageFont

    with Image.open(qr_path) as src_raw:
        src = src_raw.convert("RGB")
    qr_w, qr_h = src.size
    band_h = max(int(qr_h * 0.12), 48)
    pad_x = max(int(qr_w * 0.04), 12)

    canvas = Image.new("RGB", (qr_w, qr_h + band_h), color="white")
    canvas.paste(src, (0, 0))

    # Font scalable (Pillow ≥ 10): load_default(size=N) — repo đã require
    # qrcode[pil]==8.2 nên Pillow ≥ 10 chắc chắn có. Fail-fast nếu cũ.
    font_size = max(int(band_h * 0.45), 18)
    try:
        font = ImageFont.load_default(size=font_size)
    except TypeError as exc:
        raise RuntimeError(
            "Pillow ≥ 10 required for ImageFont.load_default(size=...)"
        ) from exc

    draw = ImageDraw.Draw(canvas)
    bbox = draw.textbbox((0, 0), masked, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    max_w = qr_w - 2 * pad_x
    if text_w > max_w and text_w > 0:
        scale = max_w / text_w
        font_size = max(int(font_size * scale), 12)
        font = ImageFont.load_default(size=font_size)
        bbox = draw.textbbox((0, 0), masked, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
    x = (qr_w - text_w) // 2 - bbox[0]
    y = qr_h + (band_h - text_h) // 2 - bbox[1]
    draw.text((x, y), masked, fill="black", font=font)

    canvas.save(qr_path, format="PNG", optimize=True)


def _overlay_email_on_svg(qr_path: Path, masked: str) -> None:
    """Parse SVG XML, mở rộng viewBox + height, append band trắng + text."""
    import xml.etree.ElementTree as ET

    NS = "http://www.w3.org/2000/svg"
    ET.register_namespace("", NS)
    tree = ET.parse(qr_path)
    root = tree.getroot()
    if root.tag.split("}")[-1] != "svg":
        raise ValueError(f"root element không phải <svg> (tag={root.tag})")

    def _to_float(value: str | None, fallback: float) -> float:
        if not value:
            return fallback
        cleaned = value.strip().replace("px", "")
        try:
            return float(cleaned)
        except ValueError:
            return fallback

    width = _to_float(root.attrib.get("width"), 320.0)
    height = _to_float(root.attrib.get("height"), 320.0)
    vb_raw = root.attrib.get("viewBox") or f"0 0 {width} {height}"
    parts = vb_raw.split()
    if len(parts) != 4:
        raise ValueError(f"viewBox không hợp lệ: {vb_raw!r}")
    vx, vy, vw, vh = (float(p) for p in parts)

    band_vh = max(vh * 0.12, 48.0)
    band_h_px = max(height * 0.12, 48.0)

    rect = ET.SubElement(root, f"{{{NS}}}rect")
    rect.set("x", _fmt_svg_num(vx))
    rect.set("y", _fmt_svg_num(vy + vh))
    rect.set("width", _fmt_svg_num(vw))
    rect.set("height", _fmt_svg_num(band_vh))
    rect.set("fill", "white")

    text = ET.SubElement(root, f"{{{NS}}}text")
    text.set("x", _fmt_svg_num(vx + vw / 2))
    text.set("y", _fmt_svg_num(vy + vh + band_vh * 0.65))
    text.set("text-anchor", "middle")
    text.set("font-family", "Arial, Helvetica, sans-serif")
    text.set("font-size", _fmt_svg_num(band_vh * 0.45))
    text.set("fill", "black")
    text.text = masked

    root.set(
        "viewBox",
        f"{_fmt_svg_num(vx)} {_fmt_svg_num(vy)} "
        f"{_fmt_svg_num(vw)} {_fmt_svg_num(vh + band_vh)}",
    )
    root.set("height", _fmt_svg_num(height + band_h_px))

    tree.write(qr_path, encoding="utf-8", xml_declaration=True)


def _summarize_confirm(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: a.get(key) for key in (
            "variant", "phase", "http_status", "ok", "keys", "error",
        )}
        for a in attempts
    ]


def _summarize_approve(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: a.get(key)
            for key in (
                "variant", "attempt", "phase", "proxy", "http_status", "ok",
                "result", "error_type", "error", "keys",
            )
        }
        for a in attempts
    ]


def _summarize_refresh(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: a.get(key)
            for key in (
                "attempt", "proxy", "http_status", "ok", "error_type", "error", "keys",
            )
        }
        for a in attempts
    ]


# ─────────────────────────────────────────────────────────────────────
# Log formatters — 1 dòng / bước, cột căn đều + icon status.
#
# Format chuẩn:
#   "[step] label[16ch] icon detail"
#
# Vd:
#   [2/6] checkout         ✓  cs=cs_live_a1gMta…  ui=custom
#   [6/6] approve loop     ▸  retries=500 delay=3s batch=3
#         attempt 001/500  ✗  http=403  unknown    proxy=103.116.38.17:8003
# ─────────────────────────────────────────────────────────────────────

_STEP_WIDTH = 5     # "[N/6]"
_LABEL_WIDTH = 16   # đủ chứa "confirm flow_qr" (15)
_STATUS_ICONS: dict[str, str] = {
    "ok": "✓",
    "fail": "✗",
    "warn": "⚠",
    "blocked": "⊘",
    "start": "▸",
    "retry": "↻",
    "info": "·",
    "skip": "—",
}


def _fmt_step(step: str, label: str, status: str = "info", detail: str = "") -> str:
    """Format 1 dòng log step. ``step`` không cần [..], tự thêm + pad.

    Vd: _fmt_step("2/6", "checkout", "ok", "cs=...") →
        "[2/6] checkout         ✓  cs=..."
    """
    icon = _STATUS_ICONS.get(status, " ")
    step_token = f"[{step}]".ljust(_STEP_WIDTH + 2)
    label_token = label.ljust(_LABEL_WIDTH)
    line = f"{step_token}{label_token}{icon}"
    if detail:
        line += f"  {detail}"
    return line


def _fmt_attempt(
    *, idx: int, total: int,
    http_status: Any, result: Any, proxy_mask: str,
    elapsed: float | None = None,
) -> str:
    """Format dòng 1 attempt approve/refresh.

    Vd: _fmt_attempt(idx=1, total=500, http_status=403, result="unknown", ...) →
        "      attempt 001/500  ✗  http=403  unknown     proxy=..."
    """
    icons = {
        "approved": "ok", "blocked": "blocked", "exception": "warn",
        None: "fail", "unknown": "fail",
    }
    status_key = str(result) if result is not None else None
    icon = _STATUS_ICONS.get(icons.get(status_key, "fail"), "✗")
    n = f"{idx:0>3}/{total}"
    h = f"http={'---' if http_status is None else http_status}"
    r = (str(result) if result is not None else "—").ljust(10)
    base = f"      attempt {n:<8}  {icon}  {h:<9}  {r}  proxy={proxy_mask}"
    if elapsed is not None:
        base += f"  ({elapsed:.1f}s)"
    return base


def _fmt_kv(*pairs: tuple[str, Any]) -> str:
    """Format key=value dạng inline gọn, bỏ qua None."""
    return "  ".join(f"{k}={v}" for k, v in pairs if v not in (None, ""))


def _short(value: str | None, head: int = 12) -> str:
    """Rút gọn chuỗi dài (vd cs_live_xxx…) cho dễ đọc."""
    if not value:
        return "-"
    return value if len(value) <= head else value[:head] + "…"


def _silent(_: str) -> None:
    """Nuốt log — dùng để tắt log nội bộ của pay_upi_http khi runner tự log."""


class _RotatingSession:
    """AsyncSession wrapper tự hồi phục khi curl_cffi/BoringSSL raise lỗi TLS
    state (`OPENSSL_internal:invalid library`, `(35) TLS connect error`, ...).

    Root cause của `invalid library`: curl_cffi AsyncSession reuse pooled TLS
    connection (keep-alive); khi 1 connection qua proxy xoay bị remote/proxy
    reset giữa chừng, BoringSSL context của connection đó hỏng, mọi request
    reuse kế tiếp đều fail (xác nhận: lexiforest/curl_cffi#352 — fix tương
    đương `force_close=True` của aiohttp). Hai lớp phòng thủ:

      1. PREVENT — disable connection reuse (``FORBID_REUSE`` + ``FRESH_CONNECT``)
         nên mỗi request mở connection TLS mới, KHÔNG bao giờ reuse pooled
         connection đã hỏng. Đây là fix gốc, triệt tiêu corruption từ nguồn.
      2. RECOVER — nếu vẫn dính lỗi TLS (vd handshake transient do proxy/mạng),
         recreate inner session (fresh BoringSSL context + connection mới) rồi
         retry. Trong 1 call có thể advance impersonate như hedge tạm; nhưng
         KHÔNG degrade vĩnh viễn — mỗi call mới reset về fingerprint primary
         (chain[0]) qua ``_reset_to_primary`` nên vài SSLError transient không
         kéo session tụt xuống fingerprint cũ nhất mãi mãi (bug bản cũ: idx tăng
         vĩnh viễn → kẹt chrome136). Bounded ``_TLS_RECREATE_MAX_PER_CALL``
         lần/request + backoff để proxy chết THẬT raise sớm, nhường approve loop
         xoay proxy.

    Session sống xuyên suốt restart loop của 1 job. Forward attr khác (cookies,
    headers) qua ``__getattr__``. Wrap ``post``/``get``/``request`` bằng retry.
    """

    def __init__(self, impersonate_chain: tuple[str, ...], *, log: LogFn):
        if not impersonate_chain:
            raise ValueError("impersonate_chain không được rỗng")
        self._chain = list(impersonate_chain)
        self._idx = 0
        self._log = log
        self._inner: Any = None

    @staticmethod
    def _AsyncSessionClass():
        # Lazy import — tránh load curl_cffi khi module bị import context
        # khác (vd PyInstaller analyze).
        from curl_cffi.requests import AsyncSession
        return AsyncSession

    @staticmethod
    def _no_reuse_curl_options() -> dict:
        """curl options tắt connection reuse → mỗi request 1 connection TLS
        mới, không reuse pooled connection đã hỏng (root-cause fix)."""
        from curl_cffi.const import CurlOpt
        return {CurlOpt.FORBID_REUSE: 1, CurlOpt.FRESH_CONNECT: 1}

    @property
    def current_impersonate(self) -> str:
        return self._chain[self._idx]

    async def _open_inner(self, impersonate: str) -> None:
        AsyncSessionCls = self._AsyncSessionClass()
        self._inner = await AsyncSessionCls(
            impersonate=impersonate,
            curl_options=self._no_reuse_curl_options(),
        ).__aenter__()

    async def _close_inner(self, exc_type=None, exc=None, tb=None) -> None:
        if self._inner is not None:
            try:
                await self._inner.__aexit__(exc_type, exc, tb)
            except Exception:  # noqa: BLE001 — cleanup best-effort
                pass
            self._inner = None

    async def __aenter__(self) -> "_RotatingSession":
        await self._open_inner(self.current_impersonate)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._close_inner(exc_type, exc, tb)

    async def _reset_to_primary(self) -> None:
        """Recreate inner session về fingerprint ưu tiên (chain[0]).

        Gọi ở đầu mỗi call khi session đang bị degrade (``_idx != 0``). Lý do:
        SSLError/curl-35 qua proxy là lỗi tầng kết nối (proxy chập / mạng), KHÔNG
        phải fingerprint bị CF soi → 1 vài hiccup transient KHÔNG được phép kéo
        session tụt xuống fingerprint cũ vĩnh viễn. Mỗi request mới bắt đầu lại
        từ primary; rotation (nếu có) chỉ là hedge tạm trong phạm vi 1 call."""
        self._idx = 0
        await self._close_inner()
        await self._open_inner(self._chain[0])

    async def _recover(self, exc: BaseException) -> None:
        """Close inner hỏng + recreate fresh session để clear corrupt BoringSSL
        state và mở connection TLS mới (root cause của SSLError transient là
        connection, không phải fingerprint).

        Trong cùng 1 call, advance impersonate như 1 hedge (phòng trường hợp
        hiếm gặp 1 fingerprint cụ thể bị MITM/proxy chặn). KHÔNG degrade vĩnh
        viễn: ``_call_with_retry`` reset về primary ở đầu call kế tiếp."""
        await self._close_inner(type(exc), exc, exc.__traceback__)
        advanced = self._idx + 1 < len(self._chain)
        if advanced:
            self._idx += 1
        new_imp = self._chain[self._idx]
        self._log(_fmt_step(
            "upi", "tls recover", "retry",
            f"impersonate={new_imp}"
            f"{'' if advanced else ' (chain cạn → recreate same)'}"
            f"  lý do={type(exc).__name__} (transient — proxy/mạng)",
        ))
        await self._open_inner(new_imp)

    async def _call_with_retry(self, method_name: str, *args, **kwargs):
        """Gọi inner.<method_name>(*args, **kwargs). Catch TLS error → recover
        (recreate fresh session) + retry, bounded ``_TLS_RECREATE_MAX_PER_CALL``
        lần. Hết quota → propagate để caller (approve loop) xoay proxy.

        Đầu mỗi call: nếu call trước đã degrade fingerprint (``_idx != 0``) thì
        reset về primary trước — chống tụt vĩnh viễn xuống fingerprint cũ nhất."""
        if self._idx != 0:
            await self._reset_to_primary()
        recreate_count = 0
        while True:
            method = getattr(self._inner, method_name)
            try:
                return await method(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                if not _is_tls_error(exc):
                    raise
                if recreate_count >= _TLS_RECREATE_MAX_PER_CALL:
                    raise
                await self._recover(exc)
                await asyncio.sleep(_TLS_RECREATE_BACKOFF[
                    min(recreate_count, len(_TLS_RECREATE_BACKOFF) - 1)
                ])
                recreate_count += 1

    async def post(self, *args, **kwargs):
        return await self._call_with_retry("post", *args, **kwargs)

    async def get(self, *args, **kwargs):
        return await self._call_with_retry("get", *args, **kwargs)

    async def request(self, *args, **kwargs):
        return await self._call_with_retry("request", *args, **kwargs)

    def __getattr__(self, name: str):
        # Forward các attr ít dùng (cookies, headers, ...) vào inner. Bỏ qua
        # private fields của class này (Python sẽ tự lookup qua __dict__).
        if name.startswith("_"):
            raise AttributeError(name)
        inner = self.__dict__.get("_inner")
        if inner is None:
            raise AttributeError(f"_RotatingSession chưa enter context: .{name}")
        return getattr(inner, name)


# ─────────────────────────────────────────────────────────────────────
# Stripe / ChatGPT calls (clone từ probe — KHÔNG dùng pay_upi_http chính
# để tách dependency build_token_fields khỏi flow chính, đồng thời giữ
# variant logic riêng cho QR mode).
# ─────────────────────────────────────────────────────────────────────


async def _create_chatgpt_checkout(
    sess: Any,
    *,
    access_token: str,
    log: LogFn,
    proxies: dict[str, str] | None,
) -> dict[str, Any]:
    from pay_upi_http import _CHATGPT_CHECKOUT_URL, _USER_AGENT, PayUpiError
    from user_agent_profile import (
        SEC_CH_UA as _SEC_CH_UA,
        SEC_CH_UA_MOBILE as _SEC_CH_UA_MOBILE,
        SEC_CH_UA_PLATFORM as _SEC_CH_UA_PLATFORM,
    )

    body: dict[str, Any] = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": "IN", "currency": "INR"},
        "checkout_ui_mode": "custom",
    }
    referer = "https://chatgpt.com/?promo_campaign=plus-1-month-free"
    body["promo_campaign"] = {
        "promo_campaign_id": "plus-1-month-free",
        "is_coupon_from_query_param": False,
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Origin": "https://chatgpt.com",
        "Referer": referer,
        "User-Agent": _USER_AGENT,
        "sec-ch-ua": _SEC_CH_UA,
        "sec-ch-ua-mobile": _SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": _SEC_CH_UA_PLATFORM,
        "x-openai-target-path": "/backend-api/payments/checkout",
        "x-openai-target-route": "/backend-api/payments/checkout",
        "OAI-Language": "en-IN",
    }
    resp = await sess.post(
        _CHATGPT_CHECKOUT_URL, headers=headers, json=body, timeout=30, proxies=proxies,
    )
    if resp.status_code != 200:
        raise PayUpiError(f"checkout HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    needed = ("checkout_session_id", "publishable_key")
    miss = [key for key in needed if not data.get(key)]
    if miss:
        raise PayUpiError(f"checkout response missing {miss}: {data}")
    return data


async def _stripe_elements_session(
    sess: Any,
    *,
    session_id: str,
    publishable_key: str,
    stripe_js_id: str,
    amount: int,
    log: LogFn,
    proxies: dict[str, str] | None,
) -> dict[str, Any]:
    from pay_upi_http import (
        _STRIPE_ELEMENTS_URL, _STRIPE_VERSION, _USER_AGENT, PayUpiError,
    )
    from user_agent_profile import (
        SEC_CH_UA as _SEC_CH_UA,
        SEC_CH_UA_MOBILE as _SEC_CH_UA_MOBILE,
        SEC_CH_UA_PLATFORM as _SEC_CH_UA_PLATFORM,
    )

    params = {
        "client_betas[0]": "custom_checkout_server_updates_1",
        "client_betas[1]": "custom_checkout_manual_approval_1",
        "deferred_intent[mode]": "subscription",
        "deferred_intent[amount]": str(amount),
        "deferred_intent[currency]": "inr",
        "deferred_intent[setup_future_usage]": "off_session",
        "deferred_intent[payment_method_types][0]": "card",
        "deferred_intent[payment_method_types][1]": "link",
        "deferred_intent[payment_method_types][2]": "upi",
        "currency": "inr",
        "key": publishable_key,
        "_stripe_version": _STRIPE_VERSION,
        "elements_init_source": "custom_checkout",
        "referrer_host": "chatgpt.com",
        "stripe_js_id": stripe_js_id,
        "locale": "en",
        "type": "deferred_intent",
        "checkout_session_id": session_id,
    }
    headers = {
        "Accept": "application/json",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
        "User-Agent": _USER_AGENT,
        "sec-ch-ua": _SEC_CH_UA,
        "sec-ch-ua-mobile": _SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": _SEC_CH_UA_PLATFORM,
        "Accept-Language": "en-IN,en;q=0.9",
    }
    resp = await sess.get(
        _STRIPE_ELEMENTS_URL, headers=headers, params=params, timeout=30, proxies=proxies,
    )
    if resp.status_code != 200:
        raise PayUpiError(f"elements/sessions HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    if not data.get("session_id"):
        raise PayUpiError(f"elements/sessions missing session_id: keys={list(data)[:20]}")
    return data


async def _stripe_confirm_upi_qr(
    sess: Any,
    *,
    session_id: str,
    publishable_key: str,
    stripe_js_id: str,
    init_data: dict[str, Any],
    elements_data: dict[str, Any],
    profile: dict[str, Any],
    email: str,
    amount: int,
    variant: str,
    log: LogFn,
    token_config: Any | None,
    proxies: dict[str, str] | None,
) -> dict[str, Any]:
    from pay_upi_http import (
        _STRIPE_CONFIRM_URL, _STRIPE_VERSION, _USER_AGENT,
        _stripe_guid, _to_form,
    )
    from user_agent_profile import (
        SEC_CH_UA as _SEC_CH_UA,
        SEC_CH_UA_MOBILE as _SEC_CH_UA_MOBILE,
        SEC_CH_UA_PLATFORM as _SEC_CH_UA_PLATFORM,
    )

    elements_session_id = elements_data.get("session_id")
    elements_session_config_id = elements_data.get("config_id") or ""
    init_config_id = init_data.get("config_id") or ""
    ppage_id = init_data.get("id") or ""
    init_checksum = init_data["init_checksum"]

    if token_config is not None:
        import stripe_token as _st

        tokens = _st.build_token_fields(ppage_id=ppage_id, config=token_config)
        js_checksum = tokens["js_checksum"]
        rv_timestamp = tokens["rv_timestamp"]
    else:
        js_checksum = None
        rv_timestamp = None

    client_attribution_metadata = {
        "checkout_config_id": init_config_id,
        "checkout_session_id": session_id,
        "client_session_id": stripe_js_id,
        "elements_session_config_id": elements_session_config_id,
        "elements_session_id": elements_session_id,
        "merchant_integration_additional_elements": [
            "expressCheckout", "payment", "address",
        ],
        "merchant_integration_source": "checkout",
        "merchant_integration_subtype": "payment-element",
        "merchant_integration_version": "custom",
        "payment_intent_creation_flow": "deferred",
        "payment_method_selection_flow": "merchant_specified",
    }
    pmd_client_attribution = dict(client_attribution_metadata)
    pmd_client_attribution["merchant_integration_source"] = "elements"
    pmd_client_attribution["merchant_integration_version"] = "2021"

    form = _to_form({
        "_stripe_version": _STRIPE_VERSION,
        "client_attribution_metadata": client_attribution_metadata,
        "elements_options_client": {
            "saved_payment_method": {"enable_redisplay": "auto", "enable_save": "auto"},
        },
        "elements_session_client": {
            "client_betas": [
                "custom_checkout_server_updates_1", "custom_checkout_manual_approval_1",
            ],
            "elements_init_source": "custom_checkout",
            "is_aggregation_expected": "false",
            "locale": "en",
            "referrer_host": "chatgpt.com",
            "session_id": elements_session_id,
            "stripe_js_id": stripe_js_id,
        },
        "expected_amount": amount,
        "expected_payment_method_type": "upi",
        "guid": _stripe_guid(),
        "init_checksum": init_checksum,
        "js_checksum": js_checksum,
        "rv_timestamp": rv_timestamp,
        "passive_captcha_ekey": None,
        "passive_captcha_token": None,
        "key": publishable_key,
        "muid": _stripe_guid(),
        "sid": _stripe_guid(),
        "payment_method_data": {
            "billing_details": {
                "address": {
                    "city": profile["city"],
                    "country": "IN",
                    "line1": profile["address_line1"],
                    "postal_code": profile["postal_code"],
                    "state": profile["state"],
                },
                "email": email,
                "name": profile["name"],
            },
            "client_attribution_metadata": pmd_client_attribution,
            "payment_user_agent": (
                "stripe.js/e5ebd5e1e6; stripe-js-v3/e5ebd5e1e6; "
                "payment-element; deferred-intent"
            ),
            "referrer": "https://chatgpt.com",
            "time_on_page": int(time.time() * 1000) % 100000,
            "type": "upi",
            "upi": _upi_payload_for_variant(variant),
        },
        "return_url": _stripe_return_url(session_id),
        "version": "e5ebd5e1e6",
    })
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
        "User-Agent": _USER_AGENT,
        "sec-ch-ua": _SEC_CH_UA,
        "sec-ch-ua-mobile": _SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": _SEC_CH_UA_PLATFORM,
        "Accept-Language": "en-IN,en;q=0.9",
    }
    resp = await sess.post(
        _STRIPE_CONFIRM_URL.format(id=session_id),
        headers=headers, data=form, timeout=30, proxies=proxies,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"_raw": (resp.text or "")[:1000]}
    return {
        "variant": variant,
        "http_status": resp.status_code,
        "ok": resp.status_code == 200,
        "keys": list(data)[:30] if isinstance(data, dict) else [],
        "error": _redact_error(data.get("error")) if isinstance(data, dict) and data.get("error") else None,
        "data": data if resp.status_code == 200 else None,
    }


async def _stripe_payment_page_refresh(
    sess: Any,
    *,
    session_id: str,
    publishable_key: str,
    stripe_js_id: str,
    elements_data: dict[str, Any],
    log: LogFn,
    proxies: dict[str, str] | None,
) -> dict[str, Any]:
    from pay_upi_http import (
        _STRIPE_PAGE_URL, _STRIPE_VERSION, _USER_AGENT, _to_form,
    )
    from user_agent_profile import (
        SEC_CH_UA as _SEC_CH_UA,
        SEC_CH_UA_MOBILE as _SEC_CH_UA_MOBILE,
        SEC_CH_UA_PLATFORM as _SEC_CH_UA_PLATFORM,
    )

    params = _to_form({
        "elements_session_client": {
            "client_betas": [
                "custom_checkout_server_updates_1", "custom_checkout_manual_approval_1",
            ],
            "elements_init_source": "custom_checkout",
            "referrer_host": "chatgpt.com",
            "stripe_js_id": stripe_js_id,
            "locale": "en",
            "is_aggregation_expected": "false",
            "session_id": elements_data.get("session_id") or "",
        },
        "elements_options_client": {
            "saved_payment_method": {"enable_save": "auto", "enable_redisplay": "auto"},
        },
        "key": publishable_key,
        "_stripe_version": _STRIPE_VERSION,
    })
    headers = {
        "Accept": "application/json",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
        "User-Agent": _USER_AGENT,
        "sec-ch-ua": _SEC_CH_UA,
        "sec-ch-ua-mobile": _SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": _SEC_CH_UA_PLATFORM,
        "Accept-Language": "en-IN,en;q=0.9",
    }
    resp = await sess.get(
        _STRIPE_PAGE_URL.format(id=session_id),
        headers=headers, params=params, timeout=30, proxies=proxies,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"_raw": (resp.text or "")[:1000]}
    return {
        "http_status": resp.status_code,
        "ok": resp.status_code == 200,
        "keys": list(data)[:30] if isinstance(data, dict) else [],
        "error": _redact_error(data.get("error")) if isinstance(data, dict) and data.get("error") else None,
        "data": data if resp.status_code == 200 else None,
    }


async def _stripe_payment_page_refresh_retry(
    sess: Any,
    *,
    session_id: str,
    publishable_key: str,
    stripe_js_id: str,
    elements_data: dict[str, Any],
    log: LogFn,
    proxy_pool: list[str],
) -> dict[str, Any]:
    candidates = proxy_pool if proxy_pool else [None]
    last_attempt: dict[str, Any] | None = None
    for index, raw_proxy in enumerate(candidates, start=1):
        # proxy_pool nay là RAW templates → materialize concrete URL cho curl_cffi.
        proxy_url = _safe_materialize(raw_proxy)
        try:
            attempt = await _stripe_payment_page_refresh(
                sess,
                session_id=session_id,
                publishable_key=publishable_key,
                stripe_js_id=stripe_js_id,
                elements_data=elements_data,
                log=log,
                proxies=_proxy_dict(proxy_url),
            )
        except Exception as exc:  # noqa: BLE001
            attempt = {
                "http_status": None,
                "ok": False,
                "keys": [],
                "error_type": type(exc).__name__,
                "error": _sanitize_proxy(str(exc))[:300],
                "data": None,
            }
        attempt["proxy"] = _mask_proxy(raw_proxy)
        attempt["attempt"] = index
        last_attempt = attempt
        if attempt.get("ok"):
            return attempt
    return last_attempt or {
        "http_status": None,
        "ok": False,
        "keys": [],
        "error_type": "NoRefreshAttempt",
        "error": "no proxy candidates available",
        "data": None,
    }


async def _download_qr_image(
    sess: Any,
    *,
    url: str,
    out_path: Path,
    proxies: dict[str, str] | None,
) -> dict[str, Any]:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        resp = await sess.get(url, timeout=30, proxies=proxies)
    except Exception as exc:  # noqa: BLE001
        return {
            "downloaded": False,
            "error_type": type(exc).__name__,
            "error": _sanitize_proxy(str(exc))[:300],
        }
    if resp.status_code != 200:
        return {"downloaded": False, "status": resp.status_code}
    content_type = str(resp.headers.get("content-type") or "").lower()
    content = resp.content
    looks_like_html = "text/html" in content_type or content.lstrip().lower().startswith(b"<html")
    if looks_like_html:
        html_path = out_path.with_suffix(".html")
        html_path.write_bytes(content)
        html_text = content.decode("utf-8", errors="replace")
        upi_uri = _extract_hosted_instruction_upi_uri(html_text)
        if not upi_uri:
            return {
                "downloaded": False,
                "rendered": False,
                "reason": "hosted instructions HTML did not contain mobile_auth_url",
                "html_path": str(html_path),
            }
        _render_qr_png(upi_uri, out_path)
        result = {
            "downloaded": False,
            "rendered": True,
            "path": str(out_path),
            "source": "hosted_instructions_html",
            "html_path": str(html_path),
        }
        if out_path.exists():
            result["bytes"] = out_path.stat().st_size
        return result

    out_path.write_bytes(content)
    return {
        "downloaded": True,
        "rendered": True,
        "path": str(out_path),
        "bytes": len(content),
    }


async def _chatgpt_approve_checkout(
    sess: Any,
    *,
    access_token: str,
    session_id: str,
    log: LogFn,
    proxies: dict[str, str] | None,
) -> dict[str, Any]:
    from pay_upi_http import _CHATGPT_APPROVE_URL, _USER_AGENT
    from user_agent_profile import (
        SEC_CH_UA as _SEC_CH_UA,
        SEC_CH_UA_MOBILE as _SEC_CH_UA_MOBILE,
        SEC_CH_UA_PLATFORM as _SEC_CH_UA_PLATFORM,
    )

    body = {"checkout_session_id": session_id, "processor_entity": "openai_llc"}
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Origin": "https://chatgpt.com",
        "Referer": f"https://chatgpt.com/checkout/openai_llc/{session_id}",
        "User-Agent": _USER_AGENT,
        "sec-ch-ua": _SEC_CH_UA,
        "sec-ch-ua-mobile": _SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": _SEC_CH_UA_PLATFORM,
        "x-openai-target-path": "/backend-api/payments/checkout/approve",
        "x-openai-target-route": "/backend-api/payments/checkout/approve",
        "OAI-Language": "en-IN",
    }
    resp = await sess.post(
        _CHATGPT_APPROVE_URL, headers=headers, json=body, timeout=30, proxies=proxies,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"_raw": (resp.text or "")[:1000]}
    result = data.get("result") if isinstance(data, dict) else None
    return {
        "http_status": resp.status_code,
        "ok": resp.status_code == 200 and result == "approved",
        "result": result,
        "keys": list(data)[:30] if isinstance(data, dict) else [],
        "data": data if resp.status_code == 200 else None,
    }


# [FIX-CF-WARMUP-2026-06-20] — GET chatgpt.com để CF set __cf_bm trước
# khi gọi /approve. Lý do: hit đầu của 1 proxy mới luôn dính CF challenge
# 403 (chưa có cookie __cf_bm). Sau warm-up, cookie nằm trong jar của
# AsyncSession → approve request kế tiếp không bị 403 nữa.
#
# Best-effort: bất kỳ exception/non-200 đều nuốt + log warn, KHÔNG fail
# approve loop (warm-up fail không có nghĩa proxy chết — cứ cho approve thử).
#
# Caller: gọi 1 lần mỗi khi sang batch proxy mới (xem call site trong
# while-loop của approve, ở chỗ `if batch_idx not in _approve_mat_cache:`).
#
# REVERT: xoá hàm này + xoá `await _warm_cf_cookie(...)` ở call site.
async def _warm_cf_cookie(
    sess: Any,
    *,
    proxies: dict[str, str] | None,
    log: LogFn,
) -> None:
    from pay_upi_http import _USER_AGENT
    from user_agent_profile import (
        SEC_CH_UA as _SEC_CH_UA,
        SEC_CH_UA_MOBILE as _SEC_CH_UA_MOBILE,
        SEC_CH_UA_PLATFORM as _SEC_CH_UA_PLATFORM,
    )
    headers = {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-IN,en;q=0.9",
        "User-Agent": _USER_AGENT,
        "sec-ch-ua": _SEC_CH_UA,
        "sec-ch-ua-mobile": _SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": _SEC_CH_UA_PLATFORM,
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
    }
    try:
        resp = await sess.get(
            "https://chatgpt.com/", headers=headers, timeout=15, proxies=proxies,
        )
        status = resp.status_code
    except Exception as exc:  # noqa: BLE001
        log(_fmt_step(
            "6/6", "cf-warmup", "warn",
            f"skip ({type(exc).__name__}: {str(exc)[:80]}) — approve sẽ tự xoay nếu 403",
        ))
        return
    # 200/403 đều có Set-Cookie __cf_bm; chỉ flag warn cho status lạ.
    icon = "ok" if status in (200, 403) else "warn"
    log(_fmt_step(
        "6/6", "cf-warmup", icon,
        f"http={status}  proxy={_mask_proxy((proxies or {}).get('https'))}",
    ))


def _is_backend_exception(attempt: dict[str, Any]) -> bool:
    return attempt.get("http_status") == 200 and attempt.get("result") == "exception"


def _is_retryable_reject(attempt: dict[str, Any]) -> bool:
    """Attempt thuộc loại 'IP/edge/risk reject' mà re-login + IP/checkout mới
    có thể fix.

    = Stripe ``result=="blocked"`` (risk reject) HOẶC ``http_status in (403, 429)``
    (Cloudflare/edge chặn IP / rate-limit). KHÔNG gồm:
      - exception/network (``http_status is None``) — transient, re-login spam → lockout;
      - backend ``result=="exception"`` (http 200) — server hiccup, không phải IP;
      - approved / http khác (500…) — không phải tín hiệu đổi IP.
    """
    if attempt.get("result") == "blocked":
        return True
    return attempt.get("http_status") in (403, 429)


def cycle_all_blocked(approve_attempts: list[dict[str, Any]]) -> bool:
    """True ⟺ có >=1 approve attempt VÀ TẤT CẢ là IP/edge reject (blocked|403|429).

    Outer-cycle (UpiJobManager) dùng để quyết định re-login: chỉ re-login khi
    nguyên nhân fail là IP/edge — login + checkout/IP mới mới có ý nghĩa. Bất kỳ
    attempt approved/exception/network/http-khác → False (không re-login: tránh
    spam login khi lỗi transient/server). Pure function → unit-test truth table.
    """
    if not approve_attempts:
        return False
    return all(_is_retryable_reject(a) for a in approve_attempts)


def _is_network_error(attempt: dict[str, Any]) -> bool:
    """Attempt fail vì network/timeout/DNS — KHÔNG có response từ server.

    Phân biệt với 4xx/5xx (proxy block hoặc Stripe transient): network error
    có ``http_status is None`` (vì except block trong approve không có resp).
    """
    return attempt.get("http_status") is None


async def _probe_connectivity(sess: Any) -> bool:
    """Best-effort check kết nối ra Internet (DIRECT, không proxy).

    Trả True nếu nhận được BẤT KỲ HTTP response nào từ chatgpt.com (kể cả
    4xx/5xx — server alive). False khi network/DNS/timeout fail.
    """
    try:
        resp = await sess.head(
            "https://chatgpt.com/",
            timeout=NETWORK_PROBE_TIMEOUT_SECONDS,
            proxies=None,
            allow_redirects=False,
        )
        # Bất cứ status code nào (200, 301, 403, 502...) đều chứng tỏ mạng OK.
        return getattr(resp, "status_code", None) is not None
    except Exception:  # noqa: BLE001
        return False


async def _wait_network_recovery(sess: Any, log: LogFn) -> bool:
    """Pause approve loop, poll connectivity tới khi mạng OK hoặc timeout.

    Returns:
        True  — mạng đã recover, caller resume bình thường.
        False — vượt quá NETWORK_RECOVERY_MAX_WAIT_SECONDS, caller fatal-break.
    """
    started = monotonic()
    poll_idx = 0
    while True:
        elapsed = monotonic() - started
        if elapsed > NETWORK_RECOVERY_MAX_WAIT_SECONDS:
            log(_fmt_step(
                "net", "outage", "fail",
                f"không recover trong {NETWORK_RECOVERY_MAX_WAIT_SECONDS:.0f}s",
            ))
            return False
        ok = await _probe_connectivity(sess)
        poll_idx += 1
        if ok:
            log(_fmt_step(
                "net", "recovered", "ok",
                f"sau {elapsed:.0f}s ({poll_idx} probes) — resume approve loop",
            ))
            return True
        # Log gọn mỗi N lần để khỏi spam
        if poll_idx == 1 or poll_idx % 6 == 0:  # lần đầu + mỗi ~30s
            log(_fmt_step(
                "net", "waiting", "info",
                f"poll {poll_idx} elapsed={elapsed:.0f}s "
                f"max={NETWORK_RECOVERY_MAX_WAIT_SECONDS:.0f}s",
            ))
        await asyncio.sleep(NETWORK_RECOVERY_POLL_SECONDS)


# ─────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────


async def run_upi_qr_probe(
    *,
    email: str,
    password: str,
    secret: str | None,
    proxy_pool: list[str],
    approve_retries: int,
    qr_out_path: Path,
    log: LogFn,
    db_path: str | None = None,  # noqa: ARG001 — proxy pool truyền trực tiếp
    restart_threshold: int = 0,
    max_restarts: int = 0,
    proxy_from_step: int = PROXY_FROM_STEP,
    approve_retry_delay: float | None = None,
    relogin_block_streak: int = 0,
    auth_sink: dict[str, Any] | None = None,
    login_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    force_fresh: bool = False,
    login_proxy_url: str | None = None,
) -> UpiQrResult:
    """Login + checkout + confirm UPI + approve loop → save QR PNG.

    Args:
        email/password/secret: ChatGPT credentials. ``secret`` = TOTP secret nếu
            account có 2FA.
        proxy_pool: list proxy line/template (raw, có thể chứa ``{SID}``). Empty
            = direct mọi step.
        approve_retries: số lần retry approve (>=1).
        qr_out_path: PNG file path để lưu QR (sẽ tự tạo parent dir).
        log: callable(str) — mỗi dòng log gọi callback this.
        restart_threshold: số `result=exception` LIÊN TIẾP để trigger restart
            checkout session (giữ login + giữ retry budget cộng dồn). 0 =
            disabled.
        max_restarts: số lần restart tối đa trong 1 job. Hết quota → fatal
            break. 0 = disabled.
        proxy_from_step: 1-6, step bắt đầu áp proxy cho flow (đồng bộ
            ``pay_upi_http._ProxyPolicy`` schema). Default lấy từ
            ``PROXY_FROM_STEP``. Khi =1, step 1 (login) cũng route qua
            ``proxy_pool[0]``; ngược lại login DIRECT.
        auth_sink: optional mutable dict — runner sẽ fill 3 keys NGAY sau khi
            login Step1 thành công: ``access_token``, ``session_cookies``,
            ``active_proxy``. Caller (UpiJobManager) đọc dict này khi
            ``wait_for(...)`` raise ``TimeoutError`` để vẫn check_plan được
            (acc có thể đã lên Plus dù approve loop bị kill bởi timeout).
            None = không expose token sớm (caller lấy từ ``UpiQrResult`` cuối
            cùng — KHÔNG khả dụng nếu timeout).
        login_proxy_url: proxy URL áp riêng cho Step 1 (login) + Step 2
            (checkout) — use case: host IP non-VN/JP cần proxy geo-eligible
            để ChatGPT trả promo plus. None/empty = giữ luồng cũ (login DIRECT,
            Step 2 theo ``proxy_from_step``). Khi set, Step 1 thử qua proxy
            tối đa ``LOGIN_PROXY_RETRY_QUOTA`` attempt; nếu retryable fail
            hết quota → fallback DIRECT cho attempt cuối + Step 2 cũng quay
            lại logic cũ (không che lỗi, log warning rõ ràng). Step 3-6 KHÔNG
            đụng — vẫn dùng ``proxy_pool`` xoay.

    Returns:
        UpiQrResult — luôn trả (kể cả khi fail), KHÔNG raise. Caller check
        ``result.ok`` để biết success. ``proxy_used`` = concrete URL Stripe
        Steps 2-5 dùng (= IP để check_plan replay sau).
    """
    if approve_retries < 1:
        raise UpiQrError(f"approve_retries phải >= 1, got {approve_retries}")
    if restart_threshold < 0:
        raise UpiQrError(f"restart_threshold phải >= 0, got {restart_threshold}")
    if max_restarts < 0:
        raise UpiQrError(f"max_restarts phải >= 0, got {max_restarts}")
    if relogin_block_streak < 0:
        raise UpiQrError(f"relogin_block_streak phải >= 0, got {relogin_block_streak}")
    if not (1 <= proxy_from_step <= 6):
        raise UpiQrError(
            f"proxy_from_step phải trong [1, 6], got {proxy_from_step}"
        )
    # Resolve approve delay — None → fallback module default (`APPROVE_DELAY`).
    # Dưới 5s sẽ bị Stripe approve rate-limit (`blocked` http=200 lặp lại) →
    # validate tại boundary để fail-fast nếu caller pass giá trị quá nhỏ.
    if approve_retry_delay is None:
        effective_approve_delay: float = float(APPROVE_DELAY)
    else:
        effective_approve_delay = float(approve_retry_delay)
        if not (2.0 <= effective_approve_delay <= 60.0):
            raise UpiQrError(
                f"approve_retry_delay phải trong [2, 60] giây, got {approve_retry_delay}"
            )
    # Restart logic chỉ kích hoạt khi cả 2 > 0.
    restart_enabled = restart_threshold > 0 and max_restarts > 0

    started = monotonic()
    masked_email = _mask_email(email)
    masked_proxy_pool = [_mask_proxy(p) for p in proxy_pool]
    # Steps 2-5 dùng first_proxy = materialize raw_pool[0]. proxy_pool nay là
    # RAW templates (lazy-materialize ở approve/refresh) → first_proxy là URL
    # concrete với SID stable cho 1 job (không xoay giữa Step 2-5).
    # KHÔNG silent direct — _materialize_or_log_warning log rõ nếu format lỗi.
    first_proxy = (
        _materialize_or_log_warning(proxy_pool[0], log) if proxy_pool else None
    )
    masked_first_proxy = _mask_proxy(first_proxy)

    def _safe_log(msg: str) -> None:
        # Mask email + proxy trước khi log để không leak credential vào job log.
        safe = msg.replace(email, masked_email)
        for raw, masked in zip(proxy_pool, masked_proxy_pool):
            safe = safe.replace(raw, masked)
        if first_proxy:
            safe = safe.replace(first_proxy, masked_first_proxy)
        log(safe)

    _safe_log(_fmt_step("upi", "account", "info",
                        f"{masked_email}  proxy_pool={len(proxy_pool)}"))
    _safe_log(_fmt_step("upi", "config", "info", _fmt_kv(
        ("approve_retries", approve_retries),
        ("delay", f"{effective_approve_delay:g}s"),
        ("batch", APPROVE_PROXY_BATCH),
        ("proxy_from_step", proxy_from_step),
        ("be_consec", APPROVE_BACKEND_EXCEPTION_CONSECUTIVE
                      if APPROVE_BACKEND_EXCEPTION_CONSECUTIVE > 0 else "off"),
        ("restart", (f"{restart_threshold}/{max_restarts}"
                     if restart_enabled else "off")),
        ("variants", ",".join(CONFIRM_VARIANTS)),
    )))

    # ── Proxy diagnostic ─────────────────────────────────────────────
    # In rõ "step nào ăn proxy, step nào DIRECT" để user verify policy đang áp
    # đúng cấu hình (debug "chập chờn ăn proxy" / "lúc thì direct lúc thì proxy").
    # Pool có entries nhưng first_proxy=None → format[0] lỗi (đã log warning ở
    # _materialize_or_log_warning). Fail-fast: raise UpiQrError để user fix
    # format thay vì im lặng chạy DIRECT toàn bộ flow.
    if proxy_pool and first_proxy is None:
        raise UpiQrError(
            f"proxy_pool có {len(proxy_pool)} entries nhưng first_proxy "
            f"resolve = None — format proxy[0] không hợp lệ. Xem log "
            f"`[proxy] format-error warn` ở trên để biết chi tiết. "
            f"Sửa Settings tab > Proxy Pool đúng format `host:port[:user[:pass]]`."
        )
    _step_names = {
        1: "login", 2: "checkout", 3: "stripe_init",
        4: "elements", 5: "confirm", 6: "approve",
    }
    _proxy_steps = []
    _direct_steps = []
    for s in range(1, 7):
        label = _step_names[s]
        if first_proxy and s >= proxy_from_step:
            _proxy_steps.append(f"{s}={label}")
        else:
            _direct_steps.append(f"{s}={label}")
    _safe_log(_fmt_step("upi", "proxy policy", "info", _fmt_kv(
        ("first_proxy", masked_first_proxy if first_proxy else "DIRECT"),
        ("via_proxy", ",".join(_proxy_steps) if _proxy_steps else "-"),
        ("direct", ",".join(_direct_steps) if _direct_steps else "-"),
    )))

    # Login proxy override (Step 1+2 ưu tiên URL/raw riêng nếu user paste).
    # Diagnostic chỉ inform — materialize canonical xảy ra trong login loop
    # phía dưới (1 lần duy nhất → SID stable cho {SID} template).
    _login_proxy_raw = (login_proxy_url or "").strip() or None
    if _login_proxy_raw:
        _safe_log(_fmt_step(
            "upi", "login proxy override", "info",
            f"{_mask_proxy(_login_proxy_raw)} → Step 1+2 "
            f"(fallback DIRECT + Step 2 theo proxy_from_step nếu proxy dead "
            f"hoặc format lỗi)",
        ))

    def _step_proxy_tag(step: int) -> str:
        """Format 'proxy=via ***@host:port' / 'proxy=DIRECT' cho step log.

        Đặt nội bộ trong scope vì depend `first_proxy` + `proxy_from_step` +
        `masked_first_proxy` (closure). Mỗi step log gọi để hiển thị nhất quán
        proxy đang áp ở step đó — fix UX 'chập chờn ăn proxy' cho job qua web
        UI (production runner truyền log=_silent vào pay_upi_http nên fix log
        format ở pay_upi_http không hiển thị; phải nhồi proxy info ở caller).
        """
        if first_proxy and step >= proxy_from_step:
            return f"proxy=via {masked_first_proxy}"
        return "proxy=DIRECT"

    # Lazy import → chỉ khi job thật sự chạy.
    from curl_cffi.requests import AsyncSession  # noqa: F401 — kept for type hints
    import stripe_token as _st
    from pay_upi_http import _stripe_init
    from random_profile import random_india_profile
    from session_phase import SessionError, get_session_pure_request, is_fatal_login_error

    # ─────────────────────────────────────────────────────────────────
    # Step 1 — login với retry. SessionError dạng "WARNING_BANNER" /
    # "no accessToken" / "session-token cookie" / "callback URL" là transient
    # (Cloudflare/proxy/server flaky, callback set-cookie chậm) → retry tối đa
    # 3 lần (initial + 2 retry). Lỗi vĩnh viễn (wrong password, MFA fail,
    # no mail provider) raise ngay không retry — tránh login spam → lockout.
    # DIRECT: no proxy để giảm captcha trên ChatGPT.
    # ─────────────────────────────────────────────────────────────────
    LOGIN_MAX_ATTEMPTS = 3
    LOGIN_RETRY_DELAY = 3.0
    # Khi user set ``login_proxy_url``, dùng proxy cho 2 attempt đầu. Nếu cả
    # 2 fail retryable (network/timeout — proxy có thể dead) → attempt cuối
    # DIRECT để vẫn cứu được job. Khi xảy ra fallback, Step 2 cũng quay về
    # logic ``proxy_from_step`` (không dùng login proxy nữa) để không lặp lại
    # cùng lỗi.
    LOGIN_PROXY_RETRY_QUOTA = 2

    def _is_login_error_retryable(exc_msg: str) -> bool:
        return not is_fatal_login_error(exc_msg)

    _safe_log(_fmt_step("1/6", "login", "start", "pure-HTTP request_phase"))
    session_data: dict[str, Any] | None = None
    last_login_error: str | None = None
    # ── Login proxy override: thử proxy trước, fallback DIRECT nếu dead ──
    # Behavior cũ: Step1 LUÔN DIRECT (proxy=None) để tránh Cloudflare
    # captcha/403 từ IP datacenter. UPI bám golden Get Session.
    # Behavior mới: nếu user paste ``login_proxy_url`` (geo-eligible VN/JP
    # cho promo plus) → ưu tiên proxy này cho Step 1 + 2. Proxy dead → log
    # warning + fallback DIRECT (giữ flow tiếp, không che lỗi).
    # Session-token KHÔNG bind IP nên login qua proxy A, approve qua pool B
    # đều hợp lệ.
    # Materialize raw line/template → URL concrete (cùng helper với pool).
    # Format lỗi → _materialize_or_log_warning trả None + log warning →
    # treat như không set (luồng cũ giữ).
    _login_raw = (login_proxy_url or "").strip() or None
    requested_login_proxy = (
        _materialize_or_log_warning(_login_raw, _safe_log)
        if _login_raw else None
    )
    # Local copy có thể bị reset về None nếu detect dead — Step 2 đọc biến
    # này để quyết định checkout proxy.
    effective_login_proxy: str | None = requested_login_proxy
    proxy_fallback_logged = False
    for login_attempt in range(1, LOGIN_MAX_ATTEMPTS + 1):
        # Chọn proxy cho attempt này: trong quota → dùng proxy, hết quota →
        # DIRECT (fallback). Lần đầu hết quota in log + clear effective.
        if requested_login_proxy and login_attempt <= LOGIN_PROXY_RETRY_QUOTA:
            current_proxy = requested_login_proxy
        else:
            if requested_login_proxy and not proxy_fallback_logged:
                _safe_log(_fmt_step(
                    "1/6", "login", "warn",
                    f"login proxy fail {LOGIN_PROXY_RETRY_QUOTA} lần → fallback DIRECT "
                    f"cho attempt {login_attempt}/{LOGIN_MAX_ATTEMPTS} + "
                    f"Step 2 theo proxy_from_step={proxy_from_step}",
                ))
                proxy_fallback_logged = True
                effective_login_proxy = None
            current_proxy = None
        try:
            if login_fn is not None:
                # Cache-aware login (UPI only): reuse cookie cache (revalidate
                # /api/auth/session) → bỏ qua login. force_fresh=True (cycle
                # re-login đổi IP) → manager wrapper bỏ reuse, login mới.
                session_data = await login_fn(force_fresh=force_fresh, proxy=current_proxy)
            else:
                session_data = await get_session_pure_request(
                    email=email,
                    password=password,
                    secret=secret,
                    proxy=current_proxy,
                    log=_safe_log,
                )
            if login_attempt > 1:
                via = "qua proxy" if current_proxy else "DIRECT"
                _safe_log(
                    f"[upi-qr] login OK ở attempt {login_attempt}/{LOGIN_MAX_ATTEMPTS} ({via})"
                )
            break
        except SessionError as exc:
            last_login_error = str(exc)
            retryable = _is_login_error_retryable(last_login_error)
            if not retryable:
                _safe_log(
                    f"[upi-qr] login fail (non-retryable): {last_login_error[:200]}"
                )
                break
            if login_attempt >= LOGIN_MAX_ATTEMPTS:
                _safe_log(
                    f"[upi-qr] login fail after {LOGIN_MAX_ATTEMPTS} attempts: "
                    f"{last_login_error[:200]}"
                )
                break
            _safe_log(
                f"[upi-qr] login transient error "
                f"(attempt {login_attempt}/{LOGIN_MAX_ATTEMPTS}): "
                f"{last_login_error[:140]} — retry sau {LOGIN_RETRY_DELAY:g}s..."
            )
            await asyncio.sleep(LOGIN_RETRY_DELAY)

    if session_data is None:
        _safe_log(_fmt_step("1/6", "login", "fail", last_login_error or "unknown"))
        return UpiQrResult(
            ok=False, email=masked_email,
            # Sanitize: last_login_error = str(SessionError) có thể nhúng proxy
            # URL (curl error qua login_proxy) → job.error → SSE/UI.
            error=_sanitize_proxy(f"login fail: {last_login_error or 'unknown'}"),
            elapsed_seconds=monotonic() - started,
            proxy_used=first_proxy,
        )

    access_token = session_data.get("accessToken")
    if not isinstance(access_token, str) or not access_token:
        _safe_log(_fmt_step("1/6", "login", "fail", "không có accessToken trong response"))
        return UpiQrResult(
            ok=False, email=masked_email,
            error="login OK nhưng không có accessToken",
            elapsed_seconds=monotonic() - started,
            proxy_used=first_proxy,
        )
    user_email = (session_data.get("user") or {}).get("email") or masked_email
    _safe_log(_fmt_step("1/6", "login", "ok", f"user={user_email}"))

    # Expose auth artifacts cho caller NGAY sau login OK — caller (UpiJobManager)
    # đọc khi `wait_for(...)` raise TimeoutError để vẫn check_plan được (acc có
    # thể đã lên Plus dù approve loop bị kill bởi timeout). Best-effort: fill
    # mọi key, missing key = caller ignore.
    if auth_sink is not None:
        try:
            auth_sink["access_token"] = access_token
            auth_sink["session_cookies"] = (
                session_data.get("__cookies")
                if isinstance(session_data, dict) else None
            )
            # active_proxy = first_proxy concrete (= IP login đã dùng / sẽ dùng
            # cho Stripe Steps khi proxy_from_step <= 5). check_plan replay qua
            # đúng IP → tránh 403/correlation entitlement endpoint.
            auth_sink["active_proxy"] = first_proxy
        except Exception:  # noqa: BLE001 — sink fill best-effort, không break flow
            pass

    stripe_js_id = str(uuid.uuid4())
    confirm_attempts: list[dict[str, Any]] = []
    approve_attempts: list[dict[str, Any]] = []
    page_refresh_attempts: list[dict[str, Any]] = []
    backend_exception_count = 0
    consecutive_backend_exception = 0
    fatal_approve_error: str | None = None
    # Block-streak re-login: set True khi N approve attempt IP/edge-reject liên
    # tiếp (chỉ khi relogin_block_streak>0) → outer loop re-login. Persist qua phase.
    relogin_requested = False
    amount = 0
    return_url = ""
    session_id = ""
    qr_image_url: str | None = None
    upi_uri: str | None = None
    qr_expires_at: int | None = None
    payment_link: str | None = None

    # ─────────────────────────────────────────────────────────────────
    # Step 2-6 — bọc trong restart loop. Mỗi "phase" tạo 1 checkout session
    # mới (giữ login + retry budget cộng dồn). Khi `consecutive_backend_
    # exception >= restart_threshold` và còn quota `max_restarts` → break
    # approve loop, sang phase kế. KHÔNG reset `approve_index_total` và
    # `proxy_virtual_attempt`.
    # ─────────────────────────────────────────────────────────────────
    profile = random_india_profile()  # đồng nhất profile mọi phase
    token_config = None  # extract 1 lần ở phase 1, các phase sau reuse
    final_confirmed = False
    final_approved = False
    approved = False
    approve_index_total = 0    # cumulative qua phase — KHÔNG reset
    proxy_virtual_attempt = 0  # cumulative qua phase — KHÔNG reset
    restart_count = 0
    triggered_restart = False
    # Per-phase context (overwrite mỗi phase) — giữ ở scope ngoài để
    # aggregate matches dùng phase cuối.
    checkout: dict[str, Any] = {}
    init_data: dict[str, Any] = {}
    elements_data: dict[str, Any] = {}
    publishable_key = ""
    _proxy_advance_enabled_static = (
        proxy_from_step <= 6
        and APPROVE_PROXY_BATCH > 1
        and len(proxy_pool) > 1
    )

    async with _RotatingSession(_IMPERSONATE_CHAIN, log=_safe_log) as sess:
        while True:
            phase_idx = restart_count + 1  # 1-based
            phase_tag = f" [p{phase_idx}]" if restart_enabled else ""
            triggered_restart = False  # reset per-phase

            if restart_count > 0:
                _safe_log(_fmt_step(
                    "upi", "restart", "info",
                    f"phase {phase_idx}/{max_restarts + 1}  → new checkout session  "
                    f"approve_idx kept at {approve_index_total}/{approve_retries}",
                ))

            # Step 2 — checkout creation (DIRECT - chatgpt API).
            # Proxy precedence:
            #   1. ``effective_login_proxy`` (login proxy override + còn alive)
            #   2. fallback luồng cũ: ``first_proxy`` nếu proxy_from_step<=2,
            #      ngược lại DIRECT.
            # Khi effective_login_proxy bị reset = None do dead, mặc nhiên
            # quay về branch fallback (luồng cũ).
            try:
                _step2_proxy = (
                    effective_login_proxy
                    if effective_login_proxy is not None
                    else (first_proxy if proxy_from_step <= 2 else None)
                )
                checkout = await _create_chatgpt_checkout(
                    sess, access_token=access_token, log=_silent,
                    proxies=_proxy_dict(_step2_proxy),
                )
            except Exception as exc:  # noqa: BLE001
                # Phase 1 fail → propagate (giữ behavior cũ là raise).
                # Phase >1 fail → fatal restart, không retry tiếp.
                if restart_count == 0:
                    raise
                fatal_approve_error = (
                    f"phase {phase_idx} checkout fail: "
                    f"{type(exc).__name__}: {str(exc)[:200]}"
                )
                _safe_log(_fmt_step(f"2/6{phase_tag}", "checkout", "fail",
                                    fatal_approve_error[:160]))
                break

            session_id = checkout["checkout_session_id"]
            return_url = _stripe_return_url(session_id)
            publishable_key = checkout["publishable_key"]
            _safe_log(_fmt_step(
                f"2/6{phase_tag}", "checkout", "ok",
                f"cs={_short(session_id, 14)}  ui={checkout.get('checkout_ui_mode') or '-'}  "
                f"{_step_proxy_tag(2)}",
            ))

            # Step 3 — Stripe init.
            try:
                init_data = await _stripe_init(
                    sess,
                    session_id=session_id,
                    publishable_key=publishable_key,
                    stripe_js_id=stripe_js_id,
                    log=_silent,
                    proxies=_proxy_for_step(first_proxy, from_step=proxy_from_step, step=3),
                )
            except Exception as exc:  # noqa: BLE001
                if restart_count == 0:
                    raise
                fatal_approve_error = (
                    f"phase {phase_idx} init fail: "
                    f"{type(exc).__name__}: {str(exc)[:200]}"
                )
                _safe_log(_fmt_step(f"3/6{phase_tag}", "init", "fail",
                                    fatal_approve_error[:160]))
                break
            amount = _extract_amount(init_data)
            _safe_log(_fmt_step(
                f"3/6{phase_tag}", "init", "ok",
                f"amount={amount}  ppage={_short(init_data.get('id') or '', 12)}  "
                f"{_step_proxy_tag(3)}",
            ))
            if PROMO and amount > 0:
                _safe_log(_fmt_step("upi", "no free offer", "fail",
                                    f"amount={amount} (promo bật nhưng > 0)"))
                if restart_count == 0:
                    return UpiQrResult(
                        ok=False, email=masked_email, amount=amount, return_url=return_url,
                        checkout_session=str(session_id)[:18] + "...",
                        error="no free offer (promo enabled but amount > 0)",
                        elapsed_seconds=monotonic() - started,
                    )
                fatal_approve_error = (
                    f"phase {phase_idx} no free offer (amount={amount})"
                )
                break

            # Step 4 — elements/sessions.
            try:
                elements_data = await _stripe_elements_session(
                    sess,
                    session_id=session_id,
                    publishable_key=publishable_key,
                    stripe_js_id=stripe_js_id,
                    amount=amount,
                    log=_silent,
                    proxies=_proxy_for_step(first_proxy, from_step=proxy_from_step, step=4),
                )
            except Exception as exc:  # noqa: BLE001
                if restart_count == 0:
                    raise
                fatal_approve_error = (
                    f"phase {phase_idx} elements fail: "
                    f"{type(exc).__name__}: {str(exc)[:200]}"
                )
                _safe_log(_fmt_step(f"4/6{phase_tag}", "elements", "fail",
                                    fatal_approve_error[:160]))
                break
            _safe_log(_fmt_step(
                f"4/6{phase_tag}", "elements", "ok",
                f"session={_short(elements_data.get('session_id') or '', 14)}  "
                f"{_step_proxy_tag(4)}",
            ))

            # Step 5a — extract Stripe token config (best-effort, chỉ phase 1).
            if restart_count == 0:
                try:
                    token_config = await _st.extract_config_live(
                        sess, log=_silent, use_cache=True,
                        fallback_dir=Path(__file__).resolve().parents[1]
                        / "runtime" / "cache" / "stripe_bundles_default",
                        proxies=None,
                    )
                    _safe_log(_fmt_step(
                        "5a", "token-config", "ok",
                        f"shift={token_config.shift}  rv={_short(token_config.rv, 8)}",
                    ))
                except _st.StripeTokenExtractError as exc:
                    _safe_log(_fmt_step(
                        "5a", "token-config", "warn",
                        f"extract fail: {str(exc)[:120]}",
                    ))

            # Step 5b — confirm variants. Mỗi phase thử lại từ đầu list.
            phase_confirmed = False
            confirm_variant_used: str | None = None
            for variant in CONFIRM_VARIANTS:
                attempt = await _stripe_confirm_upi_qr(
                    sess,
                    session_id=session_id,
                    publishable_key=publishable_key,
                    stripe_js_id=stripe_js_id,
                    init_data=init_data,
                    elements_data=elements_data,
                    profile=profile,
                    email=email,
                    amount=amount,
                    variant=variant,
                    log=_silent,
                    token_config=token_config,
                    proxies=_proxy_for_step(first_proxy, from_step=proxy_from_step, step=5),
                )
                attempt["phase"] = phase_idx
                confirm_attempts.append(attempt)
                confirm_status = "ok" if attempt.get("ok") else "fail"
                confirm_detail = (
                    f"variant={variant}  http={attempt.get('http_status')}  "
                    f"{_step_proxy_tag(5)}"
                )
                err = attempt.get("error")
                if err and isinstance(err, dict):
                    code = err.get("code") or err.get("type") or ""
                    if code:
                        confirm_detail += f"  err={code}"
                _safe_log(_fmt_step(f"5b{phase_tag}", "confirm", confirm_status, confirm_detail))
                if attempt.get("ok"):
                    phase_confirmed = True
                    confirm_variant_used = variant
                    break

            if not phase_confirmed:
                if restart_count == 0:
                    # Phase 1 confirm fail mọi variant → behavior cũ:
                    # final_confirmed=False, fall through aggregate (QR có thể
                    # vẫn pop từ confirm response payload), error sẽ là
                    # "confirm thất bại với mọi variant".
                    break
                fatal_approve_error = (
                    f"phase {phase_idx} confirm fail mọi variant"
                )
                _safe_log(_fmt_step(
                    f"5b{phase_tag}", "confirm", "fail",
                    "all variants failed in restart phase",
                ))
                break
            final_confirmed = True

            # Step 5c — refresh trước approve loop.
            refresh_attempt = await _stripe_payment_page_refresh_retry(
                sess,
                session_id=session_id,
                publishable_key=publishable_key,
                stripe_js_id=stripe_js_id,
                elements_data=elements_data,
                log=_silent,
                proxy_pool=proxy_pool if proxy_from_step <= 5 else [],
            )
            page_refresh_attempts.append(refresh_attempt)
            _safe_log(_fmt_step(
                f"5c{phase_tag}", "page-refresh",
                "ok" if refresh_attempt.get("ok") else "fail",
                f"http={refresh_attempt.get('http_status')}  proxy={refresh_attempt.get('proxy')}",
            ))

            # Step 6 — approve loop. Cumulative counter, KHÔNG reset.
            if restart_count == 0:
                _safe_log(_fmt_step(
                    "6/6", "approve loop", "start",
                    f"retries={approve_retries}  delay={effective_approve_delay:g}s  "
                    f"batch={APPROVE_PROXY_BATCH}"
                    + (f"  restart={restart_threshold}/{max_restarts}"
                       if restart_enabled else ""),
                ))
            else:
                _safe_log(_fmt_step(
                    f"6/6{phase_tag}", "approve loop", "info",
                    f"resume from {approve_index_total}/{approve_retries}  "
                    f"(consec be_excpt reset)",
                ))
            consecutive_backend_exception = 0  # reset đầu mỗi phase
            consecutive_network_error = 0      # reset đầu mỗi phase
            consecutive_reject = 0             # reset đầu mỗi phase (block-streak)

            approve_started = monotonic()
            # Cache materialized URL theo batch-index: sticky SID/IP trong 1
            # batch, SID/IP tươi sang batch mới. Reset mỗi phase restart →
            # phase mới spawn IP tươi (không reuse cache cũ).
            _approve_mat_cache: dict[int, str | None] = {}
            while approve_index_total < approve_retries:
                approve_index_total += 1
                proxy_virtual_attempt += 1
                raw_approve_proxy = _proxy_url_for_retry(
                    proxy_pool,
                    from_step=proxy_from_step,
                    step=6,
                    attempt=proxy_virtual_attempt,
                    per_proxy_attempts=APPROVE_PROXY_BATCH,
                )
                batch_idx = (proxy_virtual_attempt - 1) // APPROVE_PROXY_BATCH
                if batch_idx not in _approve_mat_cache:
                    _approve_mat_cache[batch_idx] = _safe_materialize(raw_approve_proxy)
                    # [FIX-CF-WARMUP-2026-06-20] proxy mới → warm CF cookie
                    # trước approve đầu của batch (giảm 403 hit đầu).
                    # REVERT: xoá block await này + xoá hàm _warm_cf_cookie.
                    await _warm_cf_cookie(
                        sess,
                        proxies=_proxy_dict(_approve_mat_cache[batch_idx]),
                        log=_safe_log,
                    )
                approve_proxy = _approve_mat_cache[batch_idx]  # concrete URL (no {SID})
                try:
                    approve_attempt = await _chatgpt_approve_checkout(
                        sess,
                        access_token=access_token,
                        session_id=session_id,
                        log=_silent,
                        proxies=_proxy_dict(approve_proxy),
                    )
                except Exception as exc:  # noqa: BLE001
                    approve_attempt = {
                        "http_status": None,
                        "ok": False,
                        "result": None,
                        "keys": [],
                        "error_type": type(exc).__name__,
                        "error": _sanitize_proxy(str(exc))[:300],
                        "data": None,
                    }
                approve_attempt["variant"] = confirm_variant_used
                approve_attempt["attempt"] = approve_index_total
                approve_attempt["phase"] = phase_idx
                approve_attempt["proxy"] = _mask_proxy(approve_proxy)
                approve_attempts.append(approve_attempt)
                _safe_log(_fmt_attempt(
                    idx=approve_index_total, total=approve_retries,
                    http_status=approve_attempt.get("http_status"),
                    result=approve_attempt.get("result") or approve_attempt.get("error_type"),
                    proxy_mask=_mask_proxy(approve_proxy),
                ))
                if approve_attempt.get("ok"):
                    approved = True
                    break
                # Block-streak detector (chỉ active khi relogin_block_streak>0,
                # tức max_outer_cycles>1 ở manager). N attempt IP/edge-reject
                # (blocked|403|429) LIÊN TIẾP → break cycle sớm để outer loop
                # re-login + checkout/IP mới, KHÔNG đốt hết approve_retries.
                if relogin_block_streak > 0:
                    if _is_retryable_reject(approve_attempt):
                        consecutive_reject += 1
                        if consecutive_reject >= relogin_block_streak:
                            relogin_requested = True
                            _safe_log(_fmt_step(
                                "6/6", "approve", "warn",
                                f"block-streak {consecutive_reject}/{relogin_block_streak} "
                                f"IP/edge-reject liên tiếp → break cycle để re-login",
                            ))
                            break
                    else:
                        consecutive_reject = 0
                # Phân loại response để counter chính xác:
                #   - http_status=None → network/timeout/DNS error (KHÔNG có
                #     response từ Stripe). Stripe state vẫn nguyên, KHÔNG đụng
                #     vào consecutive_backend_exception.
                #   - http_status=200 + result=exception → Stripe stuck. Tăng
                #     consecutive_backend_exception, reset network counter.
                #   - http_status=200 + result clean (approved/blocked/...) →
                #     Stripe state OK. Reset CẢ 2 counter.
                #   - http_status=4xx/5xx → proxy block hoặc Stripe transient.
                #     Reset network counter (ít nhất có response), KHÔNG reset
                #     consecutive_backend_exception (Stripe có thể vẫn stuck).
                if _is_network_error(approve_attempt):
                    consecutive_network_error += 1
                    if consecutive_network_error >= NETWORK_FAIL_DETECT:
                        _safe_log(_fmt_step(
                            "net", "outage", "warn",
                            f"{consecutive_network_error} timeouts/network errors "
                            f"liên tiếp → pause approve loop, poll connectivity",
                        ))
                        ok_recover = await _wait_network_recovery(sess, _safe_log)
                        if ok_recover:
                            consecutive_network_error = 0
                            # Resume approve loop ở attempt kế (KHÔNG đốt thêm
                            # budget trong lúc đợi). Skip sleep cuối loop.
                            continue
                        fatal_approve_error = (
                            f"network outage không recover trong "
                            f"{NETWORK_RECOVERY_MAX_WAIT_SECONDS:.0f}s "
                            f"(consec network errors={consecutive_network_error})"
                        )
                        break
                elif _is_backend_exception(approve_attempt):
                    consecutive_network_error = 0
                    backend_exception_count += 1
                    consecutive_backend_exception += 1

                    # NEW: restart trigger — khi consecutive đạt threshold và
                    # vẫn còn quota restart_count < max_restarts. KHÔNG reset
                    # approve_index_total / proxy_virtual_attempt.
                    if (
                        restart_enabled
                        and consecutive_backend_exception >= restart_threshold
                        and restart_count < max_restarts
                    ):
                        triggered_restart = True
                        _safe_log(_fmt_step(
                            f"6/6{phase_tag}", "approve", "warn",
                            f"consec be_excpt {consecutive_backend_exception}/"
                            f"{restart_threshold} → restart "
                            f"({restart_count + 1}/{max_restarts})",
                        ))
                        break

                    # Hard threshold cũ (default 0 = OFF). Khi user opt-in
                    # APPROVE_BACKEND_EXCEPTION_CONSECUTIVE > 0, sẽ fatal-break.
                    if (
                        APPROVE_BACKEND_EXCEPTION_CONSECUTIVE > 0
                        and consecutive_backend_exception
                        >= APPROVE_BACKEND_EXCEPTION_CONSECUTIVE
                    ):
                        fatal_approve_error = (
                            f"approve consecutive backend_exception threshold "
                            f"({consecutive_backend_exception}/"
                            f"{APPROVE_BACKEND_EXCEPTION_CONSECUTIVE}) "
                            f"total_exceptions={backend_exception_count}"
                        )
                        _safe_log(_fmt_step(
                            "6/6", "approve", "fail",
                            f"consec be_excpt {consecutive_backend_exception}/"
                            f"{APPROVE_BACKEND_EXCEPTION_CONSECUTIVE}",
                        ))
                        break

                    # Advance proxy ngay khi gặp backend_exception.
                    if _proxy_advance_enabled_static:
                        current_batch = (proxy_virtual_attempt - 1) // APPROVE_PROXY_BATCH
                        position_in_batch = (
                            proxy_virtual_attempt - current_batch * APPROVE_PROXY_BATCH
                        )
                        if position_in_batch < APPROVE_PROXY_BATCH:
                            proxy_virtual_attempt = (current_batch + 1) * APPROVE_PROXY_BATCH
                else:
                    http_st = approve_attempt.get("http_status")
                    res = approve_attempt.get("result")
                    if http_st == 200 and res and res != "exception":
                        # Stripe trả response clean (vd "blocked") → state OK.
                        consecutive_network_error = 0
                        if consecutive_backend_exception > 0:
                            _safe_log(_fmt_step(
                                "6/6", "approve", "info",
                                f"reset consec be_excpt "
                                f"({consecutive_backend_exception} → 0) "
                                f"result={res}",
                            ))
                            consecutive_backend_exception = 0
                    else:
                        # 4xx/5xx hoặc 200 thiếu result — proxy/Stripe issue.
                        # Reset network counter (server alive), giữ stripe.
                        consecutive_network_error = 0
                if approve_index_total < approve_retries:
                    await asyncio.sleep(effective_approve_delay)

            approve_elapsed = monotonic() - approve_started

            if approved:
                final_approved = True
                _safe_log(_fmt_step(
                    "6/6", "approve", "ok",
                    f"approved at {approve_index_total}/{approve_retries}  "
                    f"({approve_elapsed:.1f}s, restarts={restart_count})",
                ))

            # Refresh post-approve (best-effort) — chỉ làm khi không restart
            # (vì restart sẽ làm refresh ở step 5c phase kế).
            if not triggered_restart and not fatal_approve_error and (
                approved or approve_attempts
            ):
                refresh2 = await _stripe_payment_page_refresh_retry(
                    sess,
                    session_id=session_id,
                    publishable_key=publishable_key,
                    stripe_js_id=stripe_js_id,
                    elements_data=elements_data,
                    log=_silent,
                    proxy_pool=proxy_pool if proxy_from_step <= 5 else [],
                )
                page_refresh_attempts.append(refresh2)
                _safe_log(_fmt_step(
                    f"5c{phase_tag}", "page-refresh",
                    "ok" if refresh2.get("ok") else "fail",
                    f"http={refresh2.get('http_status')}  proxy={refresh2.get('proxy')}",
                ))

            # Decide outcome of phase
            if approved or fatal_approve_error or relogin_requested:
                break
            if approve_index_total >= approve_retries:
                # Cạn budget mà vẫn không approved → kết thúc, không restart.
                _safe_log(_fmt_step(
                    "6/6", "approve", "fail",
                    f"không approved sau {approve_retries} attempts "
                    f"({approve_elapsed:.1f}s, restarts={restart_count})",
                ))
                break
            if triggered_restart:
                restart_count += 1
                continue
            # Defensive: nếu không trigger gì mà loop vẫn break (không nên xảy
            # ra trong logic hiện tại), vẫn out để tránh treo.
            break

        # Aggregate matches từ mọi response (kể cả khi approve fail — QR có thể
        # đã có từ confirm response để user scan thủ công).
        matches: list[dict[str, Any]] = []
        matches.extend(_find_matches(checkout, source="chatgpt_checkout"))
        matches.extend(_find_matches(init_data, source="stripe_init"))
        matches.extend(_find_matches(elements_data, source="stripe_elements"))
        for attempt in confirm_attempts:
            if attempt.get("data") is not None:
                matches.extend(_find_matches(attempt["data"], source=f"confirm:{attempt['variant']}"))
        for attempt in approve_attempts:
            if attempt.get("data") is not None:
                matches.extend(_find_matches(attempt["data"], source=f"approve:{attempt['variant']}"))
        for index, attempt in enumerate(page_refresh_attempts, start=1):
            if attempt.get("data") is not None:
                matches.extend(_find_matches(attempt["data"], source=f"payment_page_refresh:{index}"))
        upi_uri = _find_upi_uri(matches)
        qr_image_url = _find_qr_image_url(matches)
        qr_expires_at = _find_qr_expires_at(matches)
        payment_link = _find_hosted_instructions_url(matches)

        # QR rendering (download Stripe image hoặc render từ upi:// URI).
        qr_path: str | None = None
        qr_source: str | None = None
        qr_reason: str | None = None
        if qr_image_url:
            extension = ".svg" if qr_image_url.lower().endswith(".svg") else ".png"
            target = qr_out_path.with_suffix(extension)
            qr_dl = await _download_qr_image(
                sess, url=qr_image_url, out_path=target,
                proxies=_proxy_for_step(first_proxy, from_step=proxy_from_step, step=5),
            )
            if qr_dl.get("rendered") and qr_dl.get("path"):
                qr_path = qr_dl["path"]
                qr_source = qr_dl.get("source") or "stripe_image"
            else:
                qr_reason = qr_dl.get("reason") or qr_dl.get("error") or "stripe image download fail"
        elif upi_uri:
            try:
                _render_qr_png(upi_uri, qr_out_path)
                qr_path = str(qr_out_path)
                qr_source = "upi_uri"
            except Exception as exc:  # noqa: BLE001
                qr_reason = f"qrcode render fail: {type(exc).__name__}: {exc}"
        else:
            qr_reason = "no upi:// URI or QR image URL found in any response"

        # Overlay email masked lên ảnh QR (cùng logic mask với Telegram caption).
        # Best-effort: lỗi overlay log riêng, KHÔNG bỏ qr_path — QR raw vẫn dùng
        # được. Path canonical đã ghi đè in-place, nên Telegram + web modal +
        # clipboard tự động dùng ảnh đã có watermark.
        if qr_path:
            try:
                _overlay_email_on_qr(Path(qr_path), email)
            except Exception as exc:  # noqa: BLE001
                _safe_log(_fmt_step(
                    "qr", "overlay", "fail",
                    f"{type(exc).__name__}: {str(exc)[:200]}",
                ))

        # QR final log + final summary
        if qr_path:
            qr_detail = _fmt_kv(
                ("source", qr_source),
                ("expires_at", qr_expires_at),
            )
            _safe_log(_fmt_step("qr", "saved", "ok", qr_detail))
        else:
            _safe_log(_fmt_step("qr", "saved", "fail", qr_reason or "unknown"))

        if payment_link:
            _safe_log(_fmt_step("upi", "pay link", "ok", payment_link))

    elapsed = monotonic() - started

    # Determine success: cần CẢ approve approved + qr file rendered.
    # Ưu tiên error: fatal (consec be_excpt threshold) > confirm fail > approve fail > qr fail.
    if fatal_approve_error:
        error_msg = fatal_approve_error
    elif not final_confirmed:
        error_msg = "confirm thất bại với mọi variant"
    elif not final_approved:
        error_msg = (
            f"approve không thành công sau {len(approve_attempts)} attempts "
            f"(retries={approve_retries})"
        )
    elif not qr_path:
        error_msg = qr_reason or "no QR generated"
    else:
        error_msg = None

    ok = error_msg is None
    _safe_log(_fmt_step(
        "upi", "done", "ok" if ok else "fail",
        (f"qr={'yes' if qr_path else 'no'}  approved={'yes' if final_approved else 'no'}  "
         f"restarts={restart_count}  total={elapsed:.1f}s")
        + (f"  error={error_msg}" if error_msg else ""),
    ))

    return UpiQrResult(
        ok=ok,
        email=masked_email,
        amount=amount,
        return_url=return_url,
        checkout_session=str(session_id)[:18] + "..." if session_id else "",
        qr_path=qr_path,
        qr_source=qr_source,
        qr_source_url=qr_image_url,
        qr_reason=qr_reason,
        qr_expires_at=qr_expires_at,
        payment_link=payment_link,
        has_upi_uri=bool(upi_uri),
        has_qr_image_url=bool(qr_image_url),
        confirm_attempts=_summarize_confirm(confirm_attempts),
        approve_attempts=_summarize_approve(approve_attempts),
        page_refresh_attempts=_summarize_refresh(page_refresh_attempts),
        backend_exception_count=backend_exception_count,
        restart_count=restart_count,
        error=error_msg,
        elapsed_seconds=elapsed,
        # Lưu auth artifacts để caller (UpiJobManager) re-check session sau
        # khi QR hết hạn. Cookies được get_session_pure_request inject vào
        # session_data["__cookies"] (httpOnly cookie chatgpt.com). access_token
        # giữ luôn cho future use (chưa cần ngay vì /api/auth/session dùng
        # cookies, không Bearer).
        access_token=access_token,
        session_cookies=(
            session_data.get("__cookies")
            if isinstance(session_data, dict)
            else None
        ),
        proxy_used=first_proxy,
        relogin_requested=relogin_requested,
    )


__all__ = [
    "PROMO", "PROXY_FROM_STEP", "DO_CONFIRM", "DO_APPROVE",
    "APPROVE_DELAY", "APPROVE_PROXY_BATCH",
    "APPROVE_BACKEND_EXCEPTION_CONSECUTIVE",
    "APPROVE_RESTART_THRESHOLD", "APPROVE_MAX_RESTARTS",
    "CONFIRM_VARIANTS",
    "UpiQrResult", "UpiQrError", "run_upi_qr_probe",
]
