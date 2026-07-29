"""Telegram notifier — gửi QR UPI + thông tin account qua Telegram Bot API.

Single source of truth cho config là Settings Store (`telegram.bot_token`,
`telegram.chat_id`, `upi.notify_enabled`). Notifier hydrate qua `apply_settings`
lúc startup và cập nhật live qua setters (write-through ở endpoint).

Flow khi 1 job UPI success + có QR:
    1. sendPhoto (PNG) / sendDocument (SVG) — caption gồm amount + thời điểm
       hết hạn theo giờ VN (Asia/Ho_Chi_Minh) và Ấn Độ (Asia/Kolkata).
    2. sendMessage reply vào tin trên — nội dung `email|password|secret` để
       dạng spoiler (tap mới hiện) tránh lộ khi lướt nhanh.

Dùng curl_cffi.AsyncSession (đồng nhất với phần còn lại của repo). Fail-fast,
không nuốt lỗi: trả dict kết quả + raise TelegramNotifyError khi gửi fail để
caller (UpiJobManager) log vào job.
"""
from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

LogFn = Callable[[str], None]

_API_BASE = "https://api.telegram.org/bot{token}/{method}"
_TZ_VN = ZoneInfo("Asia/Ho_Chi_Minh")
_TZ_IN = ZoneInfo("Asia/Kolkata")
_TIME_FMT = "%H:%M:%S %d/%m/%Y"

# Format đồng nhất với upi_runner: "[tg]   label[16ch] icon  detail"
_TG_LABEL_WIDTH = 16


def _tg_line(label: str, icon: str, detail: str = "") -> str:
    line = f"[tg]   {label.ljust(_TG_LABEL_WIDTH)}{icon}"
    if detail:
        line += f"  {detail}"
    return line


class TelegramNotifyError(Exception):
    """Gửi Telegram thất bại (HTTP non-200 hoặc API ok=false)."""


def _fmt_amount(amount: int) -> str:
    if not amount:
        return "-"
    return f"₹{amount / 100:.2f}"


def _fmt_expiry(expires_at: int | None) -> str:
    """Format hết hạn theo giờ VN + IN, mỗi múi 1 dòng. None → 'không xác định'."""
    if not expires_at:
        return "Hết hạn: không xác định"
    vn = datetime.fromtimestamp(expires_at, _TZ_VN).strftime(_TIME_FMT)
    inn = datetime.fromtimestamp(expires_at, _TZ_IN).strftime(_TIME_FMT)
    return f"Hết hạn: {vn} VN\nExpired: {inn} IN"


def _mask_email(email: str | None) -> str:
    """Ẩn email cho hiển thị Telegram: giữ alias trừ 3 ký tự cuối + ẩn domain trừ TLD.

    Ví dụ: ``lantrinh1xyz@hotmail.com`` → ``lantrinh1***@****.com``.
    Alias có ≤ 3 ký tự → toàn bộ thay bằng ``***``.
    Domain không có dot/TLD → thay bằng ``****``.
    Email rỗng / không hợp lệ → ``***@****``.
    """
    if not email or "@" not in email:
        return "***@****"
    local, _, domain = email.partition("@")
    if not local:
        masked_local = "***"
    elif len(local) <= 3:
        masked_local = "***"
    else:
        masked_local = local[:-3] + "***"

    dot = domain.rfind(".")
    if dot <= 0 or dot == len(domain) - 1:
        masked_domain = "****"
    else:
        masked_domain = "****" + domain[dot:]

    return f"{masked_local}@{masked_domain}"


