"""Verify ChinaICloudProvider: parse line, build, smoke fetch live.

Run: python3 test/check_china_icloud_provider.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Cho phép import từ root project khi chạy `python3 test/<file>.py`
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mail_providers import (  # noqa: E402
    ChinaICloudParseError,
    ChinaICloudProvider,
    build_provider_china_icloud,
)

LINE = (
    "brier86.toenail+ran8r616yfp5dpr0g@icloud.com"
    "----"
    "http://icloudapi.xyz/show/"
    "ARAfBBUTFgUeAANUSkdWIxsGGAQQHUMaHAhZHB0MDkgUHg0cSAsVFBlGFxoGCg==/"
    "brier86.toenail%2Bran8r616yfp5dpr0g@icloud.com"
)


def _label(idx: int, total: int, name: str) -> str:
    return f"[{idx}/{total}] {name}"


def tc01_parse_valid(idx: int, total: int) -> bool:
    name = _label(idx, total, "TC-01 parse_valid")
    try:
        email, url = ChinaICloudProvider.parse_line(LINE)
        ok = (
            email == "brier86.toenail+ran8r616yfp5dpr0g@icloud.com"
            and url.startswith("http://icloudapi.xyz/show/")
            and url.endswith("@icloud.com")
        )
        if ok:
            print(f"[PASS] {name} :: email={email} url_len={len(url)}", flush=True)
            return True
        print(f"[FAIL] {name} :: email={email!r} url={url!r}", flush=True)
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {name} :: {type(exc).__name__}: {exc}", flush=True)
        return False


def tc02_parse_no_separator(idx: int, total: int) -> bool:
    name = _label(idx, total, "TC-02 parse_no_separator")
    try:
        ChinaICloudProvider.parse_line("foo@bar.com")
        print(f"[FAIL] {name} :: expected ChinaICloudParseError, không raise", flush=True)
        return False
    except ChinaICloudParseError as exc:
        print(f"[PASS] {name} :: raised ChinaICloudParseError({exc})", flush=True)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {name} :: wrong exc {type(exc).__name__}: {exc}", flush=True)
        return False


def tc03_parse_bad_url(idx: int, total: int) -> bool:
    name = _label(idx, total, "TC-03 parse_bad_url")
    try:
        ChinaICloudProvider.parse_line("user@icloud.com----not_a_url")
        print(f"[FAIL] {name} :: expected error", flush=True)
        return False
    except ChinaICloudParseError as exc:
        print(f"[PASS] {name} :: raised {exc}", flush=True)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {name} :: wrong exc {type(exc).__name__}: {exc}", flush=True)
        return False


def tc04_parse_bad_email(idx: int, total: int) -> bool:
    name = _label(idx, total, "TC-04 parse_bad_email")
    try:
        ChinaICloudProvider.parse_line("not_email----http://x.y/z")
        print(f"[FAIL] {name} :: expected error", flush=True)
        return False
    except ChinaICloudParseError as exc:
        print(f"[PASS] {name} :: raised {exc}", flush=True)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {name} :: wrong exc {type(exc).__name__}: {exc}", flush=True)
        return False


def tc05_build_factory(idx: int, total: int) -> bool:
    name = _label(idx, total, "TC-05 build_factory")
    try:
        email, url = ChinaICloudProvider.parse_line(LINE)
        provider = build_provider_china_icloud(email=email, api_url=url)
        ok = isinstance(provider, ChinaICloudProvider) and provider.email == email and provider.api_url == url
        if ok:
            print(f"[PASS] {name} :: provider built", flush=True)
            return True
        print(f"[FAIL] {name} :: provider state mismatch", flush=True)
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {name} :: {type(exc).__name__}: {exc}", flush=True)
        return False


def tc06_empty_marker_detect(idx: int, total: int) -> bool:
    name = _label(idx, total, "TC-06 empty_marker_detect")
    body = "<h1>错误</h1><p>No email found for recipient</p>"
    if ChinaICloudProvider._is_empty_mailbox(body):
        print(f"[PASS] {name} :: marker detect OK", flush=True)
        return True
    print(f"[FAIL] {name} :: marker không detect", flush=True)
    return False


def tc07_otp_extract_html(idx: int, total: int) -> bool:
    """Provider parse OTP từ HTML mail giả lập."""
    name = _label(idx, total, "TC-07 otp_extract_html")
    fake_html = (
        "<html><body><h2>OpenAI</h2>"
        "<p>Your verification code is <b>481275</b></p>"
        "<p>This code will expire in 10 minutes.</p>"
        "</body></html>"
    )
    from mail_providers import _extract_otp
    code = _extract_otp("", fake_html)
    if code == "481275":
        print(f"[PASS] {name} :: extracted {code}", flush=True)
        return True
    print(f"[FAIL] {name} :: got {code!r}", flush=True)
    return False


def tc08_smoke_live_fetch(idx: int, total: int) -> bool:
    """Smoke fetch URL live trong 10s. Mailbox trống → expect TimeoutError."""
    name = _label(idx, total, "TC-08 smoke_live_fetch_10s")
    email, url = ChinaICloudProvider.parse_line(LINE)
    provider = build_provider_china_icloud(email=email, api_url=url)
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()

    async def _run() -> str | None:
        try:
            return await provider.poll_otp(
                recipient=email,
                started_at=started,
                timeout_seconds=10.0,
                poll_interval_seconds=3.0,
                log=lambda m: print(f"  · {m}", flush=True),
            )
        except TimeoutError as exc:
            print(f"  · timeout (expected nếu mailbox trống): {exc}", flush=True)
            return None

    result = asyncio.run(_run())
    elapsed = time.monotonic() - t0
    if result is None:
        print(
            f"[PASS] {name} :: timeout sau {elapsed:.1f}s (mailbox trống — đúng kỳ vọng)",
            flush=True,
        )
        return True
    print(f"[PASS] {name} :: bất ngờ có code {result} ({elapsed:.1f}s)", flush=True)
    return True


def tc09_signup_dispatch(idx: int, total: int) -> bool:
    """Verify signup._build_mail_provider dispatch case china_icloud."""
    name = _label(idx, total, "TC-09 signup_dispatch")
    try:
        from config import load_settings  # noqa: PLC0415
        from models import SignupRequest  # noqa: PLC0415
        from signup import _build_mail_provider  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {name} :: import: {type(exc).__name__}: {exc}", flush=True)
        return False
    try:
        email, url = ChinaICloudProvider.parse_line(LINE)
        request = SignupRequest(
            email=email,
            mail_provider="china_icloud",
            china_icloud_url=url,
            password="dummypass1234",
        )
        settings = load_settings()
        provider = _build_mail_provider(request, settings=settings)
        if isinstance(provider, ChinaICloudProvider):
            print(f"[PASS] {name} :: dispatch -> ChinaICloudProvider", flush=True)
            return True
        print(f"[FAIL] {name} :: dispatch trả {type(provider).__name__}", flush=True)
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {name} :: {type(exc).__name__}: {exc}", flush=True)
        return False


def tc10_mail_modes_registry(idx: int, total: int) -> bool:
    """Verify CHINA_ICLOUD_MODE đăng ký vào registry public."""
    name = _label(idx, total, "TC-10 mail_modes_registry")
    try:
        from web.mail_modes import get_registry, get_spec, serialize_for_api  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {name} :: import: {type(exc).__name__}: {exc}", flush=True)
        return False
    try:
        registry = get_registry()
        if "china_icloud" not in registry:
            print(f"[FAIL] {name} :: china_icloud không trong registry", flush=True)
            return False
        spec = get_spec("china_icloud")
        # parse_line phải callable
        parsed = spec.parse_line(LINE)
        request = spec.build_request(parsed)
        ok = (
            request.mail_provider == "china_icloud"
            and request.china_icloud_url
            and request.china_icloud_url.startswith("http://icloudapi.xyz/")
        )
        # serialize_for_api có chứa china_icloud
        api_modes = {m["id"] for m in serialize_for_api()}
        if not ok or "china_icloud" not in api_modes:
            print(
                f"[FAIL] {name} :: build_request mismatch hoặc thiếu serialize "
                f"(provider={request.mail_provider} url={request.china_icloud_url})",
                flush=True,
            )
            return False
        print(f"[PASS] {name} :: registry + build + serialize OK", flush=True)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {name} :: {type(exc).__name__}: {exc}", flush=True)
        return False


def main() -> int:
    cases = [
        tc01_parse_valid,
        tc02_parse_no_separator,
        tc03_parse_bad_url,
        tc04_parse_bad_email,
        tc05_build_factory,
        tc06_empty_marker_detect,
        tc07_otp_extract_html,
        tc08_smoke_live_fetch,
        tc09_signup_dispatch,
        tc10_mail_modes_registry,
    ]
    total = len(cases)
    passed = 0
    failed = 0
    print(f"=== China iCloud provider checks ({total} TC) ===", flush=True)
    for i, fn in enumerate(cases, 1):
        if fn(i, total):
            passed += 1
        else:
            failed += 1
    print(f"=== Summary: {passed}/{total} PASS, {failed} FAIL ===", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
