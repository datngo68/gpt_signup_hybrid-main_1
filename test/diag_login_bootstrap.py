#!/usr/bin/env python3
"""Diagnostic THUẦN HTTP cho bug login HTTP 409 invalid_state.

Mục tiêu: xác định CHÍNH XÁC tại sao `/api/auth/signin/openai` trả về URL
``signin?csrf=true`` thay vì ``auth.openai.com/authorize?...``.

Cách dùng:
    # Bắt buộc proxy + email (dùng đúng proxy + email đang fail thật).
    python3 test/diag_login_bootstrap.py \\
        --proxy "http://user:pass@host:port" \\
        --email "your@email.com"

    # Optional: rotate impersonate fingerprint (default chrome145).
    python3 test/diag_login_bootstrap.py \\
        --proxy ... --email ... --impersonate chrome142

    # Tắt proxy nếu muốn so sánh:
    python3 test/diag_login_bootstrap.py --email ... --no-proxy

Output: in ra terminal toàn bộ
    - Step CSRF: status, response headers (đặc biệt Set-Cookie), body, jar.
    - Step signin/openai: full request headers/body + full response
      headers/body + jar diff.
    - Phân loại kết quả: OK / CSRF rejected / Unknown.

Script KHÔNG đụng module signup, chỉ replay 2 request HTTP đúng cách
``request_phase`` đang gọi để thấy server thực sự trả gì.

Sensitive: mask giá trị cookie (chỉ in NAME + length), KHÔNG in body chứa
csrfToken full (mask 8 ký tự đầu/cuối). Ngài có thể paste output cho tôi
mà không lộ secret.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ── Sensitive masking helpers ────────────────────────────────────────


def _mask_value(value: str, *, keep: int = 6) -> str:
    if not value:
        return "<empty>"
    if len(value) <= keep * 2:
        return f"<{len(value)}b>"
    return f"{value[:keep]}…{value[-keep:]}<{len(value)}b>"


def _mask_cookie_dict(cookies_obj: Any) -> list[dict[str, Any]]:
    """Convert cookie jar → list of {name, domain, path, value_preview, secure, http_only}."""
    out: list[dict[str, Any]] = []
    try:
        for c in cookies_obj.jar:
            out.append({
                "name": c.name,
                "domain": c.domain,
                "path": c.path,
                "value": _mask_value(c.value or ""),
                "secure": c.secure,
                "http_only": getattr(c, "_rest", {}).get("HttpOnly") is not None,
                "expires": c.expires,
            })
    except Exception as exc:  # noqa: BLE001
        out.append({"_error": f"cookie iter failed: {exc!r}"})
    return out


def _mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    if len(local) <= 2:
        local_mask = "*" * len(local)
    else:
        local_mask = local[0] + "*" * (len(local) - 2) + local[-1]
    return f"{local_mask}@{domain}"


def _redact_set_cookie(value: str) -> str:
    """Set-Cookie header có thể chứa cookie value secret. Mask phần value, giữ name + flags."""
    if "=" not in value:
        return value
    name, _, rest = value.partition("=")
    val_part, sep, attrs = rest.partition(";")
    return f"{name}={_mask_value(val_part)}{sep}{attrs}"


def _dump_response_headers(resp: Any) -> dict[str, list[str]]:
    """Capture all response headers including multi-value Set-Cookie."""
    out: dict[str, list[str]] = {}
    try:
        # curl_cffi response.headers can have duplicate keys (Set-Cookie).
        for key, value in resp.headers.items():
            lower = key.lower()
            if lower == "set-cookie":
                value = _redact_set_cookie(value)
            out.setdefault(lower, []).append(value)
    except Exception as exc:  # noqa: BLE001
        out["_error"] = [f"header iter failed: {exc!r}"]
    return out


def _print_section(title: str) -> None:
    print(f"\n{'=' * 8} {title} {'=' * 8}", flush=True)


def _pretty_json(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)


# ── Diagnostic core ──────────────────────────────────────────────────


def diag(*, email: str, proxy: str | None, impersonate: str) -> int:
    masked_email = _mask_email(email)
    print(f"[diag] start — email={masked_email} proxy={'<set>' if proxy else '<none>'} impersonate={impersonate}", flush=True)

    # Lazy import sau khi append sys.path để curl_cffi resolved.
    from curl_cffi import requests as curl_requests
    from request_phase import _common_headers
    from user_agent_profile import (
        SEC_CH_UA,
        SEC_CH_UA_MOBILE,
        SEC_CH_UA_PLATFORM,
        WINDOWS_USER_AGENT,
    )

    session = curl_requests.Session(impersonate=impersonate)
    session.trust_env = False
    if proxy:
        normalized = proxy
        if proxy.startswith("socks5://"):
            normalized = "socks5h://" + proxy[len("socks5://"):]
        session.proxies = {"https": normalized, "http": normalized}
    else:
        session.proxies = {"https": "", "http": ""}

    # ── STEP 0: GET /auth/login (HTML) ───────────────────────────────
    # Giả thuyết: NextAuth chatgpt.com chỉ set cookie `__Host-next-auth.csrf-token`
    # qua SSR khi render HTML page, KHÔNG phải khi gọi thẳng `/api/auth/csrf`.
    # Browser thật luôn load /auth/login trước → cookie set qua SSR → token JSON
    # ở step 1 sẽ match cookie. Pure-request hit thẳng API → cookie thiếu → token
    # "free-floating" không bind cookie → POST step 2 reject.
    _print_section("STEP 0 — GET /auth/login (HTML page, prime CSRF cookie)")
    landing_headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Referer": "https://chatgpt.com/",
        "User-Agent": WINDOWS_USER_AGENT,
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": SEC_CH_UA_PLATFORM,
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Connection": "keep-alive",
    }
    print("[req] url    : https://chatgpt.com/auth/login", flush=True)
    print("[req] method : GET (top-level navigation mimic)", flush=True)
    print("[req] headers:", flush=True)
    print(_pretty_json(landing_headers), flush=True)

    try:
        landing_resp = session.get(
            "https://chatgpt.com/auth/login",
            headers=landing_headers,
            timeout=30,
            allow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] landing GET raised: {exc!r}", flush=True)
        return 2

    print(f"[res] status     : {landing_resp.status_code}", flush=True)
    print(f"[res] final url  : {getattr(landing_resp, 'url', '')}", flush=True)
    print(f"[res] body length: {len(landing_resp.text or '')}", flush=True)
    print("[res] headers (Set-Cookie redacted):", flush=True)
    print(_pretty_json(_dump_response_headers(landing_resp)), flush=True)
    print("[jar after step 0]:", flush=True)
    print(_pretty_json(_mask_cookie_dict(session.cookies)), flush=True)

    # ── STEP 1: GET /api/auth/csrf ───────────────────────────────────
    _print_section("STEP 1 — GET /api/auth/csrf")
    csrf_headers = _common_headers("https://chatgpt.com/auth/login")
    print("[req] url    : https://chatgpt.com/api/auth/csrf", flush=True)
    print("[req] method : GET", flush=True)
    print("[req] headers:", flush=True)
    print(_pretty_json(csrf_headers), flush=True)

    try:
        csrf_resp = session.get(
            "https://chatgpt.com/api/auth/csrf",
            headers=csrf_headers,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] CSRF GET raised: {exc!r}", flush=True)
        return 2

    print(f"[res] status : {csrf_resp.status_code}", flush=True)
    print("[res] headers (Set-Cookie redacted):", flush=True)
    print(_pretty_json(_dump_response_headers(csrf_resp)), flush=True)

    csrf_body_text = (csrf_resp.text or "")[:1000]
    print(f"[res] body   : {csrf_body_text}", flush=True)

    csrf_token = ""
    try:
        csrf_token = csrf_resp.json().get("csrfToken", "")
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] CSRF body không phải JSON: {exc!r}", flush=True)

    print(f"[csrf] token : {_mask_value(csrf_token)}", flush=True)
    print("[jar after step 1]:", flush=True)
    print(_pretty_json(_mask_cookie_dict(session.cookies)), flush=True)

    if csrf_resp.status_code != 200 or not csrf_token:
        print("[CONCLUSION] CSRF step fail — NextAuth không trả csrfToken.", flush=True)
        return 1

    # ── STEP 2: POST /api/auth/signin/openai ─────────────────────────
    _print_section("STEP 2 — POST /api/auth/signin/openai")
    device_id = str(uuid.uuid4())
    params = {
        "prompt": "login",
        "ext-passkey-client-capabilities": "01001",
        "screen_hint": "login_or_signup",
        "ext-oai-did": device_id,
        "login_hint": email,
    }
    signin_url = "https://chatgpt.com/api/auth/signin/openai?" + urlencode(params)

    signin_headers = _common_headers("https://chatgpt.com/auth/login")
    signin_headers["Content-Type"] = "application/x-www-form-urlencoded"

    body_dict = {
        "csrfToken": csrf_token,
        "callbackUrl": "https://chatgpt.com/",
        "json": "true",
    }

    print(f"[req] url    : {signin_url}", flush=True)
    print("[req] method : POST", flush=True)
    print("[req] headers:", flush=True)
    print(_pretty_json(signin_headers), flush=True)
    print("[req] body   : (csrfToken masked)", flush=True)
    print(_pretty_json({**body_dict, "csrfToken": _mask_value(csrf_token)}), flush=True)

    try:
        signin_resp = session.post(
            signin_url,
            headers=signin_headers,
            data=body_dict,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] signin/openai POST raised: {exc!r}", flush=True)
        return 2

    print(f"[res] status : {signin_resp.status_code}", flush=True)
    print("[res] headers (Set-Cookie redacted):", flush=True)
    print(_pretty_json(_dump_response_headers(signin_resp)), flush=True)

    signin_body_text = (signin_resp.text or "")[:2000]
    print(f"[res] body   : {signin_body_text}", flush=True)

    print("[jar after step 2]:", flush=True)
    print(_pretty_json(_mask_cookie_dict(session.cookies)), flush=True)

    # ── Phân loại kết quả ────────────────────────────────────────────
    _print_section("CONCLUSION")
    try:
        payload = signin_resp.json() if signin_resp.status_code == 200 else None
    except Exception:
        payload = None

    auth_url = (payload or {}).get("url", "") if isinstance(payload, dict) else ""

    if signin_resp.status_code != 200:
        print(f"[CONCLUSION] HTTP {signin_resp.status_code} — không phải 200, NextAuth từ chối ở layer khác.", flush=True)
        return 1

    if not auth_url:
        print(f"[CONCLUSION] response không có field 'url' — payload bất thường: {payload!r}", flush=True)
        return 1

    if "auth.openai.com" in auth_url:
        print(f"[CONCLUSION] OK — auth_url valid: {auth_url[:120]}", flush=True)
        print("    → Hypothesis VERIFIED: visit /auth/login HTML page TRƯỚC", flush=True)
        print("       làm middleware NextAuth set cookie csrf-token qua SSR.", flush=True)
        print("       Cần vá `_step_csrf` trong production: thêm 1 bước GET", flush=True)
        print("       https://chatgpt.com/auth/login PHÍA TRƯỚC GET /api/auth/csrf.", flush=True)
        return 0

    if "/api/auth/signin?csrf" in auth_url or "csrf=true" in auth_url:
        print("[CONCLUSION] Vẫn bị NextAuth từ chối CSRF dù đã GET landing page.", flush=True)
        print(f"    Got URL: {auth_url}", flush=True)
        print("    → Hypothesis FALSE — phải tìm hướng khác.", flush=True)
        print("    Kiểm tra jar after step 0: nếu KHÔNG thấy `__Host-next-auth.csrf-token`", flush=True)
        print("    thì SSR cũng không set, có thể chatgpt.com mới strip cookie ở", flush=True)
        print("    response curl_cffi (anti-bot fingerprint detect).", flush=True)
        return 1

    print(f"[CONCLUSION] auth_url chỉ là URL khác lạ: {auth_url}", flush=True)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True, help="Email account để đặt login_hint.")
    parser.add_argument(
        "--proxy",
        default=None,
        help="Proxy URL (vd http://user:pass@host:port hoặc socks5://...).",
    )
    parser.add_argument("--no-proxy", action="store_true", help="Force không dùng proxy (override --proxy).")
    parser.add_argument(
        "--impersonate",
        default="chrome145",
        help="curl_cffi impersonate fingerprint. Default: chrome145.",
    )
    args = parser.parse_args()

    proxy = None if args.no_proxy else args.proxy
    return diag(email=args.email, proxy=proxy, impersonate=args.impersonate)


if __name__ == "__main__":
    sys.exit(main())
