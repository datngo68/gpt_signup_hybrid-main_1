"""Phase 3 verify — Sentinel persona forwarding (Task 3.2) + _dd_s cookie (3.5).

Chạy: .venv/bin/python3 test/check_sentinel_persona_dd_s.py
"""
from __future__ import annotations

import inspect
import re
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    failures: list[str] = []

    # ─── Task 3.2 — Sentinel persona forwarding ───
    print("── Task 3.2: Sentinel persona forwarding ──")

    from sentinel_quickjs import (
        get_sentinel_token_via_quickjs,
        _ensure_sdk_file,
        _fetch_sentinel_challenge,
    )
    from sentinel_pow import (
        get_sentinel_token,
        _fetch_challenge,
    )
    from user_agent_profile import (
        BrowserPersona,
        CHROME_145_WIN,
        FIREFOX_135_MAC,
    )

    # Signature: persona param (kw-only)
    sig_quickjs = inspect.signature(get_sentinel_token_via_quickjs)
    if "persona" in sig_quickjs.parameters:
        param = sig_quickjs.parameters["persona"]
        if param.kind == inspect.Parameter.KEYWORD_ONLY \
                and param.default is None:
            print("  [PASS] get_sentinel_token_via_quickjs(persona=None) keyword-only")
        else:
            failures.append(
                f"persona param wrong: kind={param.kind}, default={param.default!r}"
            )
    else:
        failures.append("get_sentinel_token_via_quickjs missing persona param")

    sig_pow = inspect.signature(get_sentinel_token)
    if "persona" in sig_pow.parameters:
        param = sig_pow.parameters["persona"]
        if param.kind == inspect.Parameter.KEYWORD_ONLY:
            print("  [PASS] get_sentinel_token(persona=...) keyword-only")
    else:
        failures.append("get_sentinel_token missing persona param")

    # _ensure_sdk_file accept persona
    sig_sdk = inspect.signature(_ensure_sdk_file)
    if "persona" in sig_sdk.parameters:
        print("  [PASS] _ensure_sdk_file accept persona")
    else:
        failures.append("_ensure_sdk_file missing persona")

    sig_fetch = inspect.signature(_fetch_sentinel_challenge)
    if "persona" in sig_fetch.parameters:
        print("  [PASS] _fetch_sentinel_challenge accept persona")
    else:
        failures.append("_fetch_sentinel_challenge missing persona")

    sig_pow_fetch = inspect.signature(_fetch_challenge)
    if "persona" in sig_pow_fetch.parameters:
        print("  [PASS] sentinel_pow._fetch_challenge accept persona")
    else:
        failures.append("sentinel_pow._fetch_challenge missing persona")

    # Source: sentinel_quickjs imports BrowserPersona
    src_qjs = (ROOT / "sentinel_quickjs.py").read_text(encoding="utf-8")
    if "BrowserPersona" in src_qjs and "_navigator_payload(persona)" in src_qjs:
        print("  [PASS] sentinel_quickjs imports BrowserPersona + forward persona")
    else:
        failures.append("sentinel_quickjs missing BrowserPersona import or forward")

    src_pow = (ROOT / "sentinel_pow.py").read_text(encoding="utf-8")
    if "BrowserPersona" in src_pow and "persona=p" in src_pow:
        print("  [PASS] sentinel_pow imports BrowserPersona + forward")
    else:
        failures.append("sentinel_pow missing BrowserPersona import or forward")

    # ─── Task 3.5 — _dd_s cookie ───
    print("\n── Task 3.5: Datadog _dd_s cookie ──")

    from _datadog_session import gen_dd_s_cookie, inject_dd_s

    # gen_dd_s_cookie format
    val = gen_dd_s_cookie()
    # Format: aid=UUID&rum=0&id=UUID&created=DIGITS&expire=DIGITS
    pattern = re.compile(
        r"^aid=[0-9a-f-]{36}&rum=0&id=[0-9a-f-]{36}&created=\d+&expire=\d+$"
    )
    if pattern.match(val):
        print(f"  [PASS] gen_dd_s_cookie format: {val[:60]}...")
    else:
        failures.append(f"gen_dd_s_cookie format wrong: {val}")

    # rum=2 variant
    val2 = gen_dd_s_cookie(rum=2)
    if "&rum=2&" in val2:
        print("  [PASS] gen_dd_s_cookie(rum=2) → authenticated")
    else:
        failures.append(f"rum=2 not in: {val2}")

    # Reject invalid rum
    try:
        gen_dd_s_cookie(rum=1)
        failures.append("rum=1 should reject")
    except ValueError:
        print("  [PASS] gen_dd_s_cookie(rum=1) raise ValueError")

    # expire = created + 15min (15*60*1000 ms)
    parts = dict(p.split("=", 1) for p in val.split("&"))
    expire_ms = int(parts["expire"])
    created_ms = int(parts["created"])
    delta_min = (expire_ms - created_ms) / 1000 / 60
    if 14.9 <= delta_min <= 15.1:
        print(f"  [PASS] expire delta = {delta_min:.2f}min (≈ 15min)")
    else:
        failures.append(f"expire delta wrong: {delta_min}")

    # inject_dd_s với mock session
    class _MockJar:
        def __init__(self) -> None:
            self.cookies: dict = {}

        def get(self, name: str):
            return self.cookies.get(name)

        def set(self, name: str, value: str, *, domain: str = "", path: str = "/"):
            self.cookies[name] = (value, domain, path)

    class _MockSession:
        def __init__(self) -> None:
            self.cookies = _MockJar()

    sess = _MockSession()
    ok = inject_dd_s(sess)
    if ok and "_dd_s" in sess.cookies.cookies:
        v, d, _ = sess.cookies.cookies["_dd_s"]
        if d == ".chatgpt.com" and pattern.match(v):
            print(f"  [PASS] inject_dd_s set cookie domain=.chatgpt.com")
        else:
            failures.append(f"inject domain/value wrong: {d}, {v[:40]}")

    # Idempotent: lần 2 không overwrite
    ok2 = inject_dd_s(sess)
    if not ok2:
        print("  [PASS] inject_dd_s idempotent (skip if existing)")
    else:
        failures.append("inject_dd_s không idempotent — overwrite mặc định")

    # overwrite=True replace
    ok3 = inject_dd_s(sess, overwrite=True)
    if ok3:
        print("  [PASS] inject_dd_s overwrite=True replace")

    # request_phase.py wire
    src_req = (ROOT / "request_phase.py").read_text(encoding="utf-8")
    if "from _datadog_session import inject_dd_s" in src_req:
        print("  [PASS] request_phase wire inject_dd_s")
    else:
        failures.append("request_phase chưa wire inject_dd_s")

    print()
    if failures:
        print(f"[FAIL] {len(failures)} failures:")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("[OK] All Phase 3 (Task 3.2 + 3.5) invariants pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
