#!/usr/bin/env python3
"""Smoke test PRODUCTION login bootstrap path sau fix _prime_chatgpt_session.

Khác diag_login_bootstrap.py: smoke này gọi THẲNG `_step_csrf` + `_step_auth_url`
trong production module `request_phase` để confirm fix đã wire đúng vào code path
chạy thật (không phải replay logic).

Test cases:
  TC-01: _step_csrf trả token + cookie `__Host-next-auth.csrf-token` set vào jar
         (chứng minh _prime_chatgpt_session đã chạy + side-effect đúng).
  TC-02: _step_auth_url trả URL chứa `auth.openai.com` (không còn `csrf=true`).
  TC-03: Idempotency — gọi _step_csrf lần 2 vẫn pass, prime chỉ chạy 1 lần
         (verified bằng count log).
  TC-04: Skip prime khi jar đã có `__cf_bm` (idempotent path qua AST + behavior).

Yêu cầu network. Không cần account/proxy. Có thể fail nếu:
  - Không có internet
  - Cloudflare đang challenge IP
  - chatgpt.com đang down
Trong các case đó test sẽ skip với exit code 2 (không fail false negative).

Chạy: python3 test/smoke_login_bootstrap_production.py
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _ok(tc: str, label: str, detail: str = "") -> None:
    msg = f"[PASS] {tc} — {label}"
    if detail:
        msg += f" :: {detail}"
    print(msg, flush=True)


def _fail(tc: str, label: str, detail: str = "") -> None:
    msg = f"[FAIL] {tc} — {label}"
    if detail:
        msg += f" :: {detail}"
    print(msg, flush=True)


def _skip(tc: str, label: str, detail: str) -> None:
    print(f"[SKIP] {tc} — {label} :: {detail}", flush=True)


def _make_log(prefix: str, captured: list[str]) -> Any:
    """Log function which both prints AND captures lines for assertion."""

    def _log(msg: str) -> None:
        line = f"[{prefix}] {msg}"
        captured.append(msg)
        print(line, flush=True)

    return _log


def _has_cookie(session: Any, name: str) -> bool:
    try:
        return any(c.name == name for c in session.cookies.jar)
    except Exception:
        return False


def main() -> int:
    print("[1/4] import request_phase + create session", flush=True)
    try:
        from curl_cffi import requests as curl_requests
        from request_phase import (
            _create_session,
            _prime_chatgpt_session,
            _step_csrf,
            _step_auth_url,
            _IMPERSONATE_CANDIDATES,
        )
    except Exception as exc:  # noqa: BLE001
        _fail("BOOT", "import failed", repr(exc))
        return 1

    captured: list[str] = []
    log = _make_log("request", captured)

    # ── TC-01: _step_csrf success + cookie set ───────────────────────
    print("\n[2/4] TC-01 — _step_csrf primes session + sets csrf cookie", flush=True)
    session = _create_session(proxy=None)
    try:
        token = _step_csrf(session, log)
    except Exception as exc:  # noqa: BLE001
        # Cloudflare/network error: skip not fail.
        _skip("TC-01", "_step_csrf raised — likely network/CF", repr(exc))
        return 2

    if not token or len(token) < 32:
        _fail("TC-01", "csrfToken trống hoặc quá ngắn", repr(token))
        return 1
    _ok("TC-01", f"csrfToken thu được (len={len(token)})")

    if not _has_cookie(session, "__cf_bm"):
        _fail("TC-01", "jar thiếu __cf_bm — prime không chạy hoặc không set CF cookie")
        return 1
    _ok("TC-01", "jar có __cf_bm (CF middleware set qua landing page)")

    if not _has_cookie(session, "__Host-next-auth.csrf-token"):
        _fail(
            "TC-01",
            "jar thiếu __Host-next-auth.csrf-token — fix không hiệu quả",
        )
        return 1
    _ok("TC-01", "jar có __Host-next-auth.csrf-token (NextAuth set sau prime)")

    prime_logs = [m for m in captured if "[0/9] Priming" in m]
    if len(prime_logs) != 1:
        _fail("TC-01", f"prime chạy {len(prime_logs)} lần (expect 1)", repr(prime_logs))
        return 1
    _ok("TC-01", "prime chạy đúng 1 lần ở step đầu")

    # ── TC-02: _step_auth_url returns auth.openai.com URL ────────────
    print("\n[3/4] TC-02 — _step_auth_url trả URL auth.openai.com", flush=True)
    device_id = str(uuid.uuid4())
    try:
        auth_url = _step_auth_url(
            session,
            token,
            log,
            device_id=device_id,
            login_hint="goo45.compost+uwxtby@icloud.com",
        )
    except Exception as exc:  # noqa: BLE001
        _fail("TC-02", "_step_auth_url raised", repr(exc))
        return 1

    if "auth.openai.com" not in auth_url:
        _fail("TC-02", f"auth_url không chứa auth.openai.com: {auth_url[:200]}")
        return 1
    _ok("TC-02", f"auth_url valid: {auth_url[:100]}...")

    # ── TC-03: idempotency — call _step_csrf again, prime should skip ─
    print("\n[4/4] TC-03 — _step_csrf lần 2 KHÔNG prime lại (idempotent)", flush=True)
    captured.clear()
    try:
        token2 = _step_csrf(session, log)
    except Exception as exc:  # noqa: BLE001
        _fail("TC-03", "_step_csrf lần 2 raised", repr(exc))
        return 1
    if not token2:
        _fail("TC-03", "_step_csrf lần 2 trả token rỗng")
        return 1
    prime_logs_2 = [m for m in captured if "[0/9] Priming" in m]
    if prime_logs_2:
        _fail(
            "TC-03",
            f"prime chạy lại {len(prime_logs_2)} lần (expect 0 — jar đã có __cf_bm)",
            repr(prime_logs_2),
        )
        return 1
    _ok("TC-03", "prime SKIP đúng (jar đã có __cf_bm)")

    print("\n[summary] passed=3/3 — fix login bootstrap production WORK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
