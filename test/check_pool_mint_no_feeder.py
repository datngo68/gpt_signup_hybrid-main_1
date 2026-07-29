"""Verify POOL path mint `so` BÁM GOLDEN sau khi gỡ synthetic feeder (task 5.1).

Property 1 (NOT soTokenFromSyntheticEvents): đường mint `so` của pool path phải
cho KẾT QUẢ GIỐNG golden ``CamoufoxTokenGenerator`` trên CÙNG điều kiện (page
tĩnh frame.html, headless, KHÔNG synthetic event). Nghĩa là: nếu golden mint
được `so` non-empty thì pool cũng phải; nếu golden raise "returned nothing" thì
pool cũng raise — KHÔNG được dùng feeder để "vượt" golden (đó chính là drift B).

Cần Camoufox binary + network egress tới sentinel.openai.com. Không có → SKIP.

Chạy:  .venv/bin/python test/check_pool_mint_no_feeder.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MINT_FLOW = "oauth_signin"
DEVICE_ID = "00000000-0000-4000-8000-000000000001"


def _mint_outcome(mint_so_callable) -> tuple[str, str]:
    """Trả (kind, detail): kind ∈ {'nonempty','empty','raise'}."""
    from chatgpt_camoufox.chatgpt_camoufox.camoufox_vm import EnforcementError
    try:
        so = mint_so_callable(MINT_FLOW)
    except EnforcementError as exc:
        return ("raise", str(exc))
    if isinstance(so, str) and so:
        return ("nonempty", f"len={len(so)}")
    return ("empty", repr(so))


def _golden_outcome() -> tuple[str, str]:
    from chatgpt_camoufox.chatgpt_camoufox.camoufox_vm import CamoufoxTokenGenerator
    from chatgpt_camoufox.chatgpt_camoufox.fingerprint import profile_for_locale
    import random as _random
    profile = profile_for_locale(
        locale="en-US", firefox_major=135, platform="Windows", rng=_random.Random(),
    )
    gen = CamoufoxTokenGenerator(profile=profile, proxy=None, headless=True, insecure=False)
    try:
        gen.set_device_id(DEVICE_ID)
        gen.mint_token(MINT_FLOW)
        return _mint_outcome(gen.mint_so)
    finally:
        gen.close()


def _pool_outcome() -> tuple[str, str]:
    from reg_hybrid.browser_pool import _CamoufoxRunner
    runner = _CamoufoxRunner(proxy=None, headless=True, insecure=False, log=lambda m: None)
    handle = None
    try:
        handle = runner.acquire_context()
        handle.set_device_id(DEVICE_ID)
        handle.mint_token(MINT_FLOW)
        return _mint_outcome(handle.mint_so)
    finally:
        if handle is not None:
            try:
                runner.release_context(handle)
            except Exception:
                pass
        runner.shutdown()


def main() -> int:
    from chatgpt_camoufox.chatgpt_camoufox.camoufox_vm import camoufox_available
    if not camoufox_available():
        print("[SKIP] camoufox không khả dụng — cần verify pool path trên máy user.")
        return 2

    g_kind, g_detail = _golden_outcome()
    print(f"[golden] mint_so -> {g_kind} ({g_detail})")
    p_kind, p_detail = _pool_outcome()
    print(f"[pool]   mint_so -> {p_kind} ({p_detail})")

    print("=" * 70)
    # Parity: pool phải CÙNG KIND với golden (so bám golden, không từ feeder giả).
    parity = (g_kind == p_kind)
    if parity:
        print(f"[PASS] pool path mint_so BÁM GOLDEN (cùng outcome '{g_kind}') "
              f"— KHÔNG dùng synthetic feeder để vượt golden.")
        print("RESULT: PASS")
        return 0
    print(f"[FAIL] pool '{p_kind}' != golden '{g_kind}' — pool path vẫn lệch golden.")
    print("RESULT: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
