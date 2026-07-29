#!/usr/bin/env python3
"""Quét nhiều account → phân loại landing của GET /authorize WITH login_hint.

Mục tiêu: tìm account mà fast-path (login_hint) KHÔNG ra `/log-in/password`
(tức rơi vào fallback authorize/continue → nơi phát sinh 409 invalid_state).

Mỗi account chỉ làm bootstrap nhẹ (CSRF + signin + GET authorize) — KHÔNG
sentinel, KHÔNG authorize/continue, KHÔNG password. An toàn.

In realtime mỗi account: [i/N] email_masked → landing_class :: landing_url

Cách dùng:
    .venv/bin/python test/scan_login_landing.py --file runtime/sessions/accounts.txt --limit 30
    .venv/bin/python test/scan_login_landing.py --file ... --start 0 --limit 30 --proxy host:port:user:pass
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    head = local[0] if local else "*"
    return f"{head}***@{domain}"


def _classify(url: str) -> str:
    if "/log-in/password" in url:
        return "password"
    if "/email-verification" in url:
        return "otp"
    if "/log-in-or-create-account" in url:
        return "OR-CREATE(undet)"
    if "/create-account" in url:
        return "CREATE(undet)"
    return f"OTHER(undet):{url[:60]}"


def _normalize_proxy(raw: str | None) -> str | None:
    if not raw:
        return None
    if "://" in raw:
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, pw = parts
        return f"http://{user}:{pw}@{host}:{port}"
    if len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    return raw


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--proxy", default=None)
    args = ap.parse_args()

    from request_phase import _create_session, _step_csrf, _step_auth_url, USER_AGENT
    from user_agent_profile import SEC_CH_UA, SEC_CH_UA_MOBILE, SEC_CH_UA_PLATFORM

    proxy = _normalize_proxy(args.proxy)
    nav = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://chatgpt.com/",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-User": "?1",
        "User-Agent": USER_AGENT,
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": SEC_CH_UA_PLATFORM,
    }

    lines = [l.strip() for l in Path(args.file).read_text().splitlines() if l.strip()]
    chunk = lines[args.start:args.start + args.limit]
    print(f"[scan] total={len(lines)} window=[{args.start}:{args.start+args.limit}] proxy={'set' if proxy else 'DIRECT'}", flush=True)

    counts: dict[str, int] = {}
    for i, line in enumerate(chunk, start=1):
        email = line.split("|", 1)[0].strip()
        masked = _mask_email(email)
        try:
            sess = _create_session(proxy=proxy)
            did = str(uuid.uuid4())
            csrf = _step_csrf(sess, lambda *_a, **_k: None)
            au = _step_auth_url(sess, csrf, lambda *_a, **_k: None, device_id=did, login_hint=email)
            current = au
            landing = au
            for _hop in range(10):
                r = sess.get(current, headers=nav, timeout=30, allow_redirects=False)
                loc = (r.headers.get("Location") or "").strip()
                if r.status_code in (301, 302, 303, 307, 308) and loc:
                    current = loc if loc.startswith("http") else urljoin(current, loc)
                    continue
                landing = current
                break
            cls = _classify(landing)
            counts[cls.split(":")[0].split("(")[0]] = counts.get(cls.split(":")[0].split("(")[0], 0) + 1
            mark = "[PASS]" if cls == "password" else "[FLAG]"
            print(f"{mark} [{i}/{len(chunk)}] {masked} → {cls}", flush=True)
            sess.close()
        except Exception as exc:  # noqa: BLE001
            print(f"[ERR ] [{i}/{len(chunk)}] {masked} → {type(exc).__name__}: {repr(exc)[:120]}", flush=True)

    print(f"[scan] summary counts={counts}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
