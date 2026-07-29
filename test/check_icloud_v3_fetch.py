"""Fetch 1 URL từ input iCloud v3 để xem format response thật.

Run: python3 test/check_icloud_v3_fetch.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx

# 1 vài line đại diện
SAMPLES = [
    (
        "petunia-boar-3d+hblx3n@icloud.com",
        "https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/mELlXOnuhwiUDHjc7IFc-fllJLoCeuAv/data",
    ),
    (
        "pasties.sateen.7c+im3zd@icloud.com",
        "https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/g5hvaOkVuQq_THa1GONjRmzt19HaGDff/data",
    ),
]


def fetch(email: str, url: str) -> None:
    print(f"\n=== {email}")
    print(f"URL: {url}")
    try:
        r = httpx.get(
            url,
            timeout=20.0,
            follow_redirects=True,
            headers={
                "Accept": "application/json,*/*",
                "User-Agent": "Mozilla/5.0",
            },
        )
        print(f"status={r.status_code}")
        print(f"content-type={r.headers.get('content-type')}")
        text = r.text or ""
        print(f"len={len(text)}")
        print("RAW BODY (first 2000):", text[:2000])
        if "json" in (r.headers.get("content-type") or "").lower() or text.strip().startswith(("{", "[")):
            try:
                data = r.json()
                print("JSON parsed OK")
                # Hiển thị keys/shape
                if isinstance(data, list):
                    print(f"type=list, len={len(data)}")
                    if data:
                        print("first item keys:", list(data[0].keys())[:20])
                        print("first item sample:", json.dumps(data[0], ensure_ascii=False, indent=2)[:2000])
                elif isinstance(data, dict):
                    print(f"type=dict, keys={list(data.keys())[:20]}")
                    # Tìm key chứa list
                    for k in ("messages", "items", "logs", "emails", "data", "mails", "results"):
                        v = data.get(k)
                        if isinstance(v, list):
                            print(f"-> list under key={k!r}, len={len(v)}")
                            if v:
                                print(f"first item keys: {list(v[0].keys())[:20]}")
                                print("first item sample:", json.dumps(v[0], ensure_ascii=False, indent=2)[:2000])
                            break
                    else:
                        print("dict không có key list nào -> full payload (truncate 2000):")
                        print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])
            except Exception as exc:
                print("JSON parse fail:", exc)
                print("body[:600]:", text[:600])
        else:
            print("body[:1000]:", text[:1000])
    except Exception as exc:
        print("FETCH FAIL:", type(exc).__name__, exc)


def main() -> int:
    for email, url in SAMPLES:
        fetch(email, url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
