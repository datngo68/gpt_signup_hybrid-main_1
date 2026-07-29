#!/usr/bin/env python3
"""Diagnostic state-machine login pure-HTTP — bắt CHÍNH XÁC nguyên nhân
HTTP 409 invalid_state ở `authorize/continue`.

Mục tiêu (evidence-first, KHÔNG đoán):
    Probe A: GET /authorize WITH login_hint → in từng hop 302 + Location +
             Set-Cookie + landing cuối + cookie jar.
    Probe B: GET /authorize NO login_hint → tương tự.
    Probe C: trên session B → sentinel → POST authorize/continue → in FULL
             status + body (đây là 409 thật cần xem).
    Probe D (optional, --try-password): trên session A (login_hint) nếu landing
             undetermined → POST password/verify trực tiếp để biết server có
             chấp nhận password mà KHÔNG cần authorize/continue không.

Default KHÔNG chạy Probe D (tránh sai-password → lockout). Chỉ bật khi
--try-password.

Cách dùng:
    python3 test/diag_login_state_machine.py \
        --combo 'EMAIL|PASS|SECRET' \
        --proxy 'host:port:user:pass'      # hoặc --no-proxy

In realtime [PASS]/[FAIL] mỗi probe, flush ngay. Mask email/cookie/token.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _mask_value(value: str, *, keep: int = 6) -> str:
    if not value:
        return "<empty>"
    if len(value) <= keep * 2:
        return f"<{len(value)}b>"
    return f"{value[:keep]}…{value[-keep:]}<{len(value)}b>"


def _mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    head = local[0] if local else "*"
    return f"{head}***@{domain}"


def _jar(session: Any) -> str:
    try:
        names = sorted(
            f"{c.name}({c.domain})" for c in session.cookies.jar
        )
        return ", ".join(names) or "<empty>"
    except Exception as exc:  # noqa: BLE001
        return f"<jar-iter-error {exc!r}>"


def _set_cookie_names(resp: Any) -> str:
    out: list[str] = []
    try:
        for key, value in resp.headers.items():
            if key.lower() == "set-cookie":
                name = value.split("=", 1)[0]
                out.append(name)
    except Exception:  # noqa: BLE001
        pass
    return ", ".join(out) or "<none>"


def _p(msg: str) -> None:
    print(msg, flush=True)


def _step(tag: str, ok: bool, desc: str, detail: str = "") -> None:
    mark = "[PASS]" if ok else "[FAIL]"
    line = f"{mark} {tag} — {desc}"
    if detail:
        line += f" :: {detail}"
    print(line, flush=True)


def _follow_manual(session, url: str, headers: dict, *, max_hops: int = 10) -> str:
    """Follow 3xx tay, in từng hop. Trả landing cuối."""
    current = url
    for hop in range(1, max_hops + 1):
        resp = session.get(current, headers=headers, timeout=30, allow_redirects=False)
        loc = (resp.headers.get("Location") or "").strip()
        _p(
            f"       hop{hop}: {resp.status_code}  "
            f"set-cookie=[{_set_cookie_names(resp)}]  "
            f"loc={loc[:110]!r}  url={current[:90]!r}"
        )
        if resp.status_code in (301, 302, 303, 307, 308) and loc:
            current = loc if loc.startswith("http") else urljoin(current, loc)
            continue
        return current
    return current


def diag(*, email: str, password: str, proxy: str | None, impersonate: str, try_password: bool) -> int:
    from request_phase import (
        _create_session,
        _step_csrf,
        _step_auth_url,
        _get_sentinel_token,
        _step_authorize_continue,
        _common_headers,
        RequestPhaseError,
        USER_AGENT,
    )
    from user_agent_profile import (
        SEC_CH_UA as _SEC_CH_UA,
        SEC_CH_UA_MOBILE as _SEC_CH_UA_MOBILE,
        SEC_CH_UA_PLATFORM as _SEC_CH_UA_PLATFORM,
    )

    nav_headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Referer": "https://chatgpt.com/",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-User": "?1",
        "User-Agent": USER_AGENT,
        "sec-ch-ua": _SEC_CH_UA,
        "sec-ch-ua-mobile": _SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": _SEC_CH_UA_PLATFORM,
    }

    def _bootstrap(use_login_hint: bool) -> tuple[Any, str, str]:
        """Returns (session, device_id, landing). In từng hop GET authorize."""
        sess = _create_session(proxy=proxy, impersonate=impersonate)
        did = str(uuid.uuid4())
        csrf = _step_csrf(sess, _p)
        au = _step_auth_url(
            sess, csrf, _p, device_id=did,
            login_hint=email if use_login_hint else "",
        )
        _p(f"       authorize_url={au[:120]!r}")
        landing = _follow_manual(sess, au, nav_headers)
        did = sess.cookies.get("oai-did", "") or did
        _p(f"       jar={_jar(sess)}")
        return sess, did, landing

    masked = _mask_email(email)
    _p(f"[diag] start email={masked} proxy={'<set>' if proxy else 'DIRECT'} impersonate={impersonate} try_password={try_password}")

    # ── Probe A: login_hint ──────────────────────────────────────────
    _p("\n===== PROBE A — GET /authorize WITH login_hint =====")
    try:
        sess_a, did_a, land_a = _bootstrap(use_login_hint=True)
    except Exception as exc:  # noqa: BLE001
        _step("A", False, "bootstrap login_hint raised", repr(exc)[:200])
        return 2
    flow_a = (
        "password" if "/log-in/password" in land_a
        else "otp" if "/email-verification" in land_a
        else "UNDETERMINED"
    )
    _step("A", flow_a != "UNDETERMINED", f"landing flow={flow_a}", f"landing={land_a[:110]!r}")

    # ── Probe B: no login_hint ───────────────────────────────────────
    _p("\n===== PROBE B — GET /authorize NO login_hint =====")
    try:
        sess_b, did_b, land_b = _bootstrap(use_login_hint=False)
    except Exception as exc:  # noqa: BLE001
        _step("B", False, "bootstrap no-login_hint raised", repr(exc)[:200])
        return 2
    flow_b = (
        "password" if "/log-in/password" in land_b
        else "otp" if "/email-verification" in land_b
        else "UNDETERMINED"
    )
    _step("B", True, f"landing flow={flow_b}", f"landing={land_b[:110]!r}")

    # ── Probe C: authorize/continue trên session B ───────────────────
    _p("\n===== PROBE C — POST authorize/continue (session B, no login_hint) =====")
    try:
        sent = _get_sentinel_token(sess_b, did_b, "login", _p)
        _p(f"       sentinel_token={_mask_value(sent)}")
    except Exception as exc:  # noqa: BLE001
        _step("C", False, "sentinel raised", repr(exc)[:200])
        sent = ""

    # Gọi trực tiếp endpoint để bắt FULL body (kể cả 409) — không qua wrapper
    # raise sớm.
    headers = _common_headers("https://auth.openai.com/log-in")
    headers["Content-Type"] = "application/json"
    if sent:
        headers["openai-sentinel-token"] = sent
    if did_b:
        headers["oai-device-id"] = did_b
    payload = {"username": {"value": email, "kind": "email"}, "screen_hint": "login"}
    try:
        resp_c = sess_b.post(
            "https://auth.openai.com/api/accounts/authorize/continue",
            headers=headers, json=payload, timeout=30,
        )
        body_c = (resp_c.text or "")[:600]
        ok_c = resp_c.status_code == 200
        _step("C", ok_c, f"authorize/continue HTTP {resp_c.status_code}", body_c)
        _p(f"       set-cookie=[{_set_cookie_names(resp_c)}]  jar={_jar(sess_b)}")
    except Exception as exc:  # noqa: BLE001
        _step("C", False, "authorize/continue raised", repr(exc)[:200])

    # ── Probe D: password/verify trực tiếp trên session A (optional) ──
    if try_password:
        _p("\n===== PROBE D — POST password/verify (session A, login_hint) =====")
        try:
            sent_d = _get_sentinel_token(sess_a, did_a, "login", _p)
        except Exception:  # noqa: BLE001
            sent_d = ""
        h = _common_headers("https://auth.openai.com/log-in/password")
        h["Content-Type"] = "application/json"
        if did_a:
            h["oai-device-id"] = did_a
        if sent_d:
            h["openai-sentinel-token"] = sent_d
        try:
            resp_d = sess_a.post(
                "https://auth.openai.com/api/accounts/password/verify",
                headers=h, json={"password": password}, timeout=30,
            )
            _step(
                "D", resp_d.status_code == 200,
                f"password/verify HTTP {resp_d.status_code}",
                (resp_d.text or "")[:400],
            )
        except Exception as exc:  # noqa: BLE001
            _step("D", False, "password/verify raised", repr(exc)[:200])
    else:
        _p("\n[skip] PROBE D (password/verify) — dùng --try-password để bật.")

    _p("\n===== SUMMARY =====")
    _p(f"   A(login_hint) flow = {flow_a}")
    _p(f"   B(no_hint)    flow = {flow_b}")
    _p("   → Đọc body Probe C để biết server kỳ vọng gì khi 409 invalid_state.")
    return 0


def _parse_combo(combo: str) -> tuple[str, str, str | None]:
    parts = combo.split("|")
    if len(parts) < 2:
        raise SystemExit("combo phải dạng EMAIL|PASS hoặc EMAIL|PASS|SECRET")
    email, password = parts[0].strip(), parts[1].strip()
    secret = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
    return email, password, secret


def _normalize_proxy(raw: str | None) -> str | None:
    """Hỗ trợ format host:port:user:pass (như accounts/proxy pool) → URL."""
    if not raw:
        return None
    if "://" in raw:
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, pw = parts
        return f"http://{user}:{pw}@{host}:{port}"
    if len(parts) == 2:
        host, port = parts
        return f"http://{host}:{port}"
    return raw


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--combo", required=True, help="EMAIL|PASS|SECRET")
    ap.add_argument("--proxy", default=None, help="host:port:user:pass hoặc URL")
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--impersonate", default="chrome145")
    ap.add_argument("--try-password", action="store_true", help="Bật Probe D (RỦI RO lockout nếu sai pass).")
    args = ap.parse_args()

    email, password, _secret = _parse_combo(args.combo)
    proxy = None if args.no_proxy else _normalize_proxy(args.proxy)
    return diag(
        email=email, password=password, proxy=proxy,
        impersonate=args.impersonate, try_password=args.try_password,
    )


if __name__ == "__main__":
    raise SystemExit(main())
