"""Verify IcloudV3Provider: parse line, build, smoke fetch live, OTP extract.

Run: python3 test/check_icloud_v3.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mail_providers import (  # noqa: E402
    IcloudV3ParseError,
    IcloudV3Provider,
    _extract_otp,
    build_provider_icloud_v3,
)
from models import SignupRequest  # noqa: E402
from web.mail_modes import (  # noqa: E402
    ICLOUD_V3_MODE,
    MailModeParseError,
    get_registry,
    get_spec,
    serialize_for_api,
)


# Sample input user cung cấp — dùng 2 dòng đầu cho parse + smoke fetch
SAMPLES: list[tuple[str, str]] = [
    (
        "petunia-boar-3d+hblx3n@icloud.com",
        "https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/mELlXOnuhwiUDHjc7IFc-fllJLoCeuAv/data",
    ),
    (
        "pasties.sateen.7c+im3zd@icloud.com",
        "https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/g5hvaOkVuQq_THa1GONjRmzt19HaGDff/data",
    ),
]


PASS = "PASS"
FAIL = "FAIL"


def _label(idx: int, total: int, name: str) -> str:
    return f"[{idx:02d}/{total:02d}] {name}"


def tc01_parse_valid(idx: int, total: int) -> bool:
    name = _label(idx, total, "TC-01 parse_valid")
    email, url = SAMPLES[0]
    line = f"{email}|{url}"
    try:
        e, u = IcloudV3Provider.parse_line(line)
    except Exception as exc:  # noqa: BLE001
        print(f"{FAIL} {name}: unexpected {type(exc).__name__}: {exc}")
        return False
    ok = e == email and u == url
    print(f"{PASS if ok else FAIL} {name}: ({e!r}, {u[:60]!r}...)")
    return ok


def tc02_parse_missing_separator(idx: int, total: int) -> bool:
    name = _label(idx, total, "TC-02 parse_missing_separator")
    try:
        IcloudV3Provider.parse_line("petunia@icloud.com")
    except IcloudV3ParseError:
        print(f"{PASS} {name}: raised IcloudV3ParseError as expected")
        return True
    print(f"{FAIL} {name}: did NOT raise")
    return False


def tc03_parse_bad_url_scheme(idx: int, total: int) -> bool:
    name = _label(idx, total, "TC-03 parse_bad_url_scheme")
    try:
        IcloudV3Provider.parse_line("petunia@icloud.com|ftp://x/readmail/abc/data")
    except IcloudV3ParseError:
        print(f"{PASS} {name}: raised IcloudV3ParseError as expected")
        return True
    print(f"{FAIL} {name}: did NOT raise")
    return False


def tc04_parse_url_missing_marker(idx: int, total: int) -> bool:
    name = _label(idx, total, "TC-04 parse_url_missing_marker")
    try:
        IcloudV3Provider.parse_line("petunia@icloud.com|https://example.com/other/abc/data")
    except IcloudV3ParseError:
        print(f"{PASS} {name}: raised IcloudV3ParseError (no /readmail/)")
        return True
    print(f"{FAIL} {name}: did NOT raise")
    return False


def tc05_parse_empty_email(idx: int, total: int) -> bool:
    name = _label(idx, total, "TC-05 parse_empty_email")
    try:
        IcloudV3Provider.parse_line("|https://x/readmail/abc/data")
    except IcloudV3ParseError:
        print(f"{PASS} {name}: raised IcloudV3ParseError")
        return True
    print(f"{FAIL} {name}: did NOT raise")
    return False


def tc06_build_provider(idx: int, total: int) -> bool:
    name = _label(idx, total, "TC-06 build_provider")
    email, url = SAMPLES[0]
    try:
        provider = build_provider_icloud_v3(email=email, api_url=url)
    except Exception as exc:  # noqa: BLE001
        print(f"{FAIL} {name}: build raised {type(exc).__name__}: {exc}")
        return False
    ok = (
        isinstance(provider, IcloudV3Provider)
        and provider.email == email.lower()
        and provider.api_url == url
        and provider.proxy is None
    )
    print(f"{PASS if ok else FAIL} {name}: email={provider.email} url_len={len(provider.api_url)}")
    return ok


def tc07_build_provider_rejects_bad_url(idx: int, total: int) -> bool:
    name = _label(idx, total, "TC-07 build_provider_rejects_bad_url")
    try:
        build_provider_icloud_v3(
            email="petunia@icloud.com",
            api_url="https://x.com/not-readmail/abc/data",
        )
    except ValueError:
        print(f"{PASS} {name}: raised ValueError")
        return True
    print(f"{FAIL} {name}: did NOT raise")
    return False


def tc08_extract_otp_from_known_html(idx: int, total: int) -> bool:
    """HTML thật từ check_icloud_v3_fetch.py: 'Mã OTP CỦA BẠN LÀ 1312312'.

    Lưu ý: 1312312 = 7 chữ số, regex chỉ bắt 6 chữ số → check rằng
    extract code 6 chữ số đầu/cuối. Trong chuỗi này không có 6-số chuẩn
    nên test với mail OpenAI thực tế (subject contains 'verification code').
    """
    name = _label(idx, total, "TC-08 extract_otp_from_openai_style")
    subject = "Your ChatGPT verification code"
    body = (
        "<html><body>"
        "Please verify your account. Your code is <b>843219</b>."
        "</body></html>"
    )
    code = _extract_otp(subject, body)
    ok = code == "843219"
    print(f"{PASS if ok else FAIL} {name}: code={code!r}")
    return ok


def tc09_normalize_dict_messages(idx: int, total: int) -> bool:
    name = _label(idx, total, "TC-09 _normalize_dict_messages")
    payload = {"email": "a@b", "messages": [{"id": "x"}, {"id": "y"}], "logs": []}
    msgs = IcloudV3Provider._normalize(payload)
    ok = isinstance(msgs, list) and len(msgs) == 2 and msgs[0]["id"] == "x"
    print(f"{PASS if ok else FAIL} {name}: len={len(msgs)}")
    return ok


def tc10_normalize_empty(idx: int, total: int) -> bool:
    name = _label(idx, total, "TC-10 _normalize_empty_messages")
    payload = {"email": "a@b", "messages": []}
    msgs = IcloudV3Provider._normalize(payload)
    ok = msgs == []
    print(f"{PASS if ok else FAIL} {name}: len={len(msgs)}")
    return ok


def tc11_smoke_live_fetch(idx: int, total: int) -> bool:
    """Smoke test: gọi API live trong vòng 8s, xác nhận không HTTP error.

    Mail box có thể đã có code (đã test mailbox 1 có 'Mã OTP CỦA BẠN LÀ
    1312312' — không match regex 6 số) hoặc trống. KHÔNG fail nếu không
    bắt được OTP, CHỈ fail nếu raise non-Timeout exception.
    """
    name = _label(idx, total, "TC-11 smoke_live_fetch_8s")
    email, url = SAMPLES[0]
    provider = build_provider_icloud_v3(email=email, api_url=url)
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()

    async def _runner() -> tuple[str, str]:
        try:
            code = await provider.poll_otp(
                recipient=email,
                started_at=started,
                timeout_seconds=8.0,
                poll_interval_seconds=2.0,
                log=lambda m: None,
            )
            return ("found", code)
        except TimeoutError:
            return ("timeout", "")
        except Exception as exc:  # noqa: BLE001
            return ("error", f"{type(exc).__name__}: {exc}")

    status, payload = asyncio.run(_runner())
    elapsed = time.monotonic() - t0
    if status == "error":
        print(f"{FAIL} {name}: unexpected error after {elapsed:.1f}s: {payload}")
        return False
    print(f"{PASS} {name}: {status}={payload!r} elapsed={elapsed:.1f}s")
    return True


def tc12_poll_all_codes(idx: int, total: int) -> bool:
    """poll_all_codes return list (có thể rỗng), không raise."""
    name = _label(idx, total, "TC-12 poll_all_codes_returns_list")
    email, url = SAMPLES[1]  # mailbox 2 trống tại thời điểm fetch
    provider = build_provider_icloud_v3(email=email, api_url=url)

    async def _runner() -> object:
        return await provider.poll_all_codes(
            recipient=email,
            started_at=datetime.now(timezone.utc),
            log=lambda m: None,
        )

    result = asyncio.run(_runner())
    ok = isinstance(result, list)
    print(f"{PASS if ok else FAIL} {name}: type={type(result).__name__} len={len(result) if ok else 'N/A'}")
    return ok


def tc13_mail_mode_spec_registered(idx: int, total: int) -> bool:
    name = _label(idx, total, "TC-13 mail_mode_spec_registered")
    try:
        spec = get_spec("icloud_v3")
    except KeyError as exc:
        print(f"{FAIL} {name}: get_spec('icloud_v3') raised KeyError: {exc}")
        return False
    ok = spec is ICLOUD_V3_MODE and spec.id == "icloud_v3"
    print(f"{PASS if ok else FAIL} {name}: spec.id={spec.id} label={spec.label!r}")
    return ok


def tc14_mail_mode_is_first_in_registry(idx: int, total: int) -> bool:
    """icloud_v3 phải là item đầu tiên trong registry (UI dropdown default)."""
    name = _label(idx, total, "TC-14 mail_mode_is_first")
    registry = get_registry()
    first_id = next(iter(registry))
    ok = first_id == "icloud_v3"
    print(f"{PASS if ok else FAIL} {name}: first={first_id!r} (expect 'icloud_v3')")
    return ok


def tc15_mail_mode_serialize_for_api(idx: int, total: int) -> bool:
    """Serialize phải có icloud_v3 và là item đầu."""
    name = _label(idx, total, "TC-15 mail_mode_serialize_api")
    items = serialize_for_api()
    ok = (
        isinstance(items, list)
        and len(items) > 0
        and items[0]["id"] == "icloud_v3"
        and items[0]["label"]
        and items[0]["input_placeholder"]
    )
    print(f"{PASS if ok else FAIL} {name}: items[0].id={items[0]['id']!r}")
    return ok


def tc16_signup_request_default_provider(idx: int, total: int) -> bool:
    """SignupRequest default mail_provider phải là 'icloud_v3'."""
    name = _label(idx, total, "TC-16 signup_request_default")
    req = SignupRequest(email="x@icloud.com")
    ok = req.mail_provider == "icloud_v3"
    print(f"{PASS if ok else FAIL} {name}: default={req.mail_provider!r}")
    return ok


def tc17_signup_request_accept_icloud_v3_url(idx: int, total: int) -> bool:
    """SignupRequest chấp nhận icloud_v3_url field."""
    name = _label(idx, total, "TC-17 signup_request_icloud_v3_url")
    email, url = SAMPLES[0]
    req = SignupRequest(
        email=email,
        mail_provider="icloud_v3",
        icloud_v3_url=url,
    )
    ok = req.icloud_v3_url == url and req.mail_provider == "icloud_v3"
    print(f"{PASS if ok else FAIL} {name}: url_set={bool(req.icloud_v3_url)}")
    return ok


def tc18_signup_request_rejects_unknown_provider(idx: int, total: int) -> bool:
    """Regex pattern phải reject provider lạ."""
    name = _label(idx, total, "TC-18 signup_request_regex_rejects")
    try:
        SignupRequest(email="x@icloud.com", mail_provider="bogus_provider")
    except Exception as exc:  # ValidationError
        print(f"{PASS} {name}: rejected ({type(exc).__name__})")
        return True
    print(f"{FAIL} {name}: did NOT reject 'bogus_provider'")
    return False


def tc19_signup_dispatch(idx: int, total: int) -> bool:
    """signup._build_mail_provider dispatch correct class cho icloud_v3."""
    name = _label(idx, total, "TC-19 signup_dispatch")
    from config import load_settings
    from signup import _build_mail_provider

    email, url = SAMPLES[0]
    req = SignupRequest(
        email=email,
        mail_provider="icloud_v3",
        icloud_v3_url=url,
    )
    settings = load_settings()
    try:
        provider = _build_mail_provider(req, settings=settings, combo_repo=None)
    except Exception as exc:  # noqa: BLE001
        print(f"{FAIL} {name}: dispatch raised {type(exc).__name__}: {exc}")
        return False
    ok = isinstance(provider, IcloudV3Provider) and provider.email == email
    print(f"{PASS if ok else FAIL} {name}: type={type(provider).__name__} email={provider.email}")
    return ok


def tc20_signup_dispatch_missing_url(idx: int, total: int) -> bool:
    """Dispatch phải báo lỗi khi icloud_v3_url None."""
    name = _label(idx, total, "TC-20 signup_dispatch_missing_url")
    from config import load_settings
    from signup import _build_mail_provider

    req = SignupRequest(
        email="x@icloud.com",
        mail_provider="icloud_v3",
        icloud_v3_url=None,
    )
    settings = load_settings()
    try:
        _build_mail_provider(req, settings=settings, combo_repo=None)
    except ValueError as exc:
        if "icloud_v3_url" in str(exc):
            print(f"{PASS} {name}: raised ValueError mentioning icloud_v3_url")
            return True
        print(f"{FAIL} {name}: wrong msg: {exc}")
        return False
    print(f"{FAIL} {name}: did NOT raise")
    return False


def tc21_mail_mode_build_request_round_trip(idx: int, total: int) -> bool:
    """Round-trip: parse_line → build_request → SignupRequest có mail_provider/url."""
    name = _label(idx, total, "TC-21 build_request_round_trip")
    email, url = SAMPLES[0]
    line = f"{email}|{url}"
    spec = get_spec("icloud_v3")
    try:
        parsed = spec.parse_line(line)
        req = spec.build_request(parsed, headless=True)
    except (MailModeParseError, Exception) as exc:  # noqa: BLE001
        print(f"{FAIL} {name}: raised {type(exc).__name__}: {exc}")
        return False
    ok = (
        req.mail_provider == "icloud_v3"
        and req.icloud_v3_url == url
        and req.email == email
        and req.otp_timeout_seconds >= 60.0
    )
    print(f"{PASS if ok else FAIL} {name}: req.email={req.email} timeout={req.otp_timeout_seconds}s")
    return ok


TESTS = [
    tc01_parse_valid,
    tc02_parse_missing_separator,
    tc03_parse_bad_url_scheme,
    tc04_parse_url_missing_marker,
    tc05_parse_empty_email,
    tc06_build_provider,
    tc07_build_provider_rejects_bad_url,
    tc08_extract_otp_from_known_html,
    tc09_normalize_dict_messages,
    tc10_normalize_empty,
    tc11_smoke_live_fetch,
    tc12_poll_all_codes,
    tc13_mail_mode_spec_registered,
    tc14_mail_mode_is_first_in_registry,
    tc15_mail_mode_serialize_for_api,
    tc16_signup_request_default_provider,
    tc17_signup_request_accept_icloud_v3_url,
    tc18_signup_request_rejects_unknown_provider,
    tc19_signup_dispatch,
    tc20_signup_dispatch_missing_url,
    tc21_mail_mode_build_request_round_trip,
]


def main() -> int:
    total = len(TESTS)
    print(f"=== check_icloud_v3 ({total} test cases) ===")
    failed = 0
    for i, fn in enumerate(TESTS, start=1):
        try:
            ok = fn(i, total)
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL [{i:02d}/{total:02d}] {fn.__name__}: UNCAUGHT {type(exc).__name__}: {exc}")
            ok = False
        if not ok:
            failed += 1
    print(f"=== result: {total - failed}/{total} passed, {failed} failed ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
