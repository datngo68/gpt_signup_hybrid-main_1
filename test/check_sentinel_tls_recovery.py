#!/usr/bin/env python3
"""Verify sentinel TLS-library error recovery (curl: 35 invalid library).

Bug: sentinel /req dùng chung curl_cffi session với login. Khi đổi host
(chatgpt.com → sentinel.openai.com) hoặc concurrent jobs → BoringSSL corrupt
→ `curl: (35) ... OPENSSL_internal:invalid library`. Trước fix: catch → fallback
token rỗng (t="", c="") → anti-bot flag. Sau fix: retry trên session tươi.

Test cases:
    [TC-01] _is_tls_library_error nhận diện đúng các marker (pow + quickjs).
    [TC-02] _make_fresh_session tạo session mới + copy proxy (pow + quickjs).
    [TC-03] sentinel_pow._fetch_challenge: session gốc raise TLS → retry fresh
            session OK → trả challenge (không fallback).
    [TC-04] sentinel_pow._fetch_challenge: lỗi non-TLS → KHÔNG retry, return None.
    [TC-05] sentinel_quickjs._fetch_sentinel_challenge: session gốc raise TLS →
            retry fresh OK → trả payload.
    [TC-06] Fresh session exhausted (luôn raise TLS) → cuối cùng raise/return None
            (không loop vô hạn).

Chạy: python3 test/check_sentinel_tls_recovery.py
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _ok(tc, label, detail=""):
    msg = f"[PASS] {tc} — {label}"
    if detail:
        msg += f" :: {detail}"
    print(msg, flush=True)


def _fail(tc, label, detail=""):
    msg = f"[FAIL] {tc} — {label}"
    if detail:
        msg += f" :: {detail}"
    print(msg, flush=True)


_TLS_ERR_MSG = (
    "Failed to perform, curl: (35) TLS connect error: error:00000000:"
    "invalid library (0):OPENSSL_internal:invalid library (0)."
)


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {"token": "srv_tok", "proofofwork": {"required": False}}

    def json(self):
        return self._payload

    @property
    def text(self):
        return "fake"


class _FakeSession:
    """Session giả: raise TLS error N lần đầu, sau đó trả _FakeResp."""

    def __init__(self, *, fail_times=0, fail_forever=False, proxies=None, tag="orig"):
        self.fail_times = fail_times
        self.fail_forever = fail_forever
        self.calls = 0
        self.proxies = proxies or {}
        self.closed = False
        self.tag = tag

    def post(self, *args, **kwargs):
        self.calls += 1
        if self.fail_forever or self.calls <= self.fail_times:
            raise RuntimeError(_TLS_ERR_MSG)
        return _FakeResp()

    def close(self):
        self.closed = True


def t01_is_tls_error():
    print("[1/6] TC-01 — _is_tls_library_error nhận diện marker", flush=True)
    import sentinel_pow as sp
    import sentinel_quickjs as sq

    ok = True
    for mod, name in [(sp, "pow"), (sq, "quickjs")]:
        fn = mod._is_tls_library_error
        if not fn(RuntimeError(_TLS_ERR_MSG)):
            _fail("TC-01", f"{name}: không nhận diện invalid library")
            ok = False
        if not fn(RuntimeError("curl: (35) TLS connect error")):
            _fail("TC-01", f"{name}: không nhận diện curl: (35)")
            ok = False
        if fn(RuntimeError("HTTP 403 forbidden")):
            _fail("TC-01", f"{name}: false positive với lỗi HTTP thường")
            ok = False
    if ok:
        _ok("TC-01", "cả pow + quickjs nhận diện đúng TLS marker, không false-positive")
    return ok


def t02_make_fresh_session():
    print("[2/6] TC-02 — _make_fresh_session copy proxy", flush=True)
    import sentinel_pow as sp
    import sentinel_quickjs as sq

    template = _FakeSession(proxies={"https": "http://u:p@h:1", "http": "http://u:p@h:1"})
    ok = True
    for mod, name in [(sp, "pow"), (sq, "quickjs")]:
        try:
            fresh = mod._make_fresh_session(template)
        except Exception as exc:  # noqa: BLE001
            _fail("TC-02", f"{name}: _make_fresh_session raised", repr(exc)[:150])
            ok = False
            continue
        # Fresh phải là curl_cffi Session thật + copy proxy.
        if getattr(fresh, "proxies", None) != template.proxies:
            _fail("TC-02", f"{name}: proxy không copy đúng",
                  f"got {getattr(fresh, 'proxies', None)!r}")
            ok = False
        else:
            _ok("TC-02", f"{name}: fresh session copy proxy OK")
        try:
            fresh.close()
        except Exception:
            pass
    return ok


def t03_pow_retry_on_tls():
    print("[3/6] TC-03 — pow._fetch_challenge retry fresh session khi TLS error", flush=True)
    import sentinel_pow as sp

    # session gốc luôn raise TLS; fresh session (qua _make_fresh_session) trả OK.
    orig = _FakeSession(fail_forever=True, tag="orig")
    fresh_holder = {}

    def _fake_fresh(template):
        s = _FakeSession(fail_times=0, tag="fresh")  # OK ngay
        fresh_holder["s"] = s
        return s

    saved = sp._make_fresh_session
    sp._make_fresh_session = _fake_fresh
    try:
        result = sp._fetch_challenge(orig, "dev123", "login", "req_p_token")
    finally:
        sp._make_fresh_session = saved

    if result is None:
        _fail("TC-03", "fetch trả None dù fresh session OK")
        return False
    if result.get("token") != "srv_tok":
        _fail("TC-03", f"challenge sai: {result!r}")
        return False
    if not fresh_holder.get("s") or not fresh_holder["s"].closed:
        _fail("TC-03", "fresh session không được close sau dùng")
        return False
    _ok("TC-03", "retry fresh session → challenge OK + fresh closed")
    return True


def t04_pow_no_retry_on_non_tls():
    print("[4/6] TC-04 — pow._fetch_challenge KHÔNG retry lỗi non-TLS", flush=True)
    import sentinel_pow as sp

    class _HttpErrSession:
        def __init__(self):
            self.calls = 0
            self.proxies = {}

        def post(self, *args, **kwargs):
            self.calls += 1
            raise RuntimeError("connection reset by peer")  # non-TLS

    fresh_called = {"n": 0}

    def _fake_fresh(template):
        fresh_called["n"] += 1
        return _FakeSession()

    saved = sp._make_fresh_session
    sp._make_fresh_session = _fake_fresh
    sess = _HttpErrSession()
    try:
        result = sp._fetch_challenge(sess, "dev", "login", "p")
    finally:
        sp._make_fresh_session = saved

    if result is not None:
        _fail("TC-04", f"expect None, got {result!r}")
        return False
    if fresh_called["n"] != 0:
        _fail("TC-04", f"lỗi non-TLS vẫn retry fresh ({fresh_called['n']} lần)")
        return False
    if sess.calls != 1:
        _fail("TC-04", f"session gốc gọi {sess.calls} lần (expect 1)")
        return False
    _ok("TC-04", "lỗi non-TLS → return None ngay, không retry")
    return True


def t05_quickjs_retry_on_tls():
    print("[5/6] TC-05 — quickjs._fetch_sentinel_challenge retry fresh session", flush=True)
    import sentinel_quickjs as sq

    orig = _FakeSession(fail_forever=True, tag="orig")

    def _fake_fresh(template):
        return _FakeSession(fail_times=0, tag="fresh")

    saved = sq._make_fresh_session
    sq._make_fresh_session = _fake_fresh
    try:
        payload = sq._fetch_sentinel_challenge(
            orig, device_id="dev", flow="login", request_p="p", timeout_ms=20000,
        )
    except Exception as exc:  # noqa: BLE001
        sq._make_fresh_session = saved
        _fail("TC-05", "raised dù fresh session OK", repr(exc)[:150])
        return False
    sq._make_fresh_session = saved

    if not isinstance(payload, dict) or payload.get("token") != "srv_tok":
        _fail("TC-05", f"payload sai: {payload!r}")
        return False
    _ok("TC-05", "quickjs retry fresh session → payload OK")
    return True


def t06_exhausted_retries():
    print("[6/6] TC-06 — fresh session luôn TLS fail → bounded, không loop vô hạn", flush=True)
    import sentinel_pow as sp
    import sentinel_quickjs as sq

    # POW: luôn fail → return None (không raise, không loop).
    orig = _FakeSession(fail_forever=True)
    saved_sp = sp._make_fresh_session
    sp._make_fresh_session = lambda t: _FakeSession(fail_forever=True)
    try:
        result = sp._fetch_challenge(orig, "dev", "login", "p")
    finally:
        sp._make_fresh_session = saved_sp
    if result is not None:
        _fail("TC-06", f"pow expect None khi exhausted, got {result!r}")
        return False
    _ok("TC-06", "pow: exhausted → None (bounded)")

    # QuickJS: luôn fail → raise (caller fallback PoW). Bounded.
    orig2 = _FakeSession(fail_forever=True)
    saved_sq = sq._make_fresh_session
    sq._make_fresh_session = lambda t: _FakeSession(fail_forever=True)
    try:
        sq._fetch_sentinel_challenge(orig2, device_id="d", flow="login",
                                     request_p="p", timeout_ms=20000)
    except Exception:
        _ok("TC-06", "quickjs: exhausted → raise (bounded, caller fallback PoW)")
        sq._make_fresh_session = saved_sq
        return True
    finally:
        sq._make_fresh_session = saved_sq
    _fail("TC-06", "quickjs không raise khi exhausted")
    return False


def main():
    cases = [
        t01_is_tls_error,
        t02_make_fresh_session,
        t03_pow_retry_on_tls,
        t04_pow_no_retry_on_non_tls,
        t05_quickjs_retry_on_tls,
        t06_exhausted_retries,
    ]
    failures = 0
    for fn in cases:
        try:
            ok = fn()
        except Exception as exc:  # noqa: BLE001
            _fail(fn.__name__, "raised", repr(exc))
            ok = False
        if not ok:
            failures += 1
        print("", flush=True)
    print(f"[summary] passed={len(cases) - failures}/{len(cases)}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
