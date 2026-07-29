"""Deep-dive HAR manual record để tìm sentinel/anti-ban signal mà bot có
thể đang miss hoặc leak.

Mục tiêu: list headers + body của các POST chính (register, validate,
create_account), tìm:
  - Headers special (Origin, Referer, Sec-Fetch-*, so-token, sentinel-token)
  - Cookies chiếm trong request
  - Body schema thật (so cái bot đang gửi)
  - User-Agent + Accept-Language
  - Sequence cookies xuất hiện theo thời gian
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

INTERESTING_PATHS = (
    "/api/auth/csrf",
    "/api/auth/signin/openai",
    "/api/accounts/authorize",
    "/api/accounts/user/register",
    "/api/accounts/email-otp/send",
    "/api/accounts/email-otp/validate",
    "/api/accounts/create_account",
    "/api/auth/callback/openai",
)

# Headers đáng chú ý cho anti-ban / sentinel
SENTINEL_HEADER_HINTS = re.compile(
    r"so-token|sentinel|x-oai|x-openai|arkose|datadog|dd-|sec-ch|"
    r"sec-fetch|user-agent|accept-language|origin|referer|cookie|"
    r"csrf|authorization|x-statsig",
    re.IGNORECASE,
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
    print(f"loading {HAR.name}...")
    data = json.loads(HAR.read_text())
    entries = data if isinstance(data, list) else data.get("log", {}).get("entries", [])
    print(f"  entries: {len(entries)}")

    matches = []
    for e in entries:
        url = e.get("request", {}).get("url", "")
        path = url.split("?")[0]
        for tag in INTERESTING_PATHS:
            if path.endswith(tag):
                matches.append((tag, e))
                break

    if not matches:
        print("  no matches")
        return 1

    cookie_journal: list[tuple[str, str]] = []  # (ts, cookies)

    for tag, e in matches:
        req = e.get("request", {})
        res = e.get("response", {})
        method = req.get("method", "?")
        url = req.get("url", "")
        status = res.get("status")
        ts = e.get("startedDateTime", "")
        print()
        print("=" * 80)
        print(f"{ts}  {method} {url.split('?')[0]} → {status}")
        print(f"  full url: {url[:200]}")

        # Headers
        print("  -- REQUEST HEADERS --")
        for h in req.get("headers", []):
            name = h.get("name", "")
            value = h.get("value", "")
            if SENTINEL_HEADER_HINTS.search(name):
                # Cookie header có thể rất dài → list từng cookie
                if name.lower() == "cookie":
                    cookies = [c.strip().split("=", 1)[0] for c in value.split(";") if c.strip()]
                    print(f"    {name}: ({len(cookies)} cookies) {', '.join(cookies)}")
                    cookie_journal.append((ts, ",".join(sorted(cookies))))
                else:
                    print(f"    {name}: {value[:200]}")

        # Body
        post = req.get("postData") or {}
        body_text = post.get("text") or ""
        if body_text:
            print(f"  -- REQUEST BODY (mime={post.get('mimeType')}) --")
            short = body_text[:400]
            print(f"    {short}")
            if len(body_text) > 400:
                print(f"    ... (+{len(body_text)-400} chars)")

        # Response headers
        print("  -- RESPONSE HEADERS --")
        for h in res.get("headers", []):
            name = h.get("name", "")
            value = h.get("value", "")
            if SENTINEL_HEADER_HINTS.search(name) or name.lower() == "set-cookie":
                if name.lower() == "set-cookie":
                    # Tách cookie name
                    cname = value.split("=", 1)[0]
                    flags = ""
                    low = value.lower()
                    for flag in ("httponly", "secure", "samesite=lax", "samesite=strict", "samesite=none", "domain=", "path="):
                        if flag in low:
                            flags += f" [{flag}]"
                    print(f"    set-cookie: {cname}{flags}")
                else:
                    print(f"    {name}: {value[:200]}")

        # Response body snippet (chỉ JSON, không HTML)
        rcontent = res.get("content", {}) or {}
        rmime = rcontent.get("mimeType", "")
        if "json" in rmime.lower():
            body = _decode(rcontent)
            if body:
                short = body[:300]
                print(f"  -- RESPONSE BODY (json) --")
                print(f"    {short}")
                if len(body) > 300:
                    print(f"    ... (+{len(body)-300} chars)")

    # Cookie journal — xem cookie nào xuất hiện ở request nào
    print("\n" + "=" * 80)
    print("COOKIE JOURNAL (chronological, names only):")
    prev_set: set[str] = set()
    for ts, cs in cookie_journal:
        names = set(cs.split(",")) if cs else set()
        added = names - prev_set
        removed = prev_set - names
        marker = ""
        if added:
            marker += f" +{','.join(sorted(added))}"
        if removed:
            marker += f" -{','.join(sorted(removed))}"
        print(f"  {ts}  count={len(names):>2}{marker}")
        prev_set = names

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
