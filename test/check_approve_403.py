"""Smoke test: phân tích nguyên nhân HTTP 403 ở approve loop UPI.

Flow:
    [1/3] Login pure-HTTP DIRECT → access_token.
    [2/3] Create checkout DIRECT → session_id.
    [3/3] POST /backend-api/payments/checkout/approve
            (a) DIRECT
            (b) (optional) qua --proxy → so sánh với DIRECT.

Verdict tự động:
    - DIRECT 200 + proxy 403 → Cloudflare BAN proxy IP. Token/account OK.
    - DIRECT 403            → Token/session/account vấn đề (KHÔNG phải proxy).
    - DIRECT 200 + proxy 200 → Proxy này clean, 403 trong production từ proxy khác.
    - Cả 2 NetworkError      → Proxy/đường mạng down, không phải Cloudflare.

Usage:
    python3 test/check_approve_403.py \\
        --combo 'email|password|secret' \\
        [--proxy 'http://user:pass@host:port']
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from curl_cffi.requests import AsyncSession  # noqa: E402

from session_phase import get_session_pure_request  # noqa: E402
from pay_upi_http import (  # noqa: E402
    _CHATGPT_APPROVE_URL,
    _create_chatgpt_checkout,
    _IMPERSONATE,
    _SEC_CH_UA,
    _SEC_CH_UA_MOBILE,
    _SEC_CH_UA_PLATFORM,
    _USER_AGENT,
)


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _log(label: str, msg: str) -> None:
    print(f"[{_ts()}] [{label}] {msg}", flush=True)


def _truncate(s: str, n: int = 300) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[:n] + f"…(+{len(s) - n} chars)"


def _classify_body(status: int, body: str) -> str:
    """Phân loại body lỗi để biết Cloudflare hay OpenAI app trả."""
    if status == 200:
        return "200_OK"
    bl = (body or "").lower()
    if "cf-ray" in bl or "cloudflare" in bl or "just a moment" in bl \
            or "attention required" in bl:
        return "CLOUDFLARE_BLOCK"
    if bl.lstrip().startswith("<!doctype html") or bl.lstrip().startswith("<html"):
        return "HTML_PAGE"
    try:
        j = json.loads(body)
        if isinstance(j, dict):
            if "detail" in j or "error" in j or "message" in j:
                return "OPENAI_JSON_ERROR"
        return "JSON_RESPONSE"
    except Exception:
        return "UNKNOWN_NON_JSON"


def _mask_proxy(url: str) -> str:
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        return f"{p.scheme}://***@{p.hostname}:{p.port}"
    except Exception:
        return "***"


async def _call_approve(
    sess,
    *,
    label: str,
    session_id: str,
    access_token: str,
    proxies: dict | None,
) -> dict:
    body = {"checkout_session_id": session_id, "processor_entity": "openai_llc"}
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Origin": "https://chatgpt.com",
        "Referer": f"https://chatgpt.com/checkout/openai_llc/{session_id}",
        "User-Agent": _USER_AGENT,
        "sec-ch-ua": _SEC_CH_UA,
        "sec-ch-ua-mobile": _SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": _SEC_CH_UA_PLATFORM,
        "x-openai-target-path": "/backend-api/payments/checkout/approve",
        "x-openai-target-route": "/backend-api/payments/checkout/approve",
        "OAI-Language": "en-IN",
    }
    t0 = time.monotonic()
    try:
        resp = await sess.post(
            _CHATGPT_APPROVE_URL,
            headers=headers,
            json=body,
            timeout=30,
            proxies=proxies,
        )
    except Exception as exc:
        elapsed = time.monotonic() - t0
        _log(label, f"NETWORK ERROR after {elapsed:.2f}s: "
                    f"{type(exc).__name__}: {str(exc)[:200]}")
        return {
            "status": None,
            "classify": "NETWORK_ERROR",
            "result": None,
            "elapsed": elapsed,
            "body_short": "",
            "error": f"{type(exc).__name__}: {exc}",
        }

    elapsed = time.monotonic() - t0
    text = resp.text or ""
    classify = _classify_body(resp.status_code, text)
    result = None
    try:
        j = resp.json()
        if isinstance(j, dict):
            result = j.get("result")
    except Exception:
        pass

    _log(label,
         f"HTTP {resp.status_code}  {elapsed:.2f}s  "
         f"classify={classify}  result={result}")
    if resp.status_code != 200 or result not in ("approved",):
        _log(label, f"body[:300]={_truncate(text, 300)}")
    return {
        "status": resp.status_code,
        "classify": classify,
        "result": result,
        "elapsed": elapsed,
        "body_short": _truncate(text, 500),
    }


async def _run(combo: str, proxy: str | None) -> int:
    parts = combo.split("|")
    if len(parts) < 2:
        _log("init",
             "--combo phải có ít nhất 'email|password' (option |secret), "
             f"nhận: {combo[:40]}")
        return 2
    email = parts[0].strip()
    password = parts[1].strip()
    secret = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None

    _log("init",
         f"email={email}  has_secret={secret is not None}  "
         f"proxy={'yes (' + _mask_proxy(proxy) + ')' if proxy else 'no'}")

    # ─── [1/3] Login DIRECT ──────────────────────────────────────────
    _log("[1/3]", "login pure-HTTP DIRECT…")
    t0 = time.monotonic()
    try:
        session_data = await get_session_pure_request(
            email=email,
            password=password,
            secret=secret,
            proxy=None,
            log=lambda m: _log("[1/3]", m),
        )
    except Exception as exc:
        _log("[1/3]", f"LOGIN FAIL: {type(exc).__name__}: {str(exc)[:300]}")
        return 1
    elapsed = time.monotonic() - t0
    access_token = session_data.get("accessToken") if isinstance(session_data, dict) else None
    if not access_token:
        _log("[1/3]", f"login response thiếu accessToken: "
                     f"{str(session_data)[:200]}")
        return 1
    cookies_count = len(session_data.get("__cookies") or [])
    _log("[1/3]",
         f"OK {elapsed:.2f}s  token={access_token[:20]}…  "
         f"cookies={cookies_count}")

    # ─── [2/3] Create checkout DIRECT ────────────────────────────────
    _log("[2/3]", "create checkout DIRECT…")
    t0 = time.monotonic()
    async with AsyncSession(impersonate=_IMPERSONATE) as sess:
        try:
            checkout = await _create_chatgpt_checkout(
                sess,
                access_token=access_token,
                log=lambda m: _log("[2/3]", m),
                proxies=None,
            )
        except Exception as exc:
            _log("[2/3]", f"CHECKOUT FAIL: {type(exc).__name__}: "
                          f"{str(exc)[:300]}")
            return 1
        elapsed = time.monotonic() - t0
        session_id = checkout["checkout_session_id"]
        _log("[2/3]", f"OK {elapsed:.2f}s  session_id={session_id[:30]}…")

        # ─── [3/3] Approve ─────────────────────────────────────────
        _log("[3/3]", "approve DIRECT…")
        direct_res = await _call_approve(
            sess,
            label="approve.direct",
            session_id=session_id,
            access_token=access_token,
            proxies=None,
        )

        proxy_res = None
        if proxy:
            _log("[3/3]", f"approve via proxy={_mask_proxy(proxy)}…")
            proxy_res = await _call_approve(
                sess,
                label="approve.proxy",
                session_id=session_id,
                access_token=access_token,
                proxies={"http": proxy, "https": proxy},
            )

    # ─── Verdict ────────────────────────────────────────────────────
    print("", flush=True)
    print("─" * 72, flush=True)
    _log("verdict",
         f"DIRECT  status={direct_res.get('status')}  "
         f"classify={direct_res.get('classify')}  "
         f"result={direct_res.get('result')}")
    if proxy_res:
        _log("verdict",
             f"PROXY   status={proxy_res.get('status')}  "
             f"classify={proxy_res.get('classify')}  "
             f"result={proxy_res.get('result')}")
    print("─" * 72, flush=True)

    ds = direct_res.get("status")
    dc = direct_res.get("classify")
    ps = proxy_res.get("status") if proxy_res else None
    pc = proxy_res.get("classify") if proxy_res else None

    if ds == 200 and ps == 403:
        if pc == "CLOUDFLARE_BLOCK":
            _log("verdict",
                 "→ Cloudflare BAN proxy IP. Token/account OK. "
                 "Giải pháp: thay proxy / dùng residential / loại IP đó "
                 "khỏi pool.")
        else:
            _log("verdict",
                 "→ Proxy 403 (không phải HTML Cloudflare rõ ràng). "
                 "Có thể OpenAI block qua x-forwarded headers, hoặc "
                 "proxy upstream auth fail. Đọc body[:300] để biết.")
    elif ds == 403:
        if dc == "CLOUDFLARE_BLOCK":
            _log("verdict",
                 "→ DIRECT cũng bị Cloudflare 403 — IP bot bị flag "
                 "(proxy_from_step quá thấp ở approve, hoặc IP datacenter).")
        else:
            _log("verdict",
                 "→ DIRECT 403 từ OpenAI app. Token expired / account "
                 "đã pump quá nhiều / region bị block. Account-level issue.")
    elif ds == 200 and ps == 200:
        _log("verdict",
             "→ Cả 2 đều 200. Proxy `--proxy` này CLEAN. "
             "403 trong production đến từ proxy KHÁC trong pool.")
    elif ds == 200 and ps is None:
        _log("verdict",
             "→ DIRECT OK. Để chứng minh Cloudflare ban proxy, chạy lại "
             "với `--proxy <url>` của 1 IP đang FAIL 403 từ pool.")
    elif ps is None and dc == "NETWORK_ERROR":
        _log("verdict",
             "→ Cả DIRECT cũng NetworkError. Đường mạng có vấn đề, "
             "không phải Cloudflare hay proxy.")
    else:
        _log("verdict",
             f"→ Combo lạ direct={ds}/{dc} proxy={ps}/{pc}. "
             "Đọc body[:300] phía trên.")

    return 0 if ds == 200 else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Smoke test approve 403 (DIRECT vs proxy)",
    )
    ap.add_argument("--combo", required=True, help="email|password|secret")
    ap.add_argument(
        "--proxy",
        default=None,
        help="proxy URL (optional). Truyền vào → so sánh với DIRECT.",
    )
    args = ap.parse_args()
    return asyncio.run(_run(args.combo, args.proxy))


if __name__ == "__main__":
    sys.exit(main())
