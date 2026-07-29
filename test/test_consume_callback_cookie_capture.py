#!/usr/bin/env python3
"""Unit test (offline) cho fix capture session-token cookie ở pure-request.

Verify 2 hàm đã sửa trong request_phase.py:
  - _consume_callback: follow redirect hop-by-hop (allow_redirects=False) →
    capture Set-Cookie của MỖI response vào jar (kể cả hop trung gian).
  - _read_session_token_cookie: ghép chunk .0/.1 khi NextAuth split JWT > 4KB.

Dùng fake curl session deterministic — không gọi mạng, không tốn combo.

Chạy: python3 test/test_consume_callback_cookie_capture.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from request_phase import _consume_callback, _read_session_token_cookie

CALLBACK = "https://chatgpt.com/api/auth/callback/openai?code=abc123&state=xyz"


def _log(msg: str) -> None:
    print(f"  {msg}", flush=True)


# ── Fake curl_cffi session ────────────────────────────────────────────


class _FakeCookie:
    def __init__(self, name: str, value: str) -> None:
        self.name = name
        self.value = value


class _FakeJar:
    """Mimic curl_cffi Cookies: .get(name, default) + iterable of Cookie objs."""

    def __init__(self) -> None:
        self._d: dict[str, str] = {}

    def get(self, name: str, default: str = "") -> str:
        return self._d.get(name, default)

    def set(self, name: str, value: str) -> None:
        self._d[name] = value

    def __iter__(self):
        for n, v in self._d.items():
            yield _FakeCookie(n, v)


class _FakeResp:
    def __init__(self, status: int, location: str = "") -> None:
        self.status_code = status
        self.headers = {"Location": location} if location else {}


class _FakeSession:
    """hops: list[(status, location, cookies_to_set: dict)] — 1 entry / GET."""

    def __init__(self, hops: list[tuple[int, str, dict]]) -> None:
        self.cookies = _FakeJar()
        self._hops = hops
        self._i = 0

    def get(self, url, headers=None, timeout=None, allow_redirects=False):
        assert allow_redirects is False, "fix phải dùng allow_redirects=False"
        if self._i >= len(self._hops):
            return _FakeResp(200, "")
        status, location, set_cookies = self._hops[self._i]
        self._i += 1
        for n, v in set_cookies.items():
            self.cookies.set(n, v)
        return _FakeResp(status, location)


# ── Test cases ────────────────────────────────────────────────────────


def tc01_chunked_on_intermediate_hop() -> bool:
    """TC-01: cookie chunked .0/.1 set ở hop callback (302) → capture + ghép."""
    sess = _FakeSession([
        (302, "https://chatgpt.com/", {
            "__Secure-next-auth.session-token.0": "AAAA",
            "__Secure-next-auth.session-token.1": "BBBB",
        }),
        (200, "", {}),
    ])
    ok = _consume_callback(sess, CALLBACK, _log)
    token = _read_session_token_cookie(sess)
    passed = ok and token == "AAAABBBB"
    print(f"[{'PASS' if passed else 'FAIL'}] TC-01 — chunked intermediate hop "
          f":: consumed={ok} token={token!r}", flush=True)
    return passed


def tc02_base_cookie_later_hop() -> bool:
    """TC-02: cookie tên gốc set ở hop thứ 2 (relative Location) → vẫn capture."""
    sess = _FakeSession([
        (302, "/auth/next", {}),  # hop 1: chưa set cookie
        (302, "https://chatgpt.com/", {
            "__Secure-next-auth.session-token": "JWT_FULL_VALUE",
        }),
        (200, "", {}),
    ])
    ok = _consume_callback(sess, CALLBACK, _log)
    token = _read_session_token_cookie(sess)
    passed = ok and token == "JWT_FULL_VALUE"
    print(f"[{'PASS' if passed else 'FAIL'}] TC-02 — base cookie later hop "
          f":: consumed={ok} token={token!r}", flush=True)
    return passed


def tc03_no_cookie_returns_false() -> bool:
    """TC-03: không hop nào set cookie → consumed=False, token rỗng."""
    sess = _FakeSession([
        (302, "https://chatgpt.com/", {}),
        (200, "", {}),
    ])
    ok = _consume_callback(sess, CALLBACK, _log)
    token = _read_session_token_cookie(sess)
    passed = (ok is False) and token == ""
    print(f"[{'PASS' if passed else 'FAIL'}] TC-03 — no cookie set "
          f":: consumed={ok} token={token!r}", flush=True)
    return passed


def tc04_invalid_callback_url() -> bool:
    """TC-04: callback URL thiếu code= → return False ngay, không GET."""
    sess = _FakeSession([])
    ok = _consume_callback(sess, "https://chatgpt.com/no-code", _log)
    passed = ok is False
    print(f"[{'PASS' if passed else 'FAIL'}] TC-04 — invalid callback (no code=) "
          f":: consumed={ok}", flush=True)
    return passed


def tc05_base_priority_over_chunks() -> bool:
    """TC-05: _read ưu tiên cookie tên gốc nếu có (không ghép chunk thừa)."""
    jar_sess = _FakeSession([])
    jar_sess.cookies.set("__Secure-next-auth.session-token", "BASE")
    jar_sess.cookies.set("__Secure-next-auth.session-token.0", "X")
    token = _read_session_token_cookie(jar_sess)
    passed = token == "BASE"
    print(f"[{'PASS' if passed else 'FAIL'}] TC-05 — base ưu tiên hơn chunk "
          f":: token={token!r}", flush=True)
    return passed


def main() -> int:
    print("[run] test_consume_callback_cookie_capture", flush=True)
    results = [
        tc01_chunked_on_intermediate_hop(),
        tc02_base_cookie_later_hop(),
        tc03_no_cookie_returns_false(),
        tc04_invalid_callback_url(),
        tc05_base_priority_over_chunks(),
    ]
    passed = sum(results)
    total = len(results)
    print(f"\n[summary] passed={passed}/{total}", flush=True)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
