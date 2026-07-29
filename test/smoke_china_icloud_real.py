"""Smoke test ChinaICloudProvider với 2 account thật do user cung cấp.

Run: python3 test/smoke_china_icloud_real.py

Pipeline:
    1. Parse 2 line `email----url`
    2. Fetch raw HTML từng URL → log status, headers, body length, snippet
    3. Phát hiện empty marker / extract OTP nếu có
    4. Run poll_otp(timeout=20s) song song cho cả 2 (mailbox có thể trống)
    5. Run poll_all_codes
    6. Test parse line đa dòng (textarea 2 dòng)
    7. Test signup._build_mail_provider dispatch
    8. Test mail_modes registry
"""
from __future__ import annotations

import asyncio
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from mail_providers import (  # noqa: E402
    ChinaICloudProvider,
    _BROWSER_UA,
    _extract_otp,
    build_provider_china_icloud,
)

# Output dir cho HTML dump
OUT_DIR = ROOT / "runtime" / "china_icloud_smoke"


ACCOUNTS = [
    (
        "maroons_sniffle.1l+rex4m6xxwmjeqlqoy@icloud.com"
        "----"
        "http://icloudapi.xyz/show/"
        "ARAfBBUTFgUeAANUSkdWIxsGGAQQHUMaHAhZHB0MDkgUHg0cSAsVFBlGFxoGCg==/"
        "maroons_sniffle.1l%2Brex4m6xxwmjeqlqoy@icloud.com"
    ),
    (
        "fuller_elitist_5z+r3d5ru3ouwghuwril@icloud.com"
        "----"
        "http://icloudapi.xyz/show/"
        "ARAfBBUTFgUeAANUSkdWIxsGGAQQHUMaHAhZHB0MDkgUHg0cSAsVFBlGFxoGCg==/"
        "fuller_elitist_5z%2Br3d5ru3ouwghuwril@icloud.com"
    ),
]


def _hr() -> None:
    print("─" * 78, flush=True)


def _stage(name: str) -> None:
    _hr()
    print(f"=== {name}", flush=True)


