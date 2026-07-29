#!/usr/bin/env python3
"""Verify fix: pure-request login capture được session_token cookie vào jar.

Tái dùng đúng code path đã sửa: ``_consume_callback`` (hop-by-hop) +
``_get_session_tokens`` (reassembly chunk) qua ``get_session_pure_request``.

KHÔNG tốn combo reg mới — login vào account vừa tạo ở smoke_reg_pure_request.
Credentials lấy từ output reg trước đó (account #1).

Chạy: python3 test/check_session_token_capture.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import load_settings
from mail_providers import build_provider_outlook
from session_phase import get_session_pure_request


# Account #1 đã reg thành công ở smoke_reg_pure_request.py (combo #1).
EMAIL = "gaffiganseanger337@hotmail.com"
PASSWORD = "Amlhve71xdg#"
OUTLOOK_COMBO = "gaffiganseanger337@hotmail.com|Wlj80a7H|M.C519_SN1.0.U.MsaArtifacts.-ChcFkYZUxrfveT9tbumF*JlKmDFIcVR1xG0Nd2NWmSMVh2*zeBYou7KJY5F4y6Qn*!Sat6x4VkxItMt6s2axHRvPVFvAjqRyUWbA2f3pdK20*Lbff56iAfM3Jyw6hmMyoVSdF5CxMZ9afLgDp6u!F5a6y3mqYNeswkgPZmNzv4DT2C!!j2lnjPbt!mtyCBgHUcMsuUTyCjgzuPrQ7OcM7urTrlXub0ZGcXpOkpZxHdrRtAqkHUvC5802v7T1nP1euZ2uMJ2IUZFwGslwwWkcHJrwcZXstN6DjOT2uB74x5rgtSZKbj53rvWfqtSW!gear*4P5MBou*IDAK8lhWlU1Bt!9A2vzFsbIHt6SeqomjHIuTYKN3s28JkLSySHDYSQVY!8ryvOapi!kKevN02NMEdiH8E3HjDQCEwN43Vgx8o71kjxtkHez9p7AtJPrOMBpAL6QpMrBwOnXKtU**KMR0A$|9e5f94bc-e8a4-4e73-b8be-63364c29d753"


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


async def main() -> int:
    settings = load_settings()
    provider = build_provider_outlook(
        combo=OUTLOOK_COMBO,
        state_dir=settings.runtime_dir / "outlook_state",
        proxy=None,
    )

    log(f"=== LOGIN verify session_token capture — {EMAIL} ===")
    try:
        data = await get_session_pure_request(
            email=EMAIL,
            password=PASSWORD,
            secret=None,
            proxy=None,
            mail_provider=provider,
            log=log,
        )
    except Exception as exc:
        log(f"[FAIL] login raised: {type(exc).__name__}: {exc}")
        return 1

    session_token_parts = {}
    access_token = data.get("accessToken") or ""
    cookies = data.get("__cookies") or []
    st_names = []
    for ck in cookies:
        name = ck.get("name") or ""
        if name == "__Secure-next-auth.session-token":
            session_token_parts[-1] = ck.get("value") or ""
            st_names.append(name)
        elif name.startswith("__Secure-next-auth.session-token."):
            try:
                session_token_parts[int(name.rsplit(".", 1)[-1])] = ck.get("value") or ""
            except ValueError:
                pass
            st_names.append(name)
    session_token = "".join(session_token_parts[k] for k in sorted(session_token_parts))

    log("=" * 60)
    log(f"  session-token cookies in jar: {st_names}")
    log(f"  session_token len={len(session_token)}")
    log(f"  access_token len={len(access_token)}")

    if session_token:
        log("[PASS] session_token CAPTURED vào jar — fix hoạt động")
        return 0
    log("[FAIL] session_token vẫn rỗng — fix CHƯA hiệu quả")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
