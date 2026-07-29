"""Verify materialize_proxy handles credential-at form (user:pass@host:port).

Run: .venv/bin/python test/check_proxy_credential_at.py
"""
from __future__ import annotations

import sys
sys.path.insert(0, ".")

from web.proxy_format import materialize_proxy, mask_proxy


def t01_credential_at_user_pass() -> int:
    """user:pass@host:port → http://user:pass@host:port"""
    line = "9g5r1200918-region-IN-sid-TsM3NwMX-t-120:kpra1kp6@sg.cliproxy.io:3010"
    result = materialize_proxy(line)
    expected = "http://9g5r1200918-region-IN-sid-TsM3NwMX-t-120:kpra1kp6@sg.cliproxy.io:3010"
    if result != expected:
        print(f"[FAIL] t01 — got {result!r}, expected {expected!r}", flush=True)
        return 1
    print(f"[PASS] t01 — credential-at user:pass@host:port → {result}", flush=True)
    return 0


def t02_credential_at_user_only() -> int:
    """user@host:port → http://user@host:port"""
    line = "myuser@proxy.example.com:8080"
    result = materialize_proxy(line)
    expected = "http://myuser@proxy.example.com:8080"
    if result != expected:
        print(f"[FAIL] t02 — got {result!r}, expected {expected!r}", flush=True)
        return 1
    print(f"[PASS] t02 — credential-at user@host:port → {result}", flush=True)
    return 0


def t03_socks5_url_form() -> int:
    """socks5://host:port → passthrough"""
    line = "socks5://103.61.122.229:1080"
    result = materialize_proxy(line)
    if result != line:
        print(f"[FAIL] t03 — got {result!r}, expected {line!r}", flush=True)
        return 1
    print(f"[PASS] t03 — socks5 URL passthrough → {result}", flush=True)
    return 0


def t04_colon_form_unchanged() -> int:
    """host:port:user:pass — colon form vẫn hoạt động"""
    line = "sg.cliproxy.io:3010:myuser:mypass"
    result = materialize_proxy(line)
    expected = "http://myuser:mypass@sg.cliproxy.io:3010"
    if result != expected:
        print(f"[FAIL] t04 — got {result!r}, expected {expected!r}", flush=True)
        return 1
    print(f"[PASS] t04 — colon-form host:port:user:pass → {result}", flush=True)
    return 0


def t05_colon_form_pass_with_at() -> int:
    """host:port:user:p@ss — colon form, pass chứa @ → fallback colon path"""
    line = "h:80:u:p@ss"
    result = materialize_proxy(line)
    # '@' in pass URL-encoded → %40
    if "%40" not in result:
        print(f"[FAIL] t05 — expected %40 in result, got {result!r}", flush=True)
        return 1
    if not result.startswith("http://u:"):
        print(f"[FAIL] t05 — expected start with http://u:, got {result!r}", flush=True)
        return 1
    print(f"[PASS] t05 — colon-form pass with @ encoded → {result}", flush=True)
    return 0


def t06_mask_credential_at() -> int:
    """mask_proxy credential-at form"""
    line = "9g5r1200918:kpra1kp6@sg.cliproxy.io:3010"
    masked = mask_proxy(line)
    # Phải ẩn credentials
    if "kpra1kp6" in masked or "9g5r1200918" in masked:
        print(f"[FAIL] t06 — credentials leaked in mask: {masked!r}", flush=True)
        return 1
    if "sg.cliproxy.io" not in masked:
        print(f"[FAIL] t06 — host missing from mask: {masked!r}", flush=True)
        return 1
    print(f"[PASS] t06 — mask_proxy credential-at → {masked}", flush=True)
    return 0


def t07_credential_at_with_sid() -> int:
    """user-{sid}:pass@host:port → http://user-XXXXXXXX:pass@host:port"""
    line = "user-{sid}:pass@sg.cliproxy.io:3010"
    result = materialize_proxy(line)
    if not result.startswith("http://user-"):
        print(f"[FAIL] t07 — expected start http://user-, got {result!r}", flush=True)
        return 1
    if not result.endswith("@sg.cliproxy.io:3010"):
        print(f"[FAIL] t07 — expected end @sg.cliproxy.io:3010, got {result!r}", flush=True)
        return 1
    print(f"[PASS] t07 — credential-at with SID template → {result}", flush=True)
    return 0


def main() -> int:
    print("=== check_proxy_credential_at ===", flush=True)
    tests = [t01_credential_at_user_pass, t02_credential_at_user_only, t03_socks5_url_form,
             t04_colon_form_unchanged, t05_colon_form_pass_with_at, t06_mask_credential_at,
             t07_credential_at_with_sid]
    failures = sum(t() for t in tests)
    print(f"\n{'ALL PASS' if failures == 0 else f'{failures} FAILED'} ({len(tests)} tests)", flush=True)
    return failures


if __name__ == "__main__":
    sys.exit(main())