class TelegramNotifier:
    """Quản lý config + gửi thông báo Telegram. Singleton qua get_telegram_notifier()."""

    def __init__(self) -> None:
        self._enabled: bool = False
        self._bot_token: str | None = None
        self._chat_id: str | None = None

    # ── Config ──────────────────────────────────────────────────────────
    def apply_settings(self, settings: dict[str, Any]) -> None:
        """Hydrate từ settings dict lúc startup. Chỉ set khi key có mặt."""
        if "upi.notify_enabled" in settings:
            self._enabled = bool(settings["upi.notify_enabled"])
        if "telegram.bot_token" in settings:
            token = settings["telegram.bot_token"]
            self._bot_token = token.strip() if isinstance(token, str) and token.strip() else None
        if "telegram.chat_id" in settings:
            chat = settings["telegram.chat_id"]
            self._chat_id = chat.strip() if isinstance(chat, str) and chat.strip() else None

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def set_credentials(self, bot_token: str | None, chat_id: str | None) -> None:
        self._bot_token = bot_token.strip() if isinstance(bot_token, str) and bot_token.strip() else None
        self._chat_id = chat_id.strip() if isinstance(chat_id, str) and chat_id.strip() else None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def configured(self) -> bool:
        return bool(self._bot_token and self._chat_id)

    @property
    def bot_token(self) -> str | None:
        return self._bot_token

    @property
    def chat_id(self) -> str | None:
        return self._chat_id

    # ── Telegram API ────────────────────────────────────────────────────
    async def _call(self, sess: Any, method: str, *, data: dict[str, Any],
                    multipart: Any | None = None) -> dict[str, Any]:
        """Gọi 1 method Telegram Bot API.

        ``data``: dict text fields (chat_id, caption, parse_mode, text, ...).
        ``multipart``: ``curl_cffi.CurlMime`` instance khi gửi kèm file
            (sendPhoto/sendDocument). Khi multipart != None, phải gắn cả text
            fields vào multipart luôn — curl_cffi không cho dùng đồng thời
            ``data`` + ``multipart``. Caller phụ trách chuyện này; method này
            chỉ nhận MỘT trong hai.
        """
        url = _API_BASE.format(token=self._bot_token, method=method)
        if multipart is not None:
            resp = await sess.post(url, multipart=multipart, timeout=30)
        else:
            resp = await sess.post(url, data=data, timeout=30)
        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise TelegramNotifyError(
                f"{method} HTTP {resp.status_code}: non-JSON response"
            ) from exc
        if resp.status_code != 200 or not payload.get("ok"):
            desc = payload.get("description") if isinstance(payload, dict) else None
            raise TelegramNotifyError(
                f"{method} failed: HTTP {resp.status_code} {desc or payload}"
            )
        return payload

    async def send_test(self, *, log: LogFn | None = None) -> dict[str, Any]:
        """Gửi 1 tin test để verify token + chat_id."""
        if not self.configured:
            raise TelegramNotifyError("bot_token/chat_id chưa cấu hình")
        from curl_cffi.requests import AsyncSession

        text = (
            "✅ <b>gpt_signup_hybrid</b> — Telegram đã kết nối.\n"
            f"<i>{datetime.now(_TZ_VN).strftime(_TIME_FMT)} (VN)</i>"
        )
        async with AsyncSession() as sess:
            payload = await self._call(
                sess, "sendMessage",
                data={"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"},
            )
        if log:
            log(_tg_line("test", "✓", "sendMessage OK"))
        return payload

    async def notify_upi_qr(
        self,
        *,
        email: str,
        password: str,
        secret: str | None,
        amount: int,
        qr_path: str | None,
        qr_expires_at: int | None,
        checkout_session: str | None = None,
        return_url: str | None = None,
        payment_link: str | None = None,
        log: LogFn | None = None,
    ) -> bool:
        """Gửi QR (caption gồm email masked + thời điểm hết hạn). Combo reply
        hiện đang DISABLED (xem comment block trong body) — caller chỉ nhận
        ảnh QR, copy combo lấy từ Output pane trên web UI.

        Returns:
            True nếu đã gửi; False nếu skip (tắt toggle / chưa cấu hình / không có QR).
        Raises:
            TelegramNotifyError nếu gửi thất bại.
        """
        def _log(msg: str) -> None:
            if log:
                log(msg)

        if not self._enabled:
            return False
        if not self.configured:
            _log(_tg_line("skip", "—", "chưa cấu hình bot_token/chat_id"))
            return False
        if not qr_path:
            _log(_tg_line("skip", "—", "job không có QR file"))
            return False
        path = Path(qr_path)
        if not path.exists():
            _log(_tg_line("skip", "—", f"QR file không tồn tại: {path.name}"))
            return False

        is_svg = path.suffix.lower() == ".svg"
        # Caption gọn: tiêu đề + email masked + 2 dòng hết hạn (VN/IN). Bỏ
        # amount/checkout — combo đầy đủ ở tin reply (code block tap-to-copy).
        caption_lines = [
            "🟢 <b>UPI QR — ChatGPT Plus (IN)</b>",
            f"Email: {html.escape(_mask_email(email))}",
            _fmt_expiry(qr_expires_at),
        ]
        # Link thanh toán hosted (payments.stripe.com/upi/instructions/...) —
        # mở ra trang QR + nút mở app UPI. Hữu ích khi user trả bằng điện thoại.
        if payment_link:
            caption_lines.append(
                f'💳 <a href="{html.escape(payment_link, quote=True)}">Link thanh toán UPI</a>'
            )
        caption = "\n".join(caption_lines)

        # ─── DISABLED: combo reply ─────────────────────────────────────────
        # Hiện tại không gửi tin reply chứa `email|password|secret` (spam channel
        # + đã có Output pane trên web UI để copy). Giữ code dạng comment để
        # bật lại sau bằng cách uncomment block này + block sendMessage bên dưới.
        # combo = f"{email}|{password}|{secret or ''}"
        # # Vừa spoiler vừa code: ẩn sau lớp spoiler, tap mở ra là code block
        # # với nút Copy (monospace). Telegram cho phép lồng <tg-spoiler> + <pre>.
        # reply_text = f"<tg-spoiler><pre>{html.escape(combo)}</pre></tg-spoiler>"
        # ──────────────────────────────────────────────────────────────────

        from curl_cffi import CurlMime
        from curl_cffi.requests import AsyncSession

        content = path.read_bytes()
        media_field = "document" if is_svg else "photo"
        method = "sendDocument" if is_svg else "sendPhoto"
        mime_type = "image/svg+xml" if is_svg else "image/png"

        # Build multipart: tất cả text fields PHẢI gắn vào CurlMime cùng file
        # vì curl_cffi không cho dùng đồng thời data= + multipart=.
        mp = CurlMime()
        mp.addpart(name="chat_id", data=str(self._chat_id).encode("utf-8"))
        mp.addpart(name="caption", data=caption.encode("utf-8"))
        mp.addpart(name="parse_mode", data=b"HTML")
        mp.addpart(
            name=media_field,
            content_type=mime_type,
            filename=path.name,
            data=content,
        )

        async with AsyncSession() as sess:
            try:
                media_payload = await self._call(sess, method, data={}, multipart=mp)
            finally:
                mp.close()
            # ─── DISABLED: combo reply (uncomment để bật lại) ──────────────
            # message_id = (
            #     media_payload.get("result", {}).get("message_id")
            #     if isinstance(media_payload.get("result"), dict) else None
            # )
            # reply_data: dict[str, Any] = {
            #     "chat_id": self._chat_id,
            #     "text": reply_text,
            #     "parse_mode": "HTML",
            # }
            # if message_id is not None:
            #     reply_data["reply_to_message_id"] = message_id
            # await self._call(sess, "sendMessage", data=reply_data)
            # ──────────────────────────────────────────────────────────────
            del media_payload  # currently unused — giữ block fetch để keep
            # parity nếu uncomment reply ở trên.

        _log(_tg_line("sent", "✓", "QR only (combo reply disabled)"))
        return True


# ── Singleton ───────────────────────────────────────────────────────────
_notifier: TelegramNotifier | None = None


def get_telegram_notifier() -> TelegramNotifier:
    global _notifier  # noqa: PLW0603
    if _notifier is None:
        _notifier = TelegramNotifier()
    return _notifier
