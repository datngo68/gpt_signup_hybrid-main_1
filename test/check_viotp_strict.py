"""Offline regression checks for strict ViOTP rentals and redaction."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_auth import browser
from codex_auth.errors import PhoneVerificationRequired
from codex_auth.sms import SmsError, ViotpProvider


def provider(responses, service_id="1234"):
    item = ViotpProvider(api_key="private-key", service_id=service_id)
    calls = []

    def fake_get(url, timeout=15.0):
        calls.append(url)
        return responses.pop(0)

    return item, calls, fake_get


def check_strict_network_and_service_catalog():
    responses = [
        {"status_code": 200, "data": [{"id": 1234, "name": "OpenAI | ChatGPT", "price": 1000}]},
        {"status_code": 400, "message": "Hết số"},
    ]
    item, calls, fake_get = provider(responses)
    with patch("codex_auth.sms._http_get_json", side_effect=fake_get):
        try:
            item.request_phone()
        except SmsError:
            pass
        else:
            raise AssertionError("out-of-stock rental must fail without fallback")
    rental_calls = [url for url in calls if "/request/getv2" in url]
    assert len(rental_calls) == 1
    assert "network=VINAPHONE" in rental_calls[0]
    assert "private-key" not in str(SmsError("safe"))


def check_catalog_mismatch_blocks_before_rental():
    responses = [{"status_code": 200, "data": [{"id": 999, "name": "OpenAI | ChatGPT"}]}]
    item, calls, fake_get = provider(responses)
    with patch("codex_auth.sms._http_get_json", side_effect=fake_get):
        try:
            item.request_phone()
        except SmsError as exc:
            assert "999" not in str(exc)
        else:
            raise AssertionError("catalog mismatch must fail")
    assert not any("/request/getv2" in url for url in calls)


def check_response_carrier_and_prefix_policy():
    accepted = [
        {"status_code": 200, "data": [{"id": 1234, "name": "OpenAI | ChatGPT"}]},
        {"status_code": 200, "data": {"request_id": "secret-id", "phone_number": "0381234567", "network": "VINAPHONE"}},
    ]
    item, _, fake_get = provider(accepted)
    with patch("codex_auth.sms._http_get_json", side_effect=fake_get):
        assert item.request_phone()[0] == "secret-id"  # MNP-safe: explicit carrier wins.

    mnp_safe = [
        {"status_code": 200, "data": [{"id": 1234, "name": "OpenAI | ChatGPT"}]},
        {"status_code": 200, "data": {"request_id": "secret-id", "phone_number": "0381234567"}},
    ]
    item, _, fake_get = provider(mnp_safe)
    with patch("codex_auth.sms._http_get_json", side_effect=fake_get):
        assert item.request_phone()[0] == "secret-id"  # Do not reject solely by prefix.

    wrong_carrier = [
        {"status_code": 200, "data": [{"id": 1234, "name": "OpenAI | ChatGPT"}]},
        {"status_code": 200, "data": {"request_id": "secret-id", "phone_number": "0912345678", "network": "VIETTEL"}},
    ]
    item, _, fake_get = provider(wrong_carrier)
    with patch("codex_auth.sms._http_get_json", side_effect=fake_get):
        try:
            item.request_phone()
        except SmsError as exc:
            text = str(exc)
            assert "0912345678" not in text and "secret-id" not in text
        else:
            raise AssertionError("explicit non-Vina carrier must fail")


async def check_browser_one_rental_and_redaction():
    sms = Mock()
    sms.request_phone.return_value = ("secret-id", "0912345678")
    sms.cancel.return_value = True
    page = Mock()
    page.url = "https://auth.openai.com/add-phone"
    page.keyboard.press = AsyncMock()
    page.inner_text = AsyncMock(return_value="Unsupported phone number")
    field = Mock(click=AsyncMock(), type=AsyncMock())
    logs = []
    with patch.object(browser, "_first_visible", AsyncMock(side_effect=[None, field, None])), patch.object(
        browser, "_click_first", AsyncMock(return_value=True)
    ), patch.object(browser.asyncio, "sleep", AsyncMock()):
        try:
            await browser.handle_phone_verification(page, sms, log=logs.append)
        except PhoneVerificationRequired:
            pass
        else:
            raise AssertionError("OpenAI rejection must stop")
    assert sms.request_phone.call_count == 1
    output = "\n".join(logs)
    assert "secret-id" not in output and "0912345678" not in output and "private-key" not in output


async def check_otp_errors_redact_request_id():
    item = ViotpProvider(api_key="private-key", service_id="1234")
    expired = {"status_code": 200, "data": {"Status": 2}}
    with patch("codex_auth.sms._http_get_json", return_value=expired):
        try:
            await item.get_otp("secret-request-id", timeout=0.1, poll_interval=0)
        except SmsError as exc:
            assert "secret-request-id" not in str(exc)
        else:
            raise AssertionError("expired rental must fail")


if __name__ == "__main__":
    check_strict_network_and_service_catalog()
    check_catalog_mismatch_blocks_before_rental()
    check_response_carrier_and_prefix_policy()
    asyncio.run(check_browser_one_rental_and_redaction())
    asyncio.run(check_otp_errors_redact_request_id())
    print("PASS strict ViOTP policy")