def _safe_filename(email: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", email.lower())[:80]


def stage_parse() -> list[tuple[str, str]]:
    _stage("STAGE 1 — parse 2 line")
    parsed: list[tuple[str, str]] = []
    for i, line in enumerate(ACCOUNTS, 1):
        try:
            email, url = ChinaICloudProvider.parse_line(line)
            parsed.append((email, url))
            print(f"[PASS] [{i}/{len(ACCOUNTS)}] parse :: email={email}", flush=True)
            print(f"        url={url}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] [{i}/{len(ACCOUNTS)}] parse :: {type(exc).__name__}: {exc}", flush=True)
    return parsed


def stage_fetch_raw(parsed: list[tuple[str, str]]) -> list[dict]:
    _stage("STAGE 2 — fetch raw HTML từng URL")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    for i, (email, url) in enumerate(parsed, 1):
        print(f"[GET] [{i}/{len(parsed)}] {email}", flush=True)
        try:
            t0 = time.monotonic()
            resp = httpx.get(
                url,
                headers={"User-Agent": _BROWSER_UA, "Accept": "*/*"},
                timeout=20.0,
                follow_redirects=True,
            )
            elapsed = time.monotonic() - t0
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {type(exc).__name__}: {exc}", flush=True)
            results.append({"email": email, "url": url, "error": str(exc)})
            continue

        body = resp.text or ""
        ct = resp.headers.get("content-type", "?")
        print(
            f"  · status={resp.status_code} ct={ct} bytes={len(resp.content)} "
            f"text_len={len(body)} elapsed={elapsed:.2f}s",
            flush=True,
        )
        # Save raw body
        out_file = OUT_DIR / f"{_safe_filename(email)}_{int(time.time())}.html"
        out_file.write_text(body, encoding="utf-8")
        print(f"  · raw saved → {out_file.relative_to(ROOT)}", flush=True)

        snippet = body[:400].replace("\n", " ")
        print(f"  · snippet: {snippet!r}", flush=True)

        is_empty = ChinaICloudProvider._is_empty_mailbox(body)
        print(f"  · is_empty_mailbox(): {is_empty}", flush=True)

        # Extract OTP nếu có
        if not is_empty:
            code = _extract_otp("", body)
            if code:
                print(f"  · _extract_otp → {code}", flush=True)
            else:
                print("  · _extract_otp → None (page có nội dung nhưng chưa có 6 số)", flush=True)

        results.append({
            "email": email,
            "url": url,
            "status": resp.status_code,
            "content_type": ct,
            "body": body,
            "is_empty": is_empty,
            "elapsed": elapsed,
        })
    return results


async def stage_poll_otp(parsed: list[tuple[str, str]], timeout_s: float = 20.0) -> None:
    _stage(f"STAGE 3 — poll_otp (timeout {timeout_s:.0f}s, song song)")

    async def _one(idx: int, email: str, url: str) -> None:
        provider = build_provider_china_icloud(email=email, api_url=url)
        started = datetime.now(timezone.utc)
        prefix = f"[{idx}/{len(parsed)}] {email[:40]}"
        try:
            t0 = time.monotonic()
            code = await provider.poll_otp(
                recipient=email,
                started_at=started,
                timeout_seconds=timeout_s,
                poll_interval_seconds=5.0,
                log=lambda m, p=prefix: print(f"  · {p} :: {m}", flush=True),
            )
            elapsed = time.monotonic() - t0
            print(f"[PASS] {prefix} :: code={code} ({elapsed:.1f}s)", flush=True)
        except TimeoutError as exc:
            elapsed = time.monotonic() - t0
            print(
                f"[INFO] {prefix} :: timeout {elapsed:.1f}s "
                f"(mailbox trống — kỳ vọng nếu chưa request OTP): {exc}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {prefix} :: {type(exc).__name__}: {exc}", flush=True)

    await asyncio.gather(*[_one(i, e, u) for i, (e, u) in enumerate(parsed, 1)])


async def stage_poll_all_codes(parsed: list[tuple[str, str]]) -> None:
    _stage("STAGE 4 — poll_all_codes (1 lần fetch, list mọi 6 số trên page)")

    async def _one(idx: int, email: str, url: str) -> None:
        provider = build_provider_china_icloud(email=email, api_url=url)
        started = datetime.now(timezone.utc)
        prefix = f"[{idx}/{len(parsed)}] {email[:40]}"
        try:
            codes = await provider.poll_all_codes(
                recipient=email,
                started_at=started,
                log=lambda m: None,
            )
            if codes:
                print(f"[PASS] {prefix} :: codes={codes}", flush=True)
            else:
                print(f"[INFO] {prefix} :: codes=[] (mailbox trống)", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {prefix} :: {type(exc).__name__}: {exc}", flush=True)

    await asyncio.gather(*[_one(i, e, u) for i, (e, u) in enumerate(parsed, 1)])


def stage_textarea_multi() -> None:
    _stage("STAGE 5 — parse 2 line giả lập textarea")
    blob = "\n".join(ACCOUNTS)
    parsed_count = 0
    failed_count = 0
    for i, line in enumerate(blob.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            email, _ = ChinaICloudProvider.parse_line(line)
            parsed_count += 1
            print(f"[PASS] [{i}] textarea_line :: {email}", flush=True)
        except Exception as exc:  # noqa: BLE001
            failed_count += 1
            print(f"[FAIL] [{i}] textarea_line :: {type(exc).__name__}: {exc}", flush=True)
    print(f"  · parsed_count={parsed_count} failed_count={failed_count}", flush=True)


def stage_signup_dispatch(parsed: list[tuple[str, str]]) -> None:
    _stage("STAGE 6 — signup._build_mail_provider dispatch")
    try:
        from config import load_settings  # noqa: PLC0415
        from models import SignupRequest  # noqa: PLC0415
        from signup import _build_mail_provider  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] import: {type(exc).__name__}: {exc}", flush=True)
        return

    settings = load_settings()
    for i, (email, url) in enumerate(parsed, 1):
        try:
            req = SignupRequest(
                email=email,
                mail_provider="china_icloud",
                china_icloud_url=url,
                password="dummypass1234",
            )
            provider = _build_mail_provider(req, settings=settings)
            ok = isinstance(provider, ChinaICloudProvider) and provider.email == email.lower()
            if ok:
                print(f"[PASS] [{i}/{len(parsed)}] dispatch :: {email}", flush=True)
            else:
                print(
                    f"[FAIL] [{i}/{len(parsed)}] dispatch :: provider={type(provider).__name__} "
                    f"email_set={getattr(provider, 'email', None)}",
                    flush=True,
                )
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] [{i}/{len(parsed)}] dispatch :: {type(exc).__name__}: {exc}", flush=True)


def stage_registry() -> None:
    _stage("STAGE 7 — web/mail_modes.py registry build_request 2 acc")
    try:
        from web.mail_modes import get_spec, serialize_for_api  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] import: {type(exc).__name__}: {exc}", flush=True)
        return
    api_modes = serialize_for_api()
    api_ids = [m["id"] for m in api_modes]
    print(f"  · /api/mail-modes ids: {api_ids}", flush=True)
    if "china_icloud" not in api_ids:
        print("[FAIL] china_icloud không trong serialize_for_api()", flush=True)
        return
    spec = get_spec("china_icloud")
    for i, line in enumerate(ACCOUNTS, 1):
        try:
            parsed = spec.parse_line(line)
            req = spec.build_request(parsed)
            ok = (
                req.mail_provider == "china_icloud"
                and req.china_icloud_url
                and req.china_icloud_url.endswith(req.email.replace("+", "%2B"))
            )
            if ok:
                print(
                    f"[PASS] [{i}/{len(ACCOUNTS)}] spec.build_request :: "
                    f"email={req.email} otp_to={req.otp_timeout_seconds}s",
                    flush=True,
                )
            else:
                print(
                    f"[FAIL] [{i}/{len(ACCOUNTS)}] build_request mismatch :: "
                    f"provider={req.mail_provider} url={req.china_icloud_url}",
                    flush=True,
                )
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] [{i}/{len(ACCOUNTS)}] {type(exc).__name__}: {exc}", flush=True)


