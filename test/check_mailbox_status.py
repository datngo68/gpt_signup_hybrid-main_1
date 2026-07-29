"""Probe iCloud HME mailbox status — đếm số messages trong worker logs.

Mục đích: verify nhanh xem các email iCloud HME alias còn nhận mail hay không
trước khi đưa vào reg test. Nếu mailbox empty + log không có inbound event →
HME có thể inactive / đã disable / OpenAI block.

Chạy:
    .venv/bin/python test/check_mailbox_status.py
"""
from __future__ import annotations

import json
import sys
from urllib.parse import quote
from urllib.request import Request, urlopen


EMAILS = (
    "accents_jurist.0t+ormcb@icloud.com",
    "balks_haze.4c+y2ozybp@icloud.com",
    "gazer.benign-8g+u41qs9y@icloud.com",
    "kappas-nobler-9s+ws02sr@icloud.com",
    "refit_garble.6c+y2sgbra@icloud.com",
)

WORKER_LOGS_URL = "https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/logs"
API_KEY = "12345678@"


def _probe(email: str) -> dict:
    url = f"{WORKER_LOGS_URL}?mail={quote(email)}"
    req = Request(url, headers={
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
    })
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return {
            "email": email,
            "status": resp.status,
            "messages": len(data.get("messages", [])),
            "logs": len(data.get("logs", [])),
            "has_more": data.get("pagination", {}).get("hasMore", False),
        }
    except Exception as exc:
        return {"email": email, "status": "ERR", "error": str(exc)}


def main() -> int:
    print(f"{'email':<48}{'msgs':>6}{'logs':>6}{'more':>6}", flush=True)
    print("─" * 66, flush=True)
    for email in EMAILS:
        r = _probe(email)
        if "error" in r:
            print(f"{r['email']:<48}  ERR  {r['error'][:30]}", flush=True)
            continue
        print(
            f"{r['email']:<48}{r['messages']:>6}{r['logs']:>6}"
            f"{'Y' if r['has_more'] else '.':>6}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
