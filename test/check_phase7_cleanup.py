"""Phase 7 cleanup verify — dead code removed + persona wired + audit clean.

Chạy: .venv/bin/python3 test/check_phase7_cleanup.py
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    failures: list[str] = []

    # ─── Task 7.1 — Dead code removed ───
    print("── Task 7.1: Dead code removed ──")
    import request_phase
    src_req = (ROOT / "request_phase.py").read_text(encoding="utf-8")

    # _step_signup phải KHÔNG còn (xóa hẳn)
    if hasattr(request_phase, "_step_signup"):
        failures.append("_step_signup vẫn tồn tại — Phase 7 đã xóa")
        print("  [FAIL] _step_signup still exists")
    else:
        print("  [PASS] _step_signup deleted (no caller)")

    # _step_register_password phải KHÔNG còn
    if hasattr(request_phase, "_step_register_password"):
        failures.append("_step_register_password vẫn tồn tại")
        print("  [FAIL] _step_register_password still exists")
    else:
        print("  [PASS] _step_register_password deleted (no caller)")

    # _step_authorize_continue PHẢI giữ (session_phase login dùng)
    if hasattr(request_phase, "_step_authorize_continue"):
        print("  [PASS] _step_authorize_continue kept (session_phase login)")
    else:
        failures.append("_step_authorize_continue mất — login flow break")

    # _step_resend_otp PHẢI giữ (caller line ~1133)
    if hasattr(request_phase, "_step_resend_otp"):
        print("  [PASS] _step_resend_otp kept (sync flow caller exists)")
    else:
        failures.append("_step_resend_otp mất")

    # passwordless/send-otp fallback xóa — chỉ check session.post call (không phải comment)
    if 'session.post(\n            "https://auth.openai.com/api/accounts/passwordless' in src_req \
            or '"https://auth.openai.com/api/accounts/passwordless/send-otp"' in src_req:
        failures.append("passwordless/send-otp fallback (session.post) vẫn còn")
        print("  [FAIL] passwordless/send-otp session.post still exists")
    else:
        print("  [PASS] passwordless/send-otp fallback removed (chỉ-bot endpoint)")

    # ─── Task 7.2 — Persona wired vào _get_sentinel_token ───
    print("\n── Task 7.2: persona wire ──")
    sig = inspect.signature(request_phase._get_sentinel_token)
    if "persona" in sig.parameters:
        param = sig.parameters["persona"]
        if param.kind == inspect.Parameter.KEYWORD_ONLY and param.default is None:
            print("  [PASS] _get_sentinel_token(persona=None) keyword-only")
        else:
            failures.append("persona param wrong kind/default")

    # ─── Task 7.3 — CLI flag --persona ───
    print("\n── Task 7.3: CLI --persona ──")
    src_cli = (ROOT / "cli.py").read_text(encoding="utf-8")
    if "--persona" in src_cli and "firefox_mac" in src_cli:
        print("  [PASS] cli.py: --persona flag (default=firefox_mac)")
    else:
        failures.append("cli.py thiếu --persona flag")

    # SignupRequest field persona
    from models import SignupRequest

    req = SignupRequest(email="x@y.z")
    if req.persona == "firefox_mac":
        print(f"  [PASS] SignupRequest.persona default = {req.persona!r}")
    else:
        failures.append(f"SignupRequest.persona default = {req.persona!r}")

    req2 = SignupRequest(email="x@y.z", persona="chrome_win")
    if req2.persona == "chrome_win":
        print("  [PASS] SignupRequest.persona accept 'chrome_win'")
    else:
        failures.append(f"SignupRequest.persona reject 'chrome_win': {req2.persona}")

    # ─── Task 7.4 — Runtime warning pure_request ───
    print("\n── Task 7.4: pure_request warning ──")
    src_signup = (ROOT / "signup.py").read_text(encoding="utf-8")
    if "WARNING: pure_request signup KHÔNG gen được" in src_signup:
        print("  [PASS] signup.py runtime warning về so-token missing")
    else:
        failures.append("signup.py thiếu warning pure_request")

    # ─── Task 7.5 — Audit hardcode còn sót ───
    print("\n── Task 7.5: Audit clean ──")
    # request_phase phải KHÔNG còn hardcode `en-US,en;q=0.9` (đã thay persona)
    cnt = src_req.count('"en-US,en;q=0.9"')
    if cnt == 0:
        print("  [PASS] request_phase.py: 0 hardcode 'en-US,en;q=0.9'")
    else:
        failures.append(f"request_phase còn {cnt} hardcode en-US")

    # _navigate_headers helper exists
    if "_navigate_headers" in src_req:
        print("  [PASS] request_phase: _navigate_headers helper used")

    print()
    if failures:
        print(f"[FAIL] {len(failures)} failures:")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("[OK] All Phase 7 cleanup invariants pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
