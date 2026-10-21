"""One-shot live Codex verification diagnostic with secret-safe output."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_auth.runner import get_codex_auth
from codex_auth.sms import create_sms_provider
from db import get_engine
from db.repositories import SettingsRepository


class OneRentalProvider:
    def __init__(self, provider):
        self.provider = provider
        self.rentals = 0
        self.cancel_attempted = False

    def request_phone(self):
        if self.rentals:
            raise RuntimeError("live diagnostic rental limit reached")
        self.rentals += 1
        return self.provider.request_phone()

    def cancel(self, request_id):
        self.cancel_attempted = True
        return self.provider.cancel(request_id)

    def __getattr__(self, name):
        return getattr(self.provider, name)


def safe_log(message: str) -> None:
    categories = (
        ("màn hình:", "screen-transition"),
        ("nhập email", "email-submitted"),
        ("nhập password", "password-submitted"),
        ("nhập totp", "mfa-submitted"),
        ("phát hiện gate", "phone-required"),
        ("bắt đầu verify", "phone-started"),
        ("đã nhận otp", "phone-otp-received"),
        ("xác minh số điện thoại thành công", "phone-verified"),
        ("bắt được authorization code", "callback-captured"),
        ("đổi code", "token-exchange-started"),
        ("auth.json sẵn sàng", "auth-ready"),
    )
    lower = message.lower()
    for marker, event in categories:
        if marker in lower:
            print(f"LIVE event={event}", flush=True)
            return


async def main() -> int:
    values = dotenv_values(ROOT / ".dev.var")
    raw = values.get("ACC_VER_TEST") or ""
    fields = raw.split("|")
    if len(fields) != 3 or not all(fields):
        print("LIVE outcome=config-missing")
        return 2
    repo = SettingsRepository(get_engine())
    name = repo.get("codex.sms_provider") or "viotp"
    key = repo.get("codex.sms_api_key") or values.get("AIPKEY_SMS")
    service = repo.get("codex.sms_service_id")
    provider = OneRentalProvider(create_sms_provider(name, key, service))
    phone_states = []
    try:
        auth = await get_codex_auth(
            email=fields[0], password=fields[1], secret=fields[2],
            sms_provider=provider, use_api=False, headless=False,
            fetch_api_key=False, overall_timeout=240,
            phone_state=phone_states.append, log=safe_log,
        )
        ok = bool(auth.get("tokens", {}).get("access_token"))
        print(f"LIVE outcome={'success' if ok else 'missing-token'} phone_state={phone_states[-1] if phone_states else 'unknown'} rentals={provider.rentals} cancel_attempted={provider.cancel_attempted}")
        return 0 if ok else 1
    except Exception as exc:
        print(f"LIVE outcome=failed error_type={type(exc).__name__} phone_state={phone_states[-1] if phone_states else 'unknown'} rentals={provider.rentals} cancel_attempted={provider.cancel_attempted}")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
