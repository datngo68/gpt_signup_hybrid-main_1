"""Direct /api/accounts/password/verify test — bypasses OTP login flow.

Posts POST /api/accounts/password/verify with {"password": ...}. Decisive test
for password storage on server:
  - HTTP 200 → account ALIVE with correct password
  - HTTP 401 invalid_username_or_password → DEACTIVATED (or K2 password leaked)
  - HTTP 4xx anti-bot challenge → flagged but alive

Run: .venv/bin/python3 test/check_password_verify_direct.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ACCOUNTS: list[dict[str, str]] = [
    {
        "email": "cannier-17doting+k8thk@icloud.com",
        "password": "Ovdoh4v5sth#",
        "mode": "pure_request",
    },
    {
        "email": "entrees_privets_6s+9u8k2@icloud.com",
        "password": "J2mcghc61bg@",
        "mode": "pure_request",
    },
    {
        "email": "31_hollers.spikier+813dj@icloud.com",
        "password": "Egmfbrpg91x#",
        "mode": "pure_request",
    },
    {
        "email": "sherry-future-5l+6r1d1d@icloud.com",
        "password": "G8ddceyur75@",
        "mode": "pure_request",
    },
    {
        "email": "accents_jurist.0t+6hscm@icloud.com",
        "password": "R59866qxt9q#",
        "mode": "pure_request",
    },
]


def verify(acc: dict) -> dict:
    from request_phase import (
        _create_session, _step_csrf, _step_auth_url, _step_oauth_init,
        _common_headers, _get_sentinel_token,
    )
    import uuid

    result = {"email": acc["email"], "mode": acc["mode"]}
    session = _create_session(proxy=None)
    log_fn = lambda m: print(f"  [{acc['email'][:30]}] {m}", flush=True)
    try:
        # Prime + CSRF
        session.get(
            "https://chatgpt.com/auth/login", timeout=20,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        csrf = _step_csrf(session, log_fn)

        # Bootstrap authorize (need login_hint=email to get session state)
        auth_url = _step_auth_url(
            session, csrf, log_fn, login_hint=acc["email"],
        )
        device_id = _step_oauth_init(session, auth_url, log_fn)

        # Sentinel token for password_verify flow
        sentinel = _get_sentinel_token(
            session, device_id, "password_verify", log_fn,
        )

        # POST /api/accounts/password/verify
        headers = _common_headers("https://auth.openai.com/log-in/password")
        headers["Content-Type"] = "application/json"
        if device_id:
            headers["oai-device-id"] = device_id
        if sentinel:
            headers["openai-sentinel-token"] = sentinel
        resp = session.post(
            "https://auth.openai.com/api/accounts/password/verify",
            headers=headers,
            json={"password": acc["password"]},
            timeout=30,
        )
        result["status"] = resp.status_code
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text[:300]}
        result["body"] = body
        if resp.status_code == 200:
            result["verdict"] = "ALIVE"
        elif resp.status_code == 401:
            msg = str(body)
            if "invalid_username_or_password" in msg or "invalid_username" in msg:
                result["verdict"] = "DEACTIVATED"
            else:
                result["verdict"] = "UNKNOWN-401"
        else:
            result["verdict"] = f"HTTP-{resp.status_code}"
    except Exception as exc:
        result["verdict"] = "ERROR"
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            session.close()
        except Exception:
            pass
    return result


def main() -> int:
    print(f"Direct /password/verify test on {len(ACCOUNTS)} accounts...\n", flush=True)
    results = []
    for acc in ACCOUNTS:
        print(f"\n=== {acc['email']} ({acc['mode']}) ===", flush=True)
        r = verify(acc)
        results.append(r)
        print(f"  → verdict={r['verdict']}", flush=True)
        if "status" in r:
            print(f"     HTTP {r['status']}: {json.dumps(r.get('body'))[:200]}", flush=True)
        if "error" in r:
            print(f"     {r['error'][:200]}", flush=True)

    print("\n──────── Summary ────────", flush=True)
    for r in results:
        print(f"  [{r['verdict']:>14s}] {r['email']} ({r['mode']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