def stage_otp_regex_samples() -> None:
    """Verify regex bắt được OTP trong các format mail OpenAI thực tế."""
    _stage("STAGE 8 — regex sample mails OpenAI")
    samples = [
        ("plain text 6 số có context",
         "<p>OpenAI verification code: 472938. Valid for 10 minutes.</p>", "472938"),
        ("HTML có nhiều tag bao quanh",
         "<table><tr><td><h1 style='color:#000'>481275</h1></td></tr></table>", "481275"),
        ("OTP nằm sau từ khoá login code",
         "Your login code is 935610 — do not share it.", "935610"),
        ("OTP có dấu space trong HTML",
         "<div>Verification\ncode: <strong> 105872 </strong></div>", "105872"),
        ("không OTP",
         "<h1>错误</h1><p>No email found for recipient</p>", None),
    ]
    total = len(samples)
    ok = 0
    for i, (desc, body, expected) in enumerate(samples, 1):
        got = _extract_otp("", body)
        if got == expected:
            ok += 1
            print(f"[PASS] [{i}/{total}] {desc} :: got={got}", flush=True)
        else:
            print(f"[FAIL] [{i}/{total}] {desc} :: expected={expected} got={got}", flush=True)
    print(f"  · regex sample: {ok}/{total} OK", flush=True)


async def main() -> int:
    print(f"=== China iCloud smoke real (2 acc) ===  start {datetime.now().isoformat()}", flush=True)

    # 1. parse
    parsed = stage_parse()
    if len(parsed) != len(ACCOUNTS):
        print("[ABORT] parse fail — không tiếp", flush=True)
        return 1

    # 2. fetch raw
    fetched = stage_fetch_raw(parsed)

    # 3. poll_otp 20s mỗi acc, song song
    await stage_poll_otp(parsed, timeout_s=20.0)

    # 4. poll_all_codes
    await stage_poll_all_codes(parsed)

    # 5. textarea multi
    stage_textarea_multi()

    # 6. signup dispatch
    stage_signup_dispatch(parsed)

    # 7. registry
    stage_registry()

    # 8. regex
    stage_otp_regex_samples()

    _hr()
    has_otp_now = any(
        not f.get("is_empty", True) and _extract_otp("", f.get("body", "")) for f in fetched
    )
    if has_otp_now:
        print("[INFO] có OTP đang nằm sẵn trên page → provider lấy được trong stage 3.", flush=True)
    else:
        print(
            "[INFO] cả 2 mailbox đang trống → provider không có OTP để extract. "
            "Để verify end-to-end, request OTP thật trong web UI khi chạy job.",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
