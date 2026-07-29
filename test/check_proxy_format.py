"""Verify web/proxy_format.py — parse proxy line + materialize {SID} placeholder.

Pure-sync, no-network. Convention: tNN → int, [PASS]/[FAIL], main() exit 0 = all pass.
Run: .venv/bin/python test/check_proxy_format.py
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]

# Load module trực tiếp qua file path — proxy_format thuần stdlib, không relative
# import → tránh kéo nguyên server stack (web/__init__ import nặng).
_spec = importlib.util.spec_from_file_location(
    "proxy_format", ROOT / "web" / "proxy_format.py"
)
_pf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pf)
gen_sid, has_template, mask_proxy, materialize_proxy = (
    _pf.gen_sid, _pf.has_template, _pf.mask_proxy, _pf.materialize_proxy
)


def t01_colon_basic() -> int:
    out = materialize_proxy("h:1:u:p")
    if out != "http://u:p@h:1":
        print(f"[FAIL] t01 colon basic :: got {out!r}", flush=True)
        return 1
    print("[PASS] t01 colon basic", flush=True)
    return 0


def t02_no_auth() -> int:
    out = materialize_proxy("h:1")
    if out != "http://h:1" or "@" in out:
        print(f"[FAIL] t02 no-auth :: got {out!r}", flush=True)
        return 1
    print("[PASS] t02 no-auth (no @)", flush=True)
    return 0


def t03_pass_with_colon() -> int:
    out = materialize_proxy("h:1:u:p:x")
    if out != "http://u:p%3Ax@h:1":
        print(f"[FAIL] t03 pass-with-colon :: got {out!r}", flush=True)
        return 1
    print("[PASS] t03 pass-with-colon (maxsplit=3 + quote)", flush=True)
    return 0


def t04_url_form_passthrough() -> int:
    out = materialize_proxy("http://u:p@h:1")
    if out != "http://u:p@h:1":
        print(f"[FAIL] t04 url passthrough :: got {out!r}", flush=True)
        return 1
    print("[PASS] t04 url passthrough", flush=True)
    return 0


def t05_url_form_scheme() -> int:
    out = materialize_proxy("socks5://u:p@h:1")
    if out != "socks5://u:p@h:1":
        print(f"[FAIL] t05 url scheme :: got {out!r}", flush=True)
        return 1
    print("[PASS] t05 url scheme kept", flush=True)
    return 0


def t06_sid_in_user() -> int:
    out = materialize_proxy("h:1:u-SID{SID}:p")
    if not re.match(r"^http://u-SID[a-z0-9]{8}:p@h:1$", out):
        print(f"[FAIL] t06 sid-in-user :: got {out!r}", flush=True)
        return 1
    print(f"[PASS] t06 sid-in-user :: {out}", flush=True)
    return 0


def t07_sid_in_pass() -> int:
    out = materialize_proxy("h:1:u:p-{SID}")
    if not re.match(r"^http://u:p-[a-z0-9]{8}@h:1$", out):
        print(f"[FAIL] t07 sid-in-pass :: got {out!r}", flush=True)
        return 1
    print(f"[PASS] t07 sid-in-pass :: {out}", flush=True)
    return 0


def t08_same_sid_both() -> int:
    out = materialize_proxy("h:1:u-{SID}:p-{SID}")
    # backref \1 lock 2 chỗ phải bằng nhau (1 SID chung)
    if not re.match(r"^http://u-([a-z0-9]{8}):p-\1@h:1$", out):
        print(f"[FAIL] t08 same-sid-both :: got {out!r}", flush=True)
        return 1
    print(f"[PASS] t08 same-sid-both :: {out}", flush=True)
    return 0


def t09_has_template() -> int:
    cases = [
        ("h:1:u-{SID}:p", True),
        ("h:1:u-{sid}:p", True),  # lowercase
        ("h:1:u:p", False),
        ("http://u:p@h:1", False),
    ]
    for line, expected in cases:
        if has_template(line) != expected:
            print(f"[FAIL] t09 has_template :: {line!r} expected {expected}", flush=True)
            return 1
    print("[PASS] t09 has_template (case-insensitive)", flush=True)
    return 0


def t10_gen_sid() -> int:
    a = gen_sid()
    b = gen_sid()
    if len(a) != 8 or not re.match(r"^[a-z0-9]{8}$", a):
        print(f"[FAIL] t10 gen_sid shape :: got {a!r}", flush=True)
        return 1
    if a == b:
        print(f"[FAIL] t10 gen_sid not random :: {a} == {b}", flush=True)
        return 1
    print("[PASS] t10 gen_sid len 8, [a-z0-9], random", flush=True)
    return 0


def t11_lazy_per_call() -> int:
    a = materialize_proxy("h:1:u-{SID}:p")
    b = materialize_proxy("h:1:u-{SID}:p")
    if a == b:
        print(f"[FAIL] t11 lazy per-call :: same SID {a}", flush=True)
        return 1
    print("[PASS] t11 lazy per-call (2 SID khác)", flush=True)
    return 0


def t12_invalid_raises() -> int:
    for bad in ("", "   ", "hostonly"):
        try:
            materialize_proxy(bad)
        except ValueError:
            continue
        print(f"[FAIL] t12 invalid :: {bad!r} did not raise ValueError", flush=True)
        return 1
    print("[PASS] t12 invalid → ValueError", flush=True)
    return 0


def t13_gen_sid_len() -> int:
    s = gen_sid(length=12)
    if len(s) != 12 or not re.match(r"^[a-z0-9]{12}$", s):
        print(f"[FAIL] t13 gen_sid len 12 :: got {s!r}", flush=True)
        return 1
    print("[PASS] t13 gen_sid length=12", flush=True)
    return 0


def t14_url_encode_at() -> int:
    out = materialize_proxy("h:1:u:p@ss")
    if out != "http://u:p%40ss@h:1":
        print(f"[FAIL] t14 url-encode @ :: got {out!r}", flush=True)
        return 1
    parsed = urlparse(out)
    if parsed.hostname != "h" or parsed.port != 1:
        print(f"[FAIL] t14 round-trip host :: {parsed.hostname}:{parsed.port}", flush=True)
        return 1
    if unquote(parsed.password or "") != "p@ss":
        print(f"[FAIL] t14 round-trip pass :: {parsed.password!r}", flush=True)
        return 1
    print("[PASS] t14 url-encode @ + round-trip urlparse", flush=True)
    return 0


def t15_mask_proxy() -> int:
    if mask_proxy("http://u-SIDabc12345:realpass@h:1") != "http://***@h:1":
        print("[FAIL] t15 mask creds", flush=True)
        return 1
    if mask_proxy("http://h:1") != "http://h:1":
        print("[FAIL] t15 mask no-@", flush=True)
        return 1
    if mask_proxy(None) != "direct":
        print("[FAIL] t15 mask None", flush=True)
        return 1
    # colon-form raw line (pool key) cũng phải mask creds → ***@host:port
    if mask_proxy("h:1:u:p") != "***@h:1":
        print(f"[FAIL] t15 mask colon-form :: {mask_proxy('h:1:u:p')!r}", flush=True)
        return 1
    if mask_proxy("h:1") != "h:1":
        print("[FAIL] t15 mask colon no-auth", flush=True)
        return 1
    print("[PASS] t15 mask_proxy (URL/colon-form/no-@/None)", flush=True)
    return 0


def t16_case_insensitive_sid() -> int:
    out = materialize_proxy("h:1:u-{sid}:p")
    if not re.match(r"^http://u-[a-z0-9]{8}:p@h:1$", out):
        print(f"[FAIL] t16 lowercase {{sid}} :: got {out!r}", flush=True)
        return 1
    # mixed case → cùng 1 SID
    out2 = materialize_proxy("h:1:u-{SID}-{sid}:p")
    m = re.match(r"^http://u-([a-z0-9]{8})-([a-z0-9]{8}):p@h:1$", out2)
    if not m or m.group(1) != m.group(2):
        print(f"[FAIL] t16 mixed-case same SID :: got {out2!r}", flush=True)
        return 1
    print(f"[PASS] t16 case-insensitive {{sid}}/{{SID}} same SID :: {out2}", flush=True)
    return 0


def main() -> int:
    print("=== check_proxy_format ===", flush=True)
    tests = [
        t01_colon_basic, t02_no_auth, t03_pass_with_colon, t04_url_form_passthrough,
        t05_url_form_scheme, t06_sid_in_user, t07_sid_in_pass, t08_same_sid_both,
        t09_has_template, t10_gen_sid, t11_lazy_per_call, t12_invalid_raises,
        t13_gen_sid_len, t14_url_encode_at, t15_mask_proxy, t16_case_insensitive_sid,
    ]
    failures = 0
    for fn in tests:
        try:
            rc = fn()
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {fn.__name__} :: raised {type(exc).__name__}: {exc}", flush=True)
            rc = 1
        if rc != 0:
            failures += 1
    print(f"=== done :: {len(tests) - failures}/{len(tests)} pass ===", flush=True)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
