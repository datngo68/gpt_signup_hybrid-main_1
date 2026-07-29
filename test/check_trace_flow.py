"""Đọc actions.jsonl + filter requests.jsonl của manual record để xem
trace có chứa flow signup thật không, và list các bước theo thứ tự thời gian.

Mục đích: confirm trace.zip dùng được để verify flow steps của browser_phase.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(
    "/Users/vippro/Developments/gpt_signup_hybrid/runtime/research_logs/"
    "web_record_20260625-120705_manual"
)

ACTIONS = ROOT / "actions.jsonl"
REQUESTS = ROOT / "requests.jsonl"

KEY_PATTERNS = re.compile(
    r"auth\.openai\.com.*?(?:"
    r"/api/auth/(csrf|signin|callback)|"
    r"/api/accounts/(authorize|user/register|email-otp|create_account|user/me|user/check)|"
    r"/about-you|/email-verification|/log-in|/create-account|/identifier|/sso|/passkey"
    r")|"
    r"chatgpt\.com/api/auth/(csrf|signin|callback)",
    re.IGNORECASE,
)
# Loại CDN/static, jsd challenge → focus navigation requests.
SKIP_PATTERNS = re.compile(
    r"/cdn-cgi/|/cdn/|\.js$|\.css$|\.svg$|\.webp$|\.png$|\.woff",
    re.IGNORECASE,
)


def main() -> int:
    print("== actions.jsonl ==")
    if ACTIONS.exists():
        for line in ACTIONS.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = rec.get("t")
            evt = rec.get("event_type")
            url = rec.get("url", "")
            print(f"  t={t:>7.2f}  {evt:<20} {url}")

    print("\n== requests.jsonl: navigation+API hits (in order, dedup near-duplicates) ==")
    if not REQUESTS.exists():
        print("  (missing)")
        return 1

    seen = set()
    matched = 0
    rows: list[tuple] = []
    for line in REQUESTS.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        url = rec.get("url", "")
        if SKIP_PATTERNS.search(url):
            continue
        if not KEY_PATTERNS.search(url):
            continue
        method = rec.get("method", "?")
        status = rec.get("status") or rec.get("response_status")
        # bỏ rows không có status (dòng trùng "request started")
        if status is None:
            continue
        ts = (
            rec.get("timestamp")
            or rec.get("ts")
            or rec.get("time")
            or rec.get("t")
        )
        # rút gọn URL
        short = url.split("?")[0]
        rows.append((ts, method, status, short, url))

    for ts, method, status, short, url in rows:
        print(f"  t={ts:>7}  {method:<5} {status:<5} {short}")
        matched += 1

    # đếm các path quan trọng
    print("\n== path counts ==")
    counters: dict[str, int] = {}
    for ts, method, status, short, url in rows:
        for key in (
            "/api/auth/csrf",
            "/api/auth/signin",
            "/api/auth/callback",
            "/api/accounts/authorize",
            "/api/accounts/user/register",
            "/api/accounts/email-otp/send",
            "/api/accounts/email-otp/validate",
            "/api/accounts/create_account",
            "/email-verification",
            "/create-account/password",
            "/about-you",
            "/log-in",
            "/identifier",
            "/passkey",
        ):
            if key in url:
                counters[key] = counters.get(key, 0) + 1
                break
    for key in sorted(counters, key=lambda k: -counters[k]):
        print(f"  {counters[key]:>2}  {key}")

    print(f"\n  total: {matched}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
