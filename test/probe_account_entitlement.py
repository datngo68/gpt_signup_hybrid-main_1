"""Probe live entitlement endpoint — de-risk Phase 1 gate (manual-run).

Mục đích:
    Xác minh `/backend-api/accounts/check/v4-2023-04-27` có trả entitlement
    **live** (không cache) khi accessToken được mint *trước* lúc upgrade Plus
    hay không. Đây là GATE: nếu endpoint cũng lag y hệt `/api/auth/session`
    thì live-path vô ích → ship chỉ Phase 4 (auto-poll session cache).

    Script KHÔNG sửa code production, KHÔNG submit payment. Chỉ GET đọc.

Token hygiene (BẮT BUỘC):
    - accessToken đọc qua **stdin** hoặc env `PROBE_ACCESS_TOKEN` — KHÔNG bao
      giờ qua argv (tránh lọt ~/.zsh_history, `ps aux`, CI log).
    - Script KHÔNG in request headers ra stdout.
    - Mọi JSON dump được scrub: substring bắt đầu `eyJ` (JWT/token) → [REDACTED].

Usage:
    # Cách 1 (KHUYẾN NGHỊ) — dùng file UPI job đã export (token+proxy+cookies):
    #   account lên Plus → check entitlement bằng 1 lệnh:
    .venv/bin/python test/probe_account_entitlement.py \
        --export-file runtime/upi_tokens/<email>.json

    # Token qua stdin (không lọt history):
    echo "<accessToken>" | .venv/bin/python test/probe_account_entitlement.py

    # Hoặc qua env:
    PROBE_ACCESS_TOKEN="<accessToken>" .venv/bin/python test/probe_account_entitlement.py

    # Có proxy (test H3 — IP correlation):
    echo "<token>" | .venv/bin/python test/probe_account_entitlement.py \
        --proxy http://user:pass@host:port

    # So sánh với /api/auth/session (cần cookies file — Playwright JSON
    # list, dict {name:value}, hoặc raw cookie header string):
    echo "<token>" | .venv/bin/python test/probe_account_entitlement.py \
        --cookies runtime/sessions/<file>-cookies.json

Flags (optional — KHÔNG nhận token qua flag):
    --proxy URL        proxy http(s); cũng đọc env PROBE_PROXY.
    --cookies PATH     file cookies để gọi /api/auth/session so sánh.
    --impersonate TOK  curl_cffi impersonate token (default persona primary).
    --no-proxy-skip    chỉ chạy qua proxy, bỏ lần no-proxy (mặc định chạy cả 2).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

try:
    from gpt_signup_hybrid_new.user_agent_profile import (
        CURL_IMPERSONATE_PRIMARY,
        SEC_CH_UA,
        SEC_CH_UA_MOBILE,
        SEC_CH_UA_PLATFORM,
        WINDOWS_USER_AGENT,
    )
except ImportError:  # chạy từ trong package root
    from user_agent_profile import (  # type: ignore
        CURL_IMPERSONATE_PRIMARY,
        SEC_CH_UA,
        SEC_CH_UA_MOBILE,
        SEC_CH_UA_PLATFORM,
        WINDOWS_USER_AGENT,
    )

ENTITLEMENT_URL = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
SESSION_URL = "https://chatgpt.com/api/auth/session"
ENTITLEMENT_PATH = "/backend-api/accounts/check/v4-2023-04-27"

# JWT/access-token leak guard: mọi chuỗi bắt đầu "eyJ" + chars base64url/dot.
_TOKEN_RE = re.compile(r"eyJ[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)*")


def _scrub(text: str) -> str:
    """Thay mọi JWT-like substring (prefix eyJ) bằng marker — M6."""
    return _TOKEN_RE.sub("eyJ…[REDACTED]", text)


def _scrub_json(obj: Any) -> str:
    return _scrub(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _read_access_token() -> str:
    """Token từ env PROBE_ACCESS_TOKEN hoặc stdin — KHÔNG argv (M6)."""
    env = os.environ.get("PROBE_ACCESS_TOKEN", "").strip()
    if env:
        return env
    if sys.stdin.isatty():
        sys.stderr.write(
            "accessToken: dán token rồi Enter (input ẩn không được, "
            "tránh chạy interactive trên máy share):\n"
        )
    token = sys.stdin.readline().strip()
    return token


def _load_cookies(path: str) -> Any:
    """Đọc cookies từ file: JSON (Playwright list / dict) hoặc raw header."""
    raw = Path(path).read_text(encoding="utf-8").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw  # raw cookie header string


def _parse_entitlement_plan(data: dict[str, Any]) -> dict[str, Any]:
    """Mirror logic Phase 2 dự kiến — strict Plus-only (is_plus = active & label==plus).

    KHÔNG phải code production; chỉ để probe in plan parse được.
    """
    blank = {"plan": None, "is_plus": False, "has_active_subscription": False, "expires": None}
    if not isinstance(data, dict):
        return blank
    accounts = data.get("accounts")
    if not isinstance(accounts, dict) or not accounts:
        return blank
    acct = accounts.get("default")
    if not isinstance(acct, dict):
        # account đầu tiên nếu không có "default"
        acct = next((v for v in accounts.values() if isinstance(v, dict)), None)
    if not isinstance(acct, dict):
        return blank
    ent = acct.get("entitlement")
    if not isinstance(ent, dict):
        return blank
    raw_plan = ent.get("subscription_plan")
    label = None
    if isinstance(raw_plan, str) and raw_plan.strip():
        s = raw_plan.strip().lower()
        if s.startswith("chatgpt"):
            s = s[len("chatgpt"):]
        if s.endswith("plan"):
            s = s[: -len("plan")]
        label = s or None
    has_active = bool(ent.get("has_active_subscription"))
    return {
        "plan": label,
        "is_plus": has_active and label == "plus",
        "has_active_subscription": has_active,
        "expires": ent.get("expires_at"),
    }


def _build_headers(access_token: str, *, variant: str) -> dict[str, str]:
    """2 biến thể header để chốt recipe nào trả 200 (C1).

    full    = recipe backend-api thật (mirror upi_runner.py:503-517).
    minimal = chỉ Bearer + UA/sec-ch-ua (thiếu Origin/x-openai-target-*/OAI-Language).
    """
    base = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "*/*",
        "User-Agent": WINDOWS_USER_AGENT,
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": SEC_CH_UA_PLATFORM,
    }
    if variant == "minimal":
        return base
    base.update({
        "Accept-Language": "en-IN,en;q=0.9",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "x-openai-target-path": ENTITLEMENT_PATH,
        "x-openai-target-route": ENTITLEMENT_PATH,
        "OAI-Language": "en-IN",
    })
    return base


async def _probe_once(
    *,
    access_token: str,
    variant: str,
    proxy: str | None,
    impersonate: str,
) -> dict[str, Any]:
    """1 lần GET entitlement với (variant header, proxy) cho trước."""
    from curl_cffi.requests import AsyncSession

    headers = _build_headers(access_token, variant=variant)
    proxies = {"http": proxy, "https": proxy} if proxy else None
    label = f"variant={variant:<7} proxy={'ON ' if proxy else 'OFF'}"
    out: dict[str, Any] = {"variant": variant, "proxy": bool(proxy), "status": None}
    try:
        async with AsyncSession(impersonate=impersonate, proxies=proxies) as sess:
            resp = await sess.get(ENTITLEMENT_URL, headers=headers, timeout=20.0)
    except Exception as exc:  # noqa: BLE001 — probe muốn nuốt mọi lỗi để in
        out["error"] = _scrub(str(exc))
        print(f"  [{label}] ERROR: {out['error']}")
        return out

    out["status"] = resp.status_code
    print(f"  [{label}] HTTP {resp.status_code}")
    if resp.status_code != 200:
        body = _scrub((resp.text or "")[:300])
        print(f"      body[:300]: {body}")
        return out
    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"JSON parse fail: {exc}"
        print(f"      JSON parse fail: {exc}")
        return out

    parsed = _parse_entitlement_plan(data)
    out["parsed"] = parsed
    # In entitlement block scrubbed + plan parse được
    accounts = data.get("accounts") if isinstance(data, dict) else None
    ent_block: Any = None
    if isinstance(accounts, dict):
        acct = accounts.get("default") or next(
            (v for v in accounts.values() if isinstance(v, dict)), None
        )
        if isinstance(acct, dict):
            ent_block = acct.get("entitlement")
    print("      entitlement: " + _scrub_json(ent_block))
    print(f"      parsed: {parsed}")
    return out


async def _probe_session(*, cookies: Any, proxy: str | None, impersonate: str) -> None:
    """So sánh baseline: /api/auth/session (cookie-auth) → accountPlan/planType."""
    try:
        try:
            from gpt_signup_hybrid_new.session_phase import fetch_session_via_http
        except ImportError:
            from session_phase import fetch_session_via_http  # type: ignore
        data = await fetch_session_via_http(
            cookies=cookies, proxy=proxy, timeout=20.0, impersonate=impersonate
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  /api/auth/session ERROR: {_scrub(str(exc))}")
        return
    top = data.get("accountPlan") if isinstance(data, dict) else None
    acct = data.get("account") if isinstance(data, dict) else None
    plan_type = acct.get("planType") if isinstance(acct, dict) else None
    print(f"  /api/auth/session → accountPlan={top!r}  account.planType={plan_type!r}")
    print("      (đây là giá trị CACHE — so với entitlement live ở trên)")


async def _main_async(args: argparse.Namespace) -> int:
    print("⚠️  TOKEN HYGIENE: accessToken paid = full account-takeover credential.")
    print("    Token chỉ đọc qua stdin/env (không argv). Tránh chạy trên máy share /")
    print("    CI log; output đã scrub mọi chuỗi prefix 'eyJ'. KHÔNG paste output có token.")
    print()

    # Nguồn token: --export-file (file runtime/session_cache/<instance>/<slug>.json do UPI
    # job ghi ra) > stdin/env. File là local + gitignored → token KHÔNG qua argv.
    export_cookies: Any = None
    export_proxy: str | None = None
    if args.export_file:
        try:
            ed = json.loads(Path(args.export_file).read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"✗ Đọc export-file lỗi: {exc}")
            return 2
        access_token = (ed.get("access_token") or "").strip()
        export_cookies = ed.get("session_cookies") or ed.get("cookies")  # chấp nhận cả 2 key
        export_proxy = ed.get("proxy")
        print(
            f"  (export-file: {args.export_file} — email={ed.get('email')!r} "
            f"exported_at={ed.get('exported_at')!r} qr={ed.get('qr_produced')})"
        )
    else:
        access_token = _read_access_token()

    if not access_token:
        print("✗ Thiếu accessToken (export-file rỗng / stdin rỗng & env PROBE_ACCESS_TOKEN trống).")
        return 2
    if not access_token.startswith("eyJ"):
        print("⚠️  Token không bắt đầu 'eyJ' — có chắc là accessToken JWT? Vẫn thử.")

    # Proxy: --proxy > env > proxy đã mint token (export-file). Replay Bearer
    # qua đúng IP tránh 403/correlation (H3).
    proxy = args.proxy or os.environ.get("PROBE_PROXY") or export_proxy or None
    impersonate = args.impersonate or CURL_IMPERSONATE_PRIMARY

    # Ma trận: 2 header variant × {no-proxy, proxy} (H3 + C1).
    proxy_modes: list[str | None] = []
    if not args.no_proxy_skip:
        proxy_modes.append(None)
    if proxy:
        proxy_modes.append(proxy)
    if not proxy_modes:
        proxy_modes = [None]

    print(f"Endpoint: {ENTITLEMENT_URL}")
    print(f"Impersonate: {impersonate}  | proxy configured: {bool(proxy)}")
    print("─" * 64)
    print("PROBE entitlement (live):")
    results: list[dict[str, Any]] = []
    for pmode in proxy_modes:
        for variant in ("full", "minimal"):
            results.append(
                await _probe_once(
                    access_token=access_token,
                    variant=variant,
                    proxy=pmode,
                    impersonate=impersonate,
                )
            )

    # Baseline session: cookies từ --cookies hoặc export-file (session_cookies).
    cookies_for_baseline: Any = None
    if args.cookies:
        cookies_for_baseline = _load_cookies(args.cookies)
    elif export_cookies:
        cookies_for_baseline = export_cookies
    if cookies_for_baseline is not None:
        print("─" * 64)
        print("BASELINE /api/auth/session (cache):")
        await _probe_session(
            cookies=cookies_for_baseline, proxy=proxy, impersonate=impersonate
        )

    # Verdict summary — copy vào plan.md.
    print("─" * 64)
    print("VERDICT (copy vào plan.md):")
    ok_combos = [
        f"{r['variant']}/{'proxy' if r['proxy'] else 'noproxy'}"
        for r in results
        if r.get("status") == 200
    ]
    blocked = [
        f"{r['variant']}/{'proxy' if r['proxy'] else 'noproxy'}={r.get('status')}"
        for r in results
        if r.get("status") not in (200, None)
    ]
    print(f"  Header recipe 200: {ok_combos or '(none — tất cả non-200)'}")
    print(f"  Non-200 combos:    {blocked or '(none)'}")
    plus_seen = any(
        r.get("status") == 200 and (r.get("parsed") or {}).get("is_plus")
        for r in results
    )
    plan_seen = sorted({
        (r.get("parsed") or {}).get("plan")
        for r in results
        if r.get("status") == 200 and (r.get("parsed") or {}).get("plan")
    })
    print(f"  Plan parse được:   {plan_seen or '(none)'}  | is_plus seen: {plus_seen}")
    print("  → PASS nếu entitlement live phản ánh Plus với token STALE (pre-upgrade);")
    print("    FAIL nếu live cũng trả plan giống cache /api/auth/session.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe live entitlement endpoint (manual-run).")
    parser.add_argument("--proxy", default=None, help="proxy http(s) URL (hoặc env PROBE_PROXY)")
    parser.add_argument("--cookies", default=None, help="file cookies để gọi /api/auth/session so sánh")
    parser.add_argument("--impersonate", default=None, help="curl_cffi impersonate token")
    parser.add_argument(
        "--export-file", default=None,
        help="file runtime/session_cache/<instance>/<slug>.json (UPI job ghi) — lấy token+proxy+cookies, KHÔNG cần stdin",
    )
    parser.add_argument(
        "--no-proxy-skip", action="store_true",
        help="bỏ lần no-proxy, chỉ chạy qua proxy",
    )
    # KHÔNG có flag token — M6.
    args = parser.parse_args()
    try:
        return asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
