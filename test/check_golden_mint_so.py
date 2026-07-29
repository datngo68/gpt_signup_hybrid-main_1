"""Probe: GOLDEN CamoufoxTokenGenerator.mint_so trên static page headless.

Mục tiêu: xác định golden có mint được `so` non-empty trên page tĩnh frame.html
headless KHÔNG feeder hay không — để đối chiếu với pool path sau khi gỡ feeder.

Chạy:  .venv/bin/python test/check_golden_mint_so.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MINT_FLOW = "oauth_signin"
DEVICE_ID = "00000000-0000-4000-8000-000000000001"


def main() -> int:
    from chatgpt_camoufox.chatgpt_camoufox.camoufox_vm import (
        CamoufoxTokenGenerator, camoufox_available, EnforcementError,
    )
    from chatgpt_camoufox.chatgpt_camoufox.fingerprint import profile_for_locale
    import random as _random

    if not camoufox_available():
        print("[SKIP] camoufox không khả dụng")
        return 2

    profile = profile_for_locale(
        locale="en-US", firefox_major=135, platform="Windows",
        rng=_random.Random(),
    )
    gen = CamoufoxTokenGenerator(
        profile=profile, proxy=None, headless=True, insecure=False,
    )
    try:
        gen.set_device_id(DEVICE_ID)
        tok = gen.mint_token(MINT_FLOW)
        print(f"[golden mint_token] p={len(tok.p)} t={len(tok.t)} c={len(tok.c)}")
        try:
            so = gen.mint_so(MINT_FLOW)
            print(f"[golden mint_so] so_len={len(so)} sample={so[:24]!r}")
            print("[RESULT] GOLDEN mint_so NON-EMPTY trên static page" if so
                  else "[RESULT] GOLDEN mint_so EMPTY (string rỗng)")
        except EnforcementError as exc:
            print(f"[RESULT] GOLDEN mint_so RAISE: {exc}")
    finally:
        gen.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
