"""Import thật mfa_phase + models để bắt NameError/import lỗi runtime.

Chỉ import + introspect chữ ký, KHÔNG gọi mạng.
"""
from __future__ import annotations

import inspect
import sys

print("[1/5] import models", flush=True)
from models import SignupRequest, BrowserHandoff, SignupResult  # noqa: E402

print("[2/5] import mfa_phase", flush=True)
import mfa_phase  # noqa: E402

print("[3/5] check entrypoints callable", flush=True)
assert callable(mfa_phase.enable_2fa_in_page), "enable_2fa_in_page không callable"
assert callable(mfa_phase.enable_2fa_in_session), "enable_2fa_in_session không callable"
assert inspect.iscoroutinefunction(mfa_phase.enable_2fa_in_page), "enable_2fa_in_page phải là async"
assert not inspect.iscoroutinefunction(mfa_phase.enable_2fa_in_session), "enable_2fa_in_session phải sync"
print("[PASS] entrypoints OK", flush=True)

print("[4/5] check SignupRequest.mfa_inline default True", flush=True)
req = SignupRequest(email="a@b.com")
assert req.mfa_inline is True, f"mfa_inline default phải True, got {req.mfa_inline}"
print("[PASS] mfa_inline default True", flush=True)

print("[5/5] check result model fields", flush=True)
res = SignupResult(success=True, email="a@b.com")
assert res.two_factor is None and res.two_factor_partial is None
ho = BrowserHandoff(state_param="s", device_id="d", auth_session_logging_id="l", callback_url="u")
assert ho.two_factor is None and ho.two_factor_partial is None
print("[PASS] model fields OK", flush=True)

# Smoke logic _classify_enroll_response (pure, no network)
print("[extra] _classify_enroll_response", flush=True)
import json as _json
ok = mfa_phase._classify_enroll_response(200, _json.dumps({"secret": "X", "factor": {"id": "f"}, "session_id": "s"}))
assert ok[0] == "ok", ok
conf = mfa_phase._classify_enroll_response(409, "factor already exists")
assert conf[0] == "conflict", conf
err = mfa_phase._classify_enroll_response(403, "<html>cloudflare</html>")
assert err[0] == "error", err
print("[PASS] classify logic OK", flush=True)

print("RESULT: ALL PASS", flush=True)
sys.exit(0)
