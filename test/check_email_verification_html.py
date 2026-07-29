"""Extract response body của /email-verification (1st + 2nd load) trong HAR
manual để xem nội dung HTML — confirm có button "Continue with password" hay
form set password trực tiếp.
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

HAR = Path(
    "/Users/vippro/Developments/gpt_signup_hybrid/runtime/research_logs/"
    "web_record_20260625-120705_manual/trace.har.critical_cache.json"
)


def _decode(content: dict) -> str:
    text = content.get("text") or ""
    if content.get("encoding") == "base64":
        try:
            return base64.b64decode(text).decode("utf-8", errors="replace")
        except Exception:
            return ""
    return text


def main() -> int:
    print(f"loading {HAR} ({HAR.stat().st_size / 1024 / 1024:.1f} MB)...")
    har = json.loads(HAR.read_text())
    if isinstance(har, dict):
        entries = har.get("log", {}).get("entries", [])
    elif isinstance(har, list):
        # critical_cache.json: list of entries trực tiếp
        entries = har
    else:
        print(f"unexpected HAR shape: {type(har).__name__}")
        return 1
    print(f"  entries: {len(entries)}")

    target_paths = (
        "/email-verification",
        "/create-account/password",
        "/about-you",
    )
    hits = []
    for e in entries:
        req = e.get("request", {})
        res = e.get("response", {})
        url = req.get("url", "")
        # match exact path (không có query string)
        path = url.split("?")[0]
        for tp in target_paths:
            if path.endswith(tp):
                hits.append((e.get("startedDateTime"), req.get("method"), res.get("status"), url, res))
                break

    print(f"\n  hits: {len(hits)}")
    for i, (ts, method, status, url, res) in enumerate(hits, 1):
        print(f"\n--- hit {i}: {method} {url.split('?')[0]} → {status} (at {ts}) ---")
        content = res.get("content", {}) or {}
        mime = content.get("mimeType", "")
        size = content.get("size", 0)
        body = _decode(content)
        print(f"  mime={mime} size={size}")
        if not body:
            print("  (no body)")
            continue
        # Tìm các marker quan trọng
        markers = {
            "password input": r"<input[^>]*type=\"password\"",
            "input name=username": r"<input[^>]*name=\"username\"",
            "input name=password": r"<input[^>]*name=\"password\"",
            "input name=code (OTP)": r"<input[^>]*name=\"code\"",
            "Continue with password text": r"[Cc]ontinue with password",
            "Use a password instead": r"[Uu]se a password",
            "Login button text": r">[Cc]ontinue<",
            "form action": r"<form[^>]*action=\"[^\"]*\"",
        }
        for label, pat in markers.items():
            m = re.findall(pat, body, re.IGNORECASE)
            if m:
                print(f"  ✓ {label}: {len(m)} occurrence(s) — first: {str(m[0])[:120]}")
            else:
                print(f"  · {label}: not found")

        # Print 1st 800 chars
        snippet = body[:800].replace("\n", " ")
        print(f"  body[:800]: {snippet}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
