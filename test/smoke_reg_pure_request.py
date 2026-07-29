#!/usr/bin/env python3
"""Smoke test LIVE: reg ChatGPT qua pure_request (HTTP-only) với combo hotmail.

Mục đích: reproduce bug "reg pure http request không reg được" → bắt đúng error
ở step nào trong state machine request_phase.

Flow:
  - Parse combo hotmail → email + outlook combo (mail_provider='outlook').
  - Build SignupRequest(reg_mode='pure_request').
  - run_signup() → in realtime từng [request] [x/8] step.
  - In SignupResult cuối: success / error.

CẢNH BÁO: test này THẬT — tạo account ChatGPT thật + tiêu 1 OTP của combo.
Chạy: python3 test/smoke_reg_pure_request.py [combo_index]
  combo_index: 1..3 (default 1) — chọn combo nào trong COMBOS.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models import SignupRequest, SignupResult
from signup import run_signup


COMBOS = [
    "gaffiganseanger337@hotmail.com|Wlj80a7H|M.C519_SN1.0.U.MsaArtifacts.-ChcFkYZUxrfveT9tbumF*JlKmDFIcVR1xG0Nd2NWmSMVh2*zeBYou7KJY5F4y6Qn*!Sat6x4VkxItMt6s2axHRvPVFvAjqRyUWbA2f3pdK20*Lbff56iAfM3Jyw6hmMyoVSdF5CxMZ9afLgDp6u!F5a6y3mqYNeswkgPZmNzv4DT2C!!j2lnjPbt!mtyCBgHUcMsuUTyCjgzuPrQ7OcM7urTrlXub0ZGcXpOkpZxHdrRtAqkHUvC5802v7T1nP1euZ2uMJ2IUZFwGslwwWkcHJrwcZXstN6DjOT2uB74x5rgtSZKbj53rvWfqtSW!gear*4P5MBou*IDAK8lhWlU1Bt!9A2vzFsbIHt6SeqomjHIuTYKN3s28JkLSySHDYSQVY!8ryvOapi!kKevN02NMEdiH8E3HjDQCEwN43Vgx8o71kjxtkHez9p7AtJPrOMBpAL6QpMrBwOnXKtU**KMR0A$|9e5f94bc-e8a4-4e73-b8be-63364c29d753",
    "calladozeherquist653@hotmail.com|0VExXeTy17q|M.C543_SN1.0.U.MsaArtifacts.-ChqzC8P7e810YXU7FOw9fUdL*VEBPQAtzYQpCV*pQVHrs8gfApUQArfvuAFLbNDqVB!hp!SHSgICux9*cGM5mCIvyp7a0ZKXuoJe8OzFb3BHDHUp7jXEbcN89a2I827q7xe!zZE9Xs1emx1t1seqXSYbSor!lWJgzeQk5w0xUo8P*wayRY3W1AgAYe5G04WQELzoIdsNGo9iodq2cLs81in4cJYJ5CEQr13WrBG8hEsZr2aMvaQwn3gmqsx64zp9*j5yUapWgI!ixcWdDGoThKbXULFLwDpagLJTtgUbNdI2cURESGkc4NrIaTMPsFLOcyAjH*qUokAfzPs4v1x67qAHO6nwpKvQ*je9HGRAlQg9Ipe*7Ov0cNDWT!Zw3P!TlpWhQzF75Et4DJNCwvCF*oZKOhbtQsvoJ9IiQl3a7tWZ9KF5FG1DR9tfy2jxKITrPX1AOdVZvbtBvMRCNojbBsE$|9e5f94bc-e8a4-4e73-b8be-63364c29d753",
    "frederickmuehleisen711@hotmail.com|pCZBJMn3|M.C542_SN1.0.U.MsaArtifacts.-CnAk!FhSFKIKQO!bqfIXp4jr*yrtyj6VTqwoLcsfQ6eShVTX3n4xE2NXpi*UMYthNmuBYz3fmcV*YcHQNDjnkNnJx1rXrdsJ*8UHzChZX*6FuoOqKlbUMrS2TM4lIS1gRaZvRwZLuNmZjqEaxW2AlftzprwzFFzKWbziPZXmF9dPhtBJlrKdogbDZepHUrn2!AXfFSm3ZBkjC5pxm8P2DI3FNjHPJcEOw6H2nNcRc2TcOjwZofRHJ78DyH1oX5ykOdtdwkRanItvPX5Ezk077D7Vm1N4qNmgqivTZnkT4J2z9BPBVD85Xrb1G3NZiPceGNWnC*u1ST6gPM3AumKJda6e9Qzkqo1QYe4GiyGjEdknVd1O8Fhw6cljAz6Dtzy14VroWvX8YToICsn!2HClMIDyyawxb9JwaRSPWnhr9QB8zi8y1MBjFiNnkFH1cMuoLyq!yyLne537w1qfOa0sI5Q$|9e5f94bc-e8a4-4e73-b8be-63364c29d753",
]


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


async def main() -> int:
    idx = 1
    if len(sys.argv) > 1:
        try:
            idx = int(sys.argv[1])
        except ValueError:
            idx = 1
    idx = max(1, min(idx, len(COMBOS)))
    combo = COMBOS[idx - 1]
    email = combo.split("|", 1)[0].strip()

    log(f"=== SMOKE reg pure_request — combo #{idx} email={email} ===")

    request = SignupRequest(
        email=email,
        mail_provider="outlook",
        outlook_combo=combo,
        reg_mode="pure_request",
        otp_timeout_seconds=180.0,
        otp_poll_interval_seconds=5.0,
    )

    t0 = datetime.now()
    result: SignupResult = await run_signup(request, log=log)
    dt = (datetime.now() - t0).total_seconds()

    log("=" * 60)
    if result.success:
        log(f"[PASS] reg OK in {dt:.1f}s")
        log(f"  email={result.email}")
        log(f"  password={result.password}")
        log(f"  session_token={'yes' if result.session_token else 'no'} (len={len(result.session_token or '')})")
        log(f"  access_token={'yes' if result.access_token else 'no'} (len={len(result.access_token or '')})")
        log(f"  user_id={result.user_id}")
        st_cookies = [c.get("name") for c in (result.cookies or []) if "session-token" in (c.get("name") or "")]
        log(f"  session-token cookies in jar: {st_cookies}")
        return 0
    log(f"[FAIL] reg FAILED in {dt:.1f}s")
    log(f"  error={result.error}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
