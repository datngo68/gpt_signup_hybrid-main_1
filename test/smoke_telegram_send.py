"""Smoke test thật: gửi QR + combo lên Telegram qua bot.

Verify CurlMime path hoạt động (không còn NotImplementedError với files=).

Dùng:
    TG_BOT_TOKEN=123:ABC TG_CHAT_ID=999 python3 test/smoke_telegram_send.py

Optional:
    TG_QR_PATH=/path/to/qr.png   (mặc định: tạo PNG giả 32x32 màu trắng)
    TG_AMOUNT=2000               (₹20.00 — UPI dùng paise)
    TG_EXPIRES_IN=600            (giây từ now)

Script tự in [PASS]/[FAIL] từng bước. Không buffer (flush=True).
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _need(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"[FAIL] env {name} không có — set trước khi chạy", flush=True)
        sys.exit(2)
    return val


def _ensure_qr_png() -> Path:
    """Lấy QR từ env hoặc render PNG mẫu (32x32 trắng) để test multipart."""
    custom = os.environ.get("TG_QR_PATH", "").strip()
    if custom:
        p = Path(custom)
        if not p.exists():
            print(f"[FAIL] TG_QR_PATH không tồn tại: {p}", flush=True)
            sys.exit(2)
        return p
    # Tạo PNG mẫu để gửi (header 8 byte + IHDR 32x32 + IDAT 1 pixel trắng).
    out = ROOT / "runtime" / "smoke_telegram.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        try:
            import qrcode  # type: ignore[import-untyped]

            qrcode.make("upi://test/smoke?ts=" + str(int(time.time()))).save(out)
        except ModuleNotFoundError:
            # Fallback: PNG 1x1 trắng — đủ cho Telegram chấp nhận.
            out.write_bytes(bytes.fromhex(
                "89504e470d0a1a0a0000000d49484452000000010000000108020000"
                "00907753de0000000c4944415478da6364f80f00000300010000"
                "31350000000049454e44ae426082"
            ))
    return out


async def _run() -> int:
    bot_token = _need("TG_BOT_TOKEN")
    chat_id = _need("TG_CHAT_ID")
    amount = int(os.environ.get("TG_AMOUNT", "2000"))
    expires_in = int(os.environ.get("TG_EXPIRES_IN", "600"))
    qr_path = _ensure_qr_png()

    print(f"[1/3] qr file = {qr_path} ({qr_path.stat().st_size} bytes)", flush=True)

    # Import notifier
    from web.telegram_notifier import TelegramNotifier, TelegramNotifyError

    notifier = TelegramNotifier()
    notifier.set_enabled(True)
    notifier.set_credentials(bot_token, chat_id)
    print(f"[2/3] notifier configured={notifier.configured} enabled={notifier.enabled}", flush=True)

    # Gửi test message trước (verify token + chat_id)
    try:
        await notifier.send_test()
        print("[PASS] TC-01 — sendMessage test OK", flush=True)
    except TelegramNotifyError as exc:
        print(f"[FAIL] TC-01 — sendMessage test FAIL: {exc}", flush=True)
        return 1

    # Gửi QR + combo (multipart)
    try:
        sent = await notifier.notify_upi_qr(
            email="smoke_test@example.com",
            password="GPT#smoke_test",
            secret="JBSWY3DPEHPK3PXP",
            amount=amount,
            qr_path=str(qr_path),
            qr_expires_at=int(time.time()) + expires_in,
            checkout_session="cs_test_smoke",
            log=lambda m: print(f"  log: {m}", flush=True),
        )
        if sent:
            print("[PASS] TC-02 — sendPhoto + reply combo OK", flush=True)
        else:
            print("[FAIL] TC-02 — notify_upi_qr trả False (skip path)", flush=True)
            return 1
    except TelegramNotifyError as exc:
        print(f"[FAIL] TC-02 — notify_upi_qr fail: {exc}", flush=True)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] TC-02 — exception bất ngờ: {type(exc).__name__}: {exc}", flush=True)
        return 1

    print("[3/3] OK — kiểm tra Telegram đã nhận tin.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
