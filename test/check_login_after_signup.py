"""Phase 11.7 login verify — POST /api/accounts/password/verify for each
recently signed-up account. ``invalid_username_or_password`` = account got
deactivated by anti-ban (the bug we're trying to fix).

Run: .venv/bin/python3 test/check_login_after_signup.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Hard-coded accounts to verify — must be email + password from recent runs.
ACCOUNTS: list[dict[str, str]] = [
    # Mode 1: pure_request (K2 + K2c, expected so_token=yes)
    {
        "email": "kappas-nobler-9s+0hwkm3@icloud.com",
        "password": "W2mq8fiivxq@",
        "mode": "pure_request",
        "user_id": "user-qmTxyNkwGnih0fCXcCouNOtm",
    },
    # Mode 2: browser
    {
        "email": "refit_garble.6c+bcaqau9@icloud.com",
        "password": "Xzh4leiy9kg@",
        "mode": "browser",
        "user_id": "user-bAWCcHAdLIUWk9OjdE8XcXwT",
    },
]


async def verify_login(acc: dict) -> dict:
    from session_phase import get_session_pure_request
    from mail_providers import build_provider_worker
    # Reuse iCloud HME worker for OTP polling (matches signup config).
    provider = build_provider_worker(
        logs_url="https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/logs",
        api_key="12345678@",
        insecure_tls=False,
    )
    result: dict = {"email": acc["email"], "mode": acc["mode"]}
    try:
        session = await get_session_pure_request(
            email=acc["email"],
            password=acc["password"],
            mail_provider=provider,
            login_flow="anti409",
            log=lambda m: print(f"  [{acc['email'][:30]}] {m}", flush=True),
        )
        result["success"] = True
        result["session_keys"] = sorted(session.keys()) if isinstance(session, dict) else []
        result["user_id_match"] = (
            isinstance(session, dict)
            and session.get("user", {}).get("id") == acc["user_id"]
        )
    except Exception as exc:
        msg = str(exc)
        result["success"] = False
        result["error_type"] = type(exc).__name__
        result["error"] = msg[:200]
        result["deactivated"] = (
            "invalid_username_or_password" in msg
            or "invalid_username" in msg
            or ("Login failed" in msg and "password verify" in msg)
        )
    return result


async def main() -> int:
    print(f"Verifying {len(ACCOUNTS)} accounts...\n", flush=True)
    results = []
    for acc in ACCOUNTS:
        print(f"\n=== {acc['email']} ({acc['mode']}) ===", flush=True)
        r = await verify_login(acc)
        results.append(r)
        if r["success"]:
            uid_ok = "✓" if r.get("user_id_match") else "?"
            print(
                f"  → SUCCESS — session keys: {r['session_keys']!r} "
                f"user_id_match={uid_ok}",
                flush=True,
            )
        else:
            deact = " DEACTIVATED" if r.get("deactivated") else ""
            print(
                f"  → FAIL{deact}: "
                f"{r.get('error_type')}: {r.get('error', '')[:160]}",
                flush=True,
            )
    print("\n──────── Summary ────────", flush=True)
    n_ok = sum(1 for r in results if r["success"])
    n_deact = sum(1 for r in results if r.get("deactivated"))
    print(f"OK:           {n_ok}/{len(results)}")
    print(f"DEACTIVATED:  {n_deact}/{len(results)}")
    for r in results:
        tag = "OK" if r["success"] else ("DEACT" if r.get("deactivated") else "FAIL")
        print(f"  [{tag}] {r['email']} ({r['mode']})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
