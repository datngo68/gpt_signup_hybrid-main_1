"""Debug: gọi trực tiếp Worker logs API cho các iCloud email để xem vì sao không lấy được OTP.

Mô phỏng đúng logic WorkerMailProvider:
- Header: Accept json + Browser UA + Authorization Bearer
- URL: {logs_url}?mail=<recipient>
- Normalize payload → list messages
- Với mỗi message: in to/subject/date + thử _extract_otp
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

# Cho phép import module gốc của repo
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from mail_providers import WorkerMailProvider, _extract_otp, _parse_dt  # noqa: E402

LOGS_URL = "https://icloud-email.linhtd.com/logs"
API_KEY = "1481d2b23a24f5c7e9cd4a2cdbdb6af7ed03309f97b195b5"

EMAILS = [
    "broader_bravura.8u+7kzfk7@icloud.com",
]

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


async def probe(client: httpx.AsyncClient, mailbox: str) -> None:
    mailbox = mailbox.strip().lower()
    headers = {
        "Accept": "application/json",
        "User-Agent": _BROWSER_UA,
        "Authorization": f"Bearer {API_KEY}",
    }
    url = f"{LOGS_URL}?mail={quote(mailbox)}"
    print(f"\n=== {mailbox} ===", flush=True)
    print(f"[GET] {url}", flush=True)
    try:
        resp = await client.get(url, headers=headers)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] network error: {type(exc).__name__}: {exc!r}", flush=True)
        return

    print(f"[HTTP] status={resp.status_code} content-type={resp.headers.get('content-type')}", flush=True)
    if resp.status_code != 200:
        print(f"[BODY raw <=500] {resp.text[:500]!r}", flush=True)
        return

    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] không phải JSON: {type(exc).__name__}: {exc}", flush=True)
        print(f"[BODY raw <=500] {resp.text[:500]!r}", flush=True)
        return

    print(f"[JSON] top-level type={type(payload).__name__}", flush=True)
    if isinstance(payload, dict):
        print(f"[JSON] keys={list(payload.keys())}", flush=True)

    messages = WorkerMailProvider._normalize(payload)
    print(f"[NORMALIZE] {len(messages)} message(s)", flush=True)

    if not messages:
        snippet = json.dumps(payload, ensure_ascii=False)[:1500]
        print(f"[RAW <=1500] {snippet}", flush=True)
        return

    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            print(f"  [{i}] không phải dict: {msg!r}", flush=True)
            continue
        msg_to = str(msg.get("to") or "").strip().lower()
        subject = str(msg.get("subject") or "")
        raw_date = msg.get("date") or msg.get("receivedAt") or msg.get("created_at")
        msg_dt = _parse_dt(raw_date)
        body = (
            msg.get("bodyText") or msg.get("text") or msg.get("body")
            or msg.get("htmlBody") or msg.get("content") or msg.get("html") or ""
        )
        code = _extract_otp(subject, str(body))
        print(
            f"  [{i}] to={msg_to!r} match={(not msg_to or msg_to == mailbox)} "
            f"date={raw_date!r} parsed={msg_dt} subject={subject[:80]!r} "
            f"body_len={len(str(body))} otp={code}",
            flush=True,
        )
        print(f"      msg keys={list(msg.keys())}", flush=True)
        print(f"      body_snippet={str(body)[:300]!r}", flush=True)


async def main() -> None:
    started_at = datetime.now(timezone.utc)
    print(f"now(utc)={started_at.isoformat()}", flush=True)
    async with httpx.AsyncClient(verify=True, timeout=20.0, follow_redirects=True) as client:
        for email in EMAILS:
            await probe(client, email)


if __name__ == "__main__":
    asyncio.run(main())
