"""Verify fix triệt để bug UPI `invalid library` (BoringSSL state corrupt).

Kiểm tra:
  TC-01  AST parse web/upi_runner.py OK.
  TC-02  CurlOpt.FORBID_REUSE / FRESH_CONNECT tồn tại + _no_reuse_curl_options đúng.
  TC-03  _RotatingSession recover transient TLS error rồi success (trong quota).
  TC-04  Chain cạn KHÔNG dead-end — vẫn recreate với impersonate cuối.
  TC-05  Vượt _TLS_RECREATE_MAX_PER_CALL → raise (nhường approve loop xoay proxy).
  TC-06  Lỗi non-TLS propagate ngay, không recover.

Chạy: python3 test/check_upi_rotating_session.py
"""
from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

_FAILS = 0


def _log(ok: bool, tc: str, desc: str, detail: str = "") -> None:
    global _FAILS
    tag = "[PASS]" if ok else "[FAIL]"
    if not ok:
        _FAILS += 1
    print(f"{tag} {tc} — {desc} :: {detail}", flush=True)


_UPI_PATH = _ROOT / "web" / "upi_runner.py"


def tc01_ast() -> None:
    src = _UPI_PATH.read_text(encoding="utf-8")
    try:
        ast.parse(src)
        _log(True, "TC-01", "AST parse upi_runner.py", "syntax OK")
    except SyntaxError as exc:
        _log(False, "TC-01", "AST parse upi_runner.py", f"{exc}")


def tc02_curlopt() -> None:
    from curl_cffi.const import CurlOpt
    from web.upi_runner import _RotatingSession

    has_forbid = hasattr(CurlOpt, "FORBID_REUSE")
    has_fresh = hasattr(CurlOpt, "FRESH_CONNECT")
    opts = _RotatingSession._no_reuse_curl_options()
    ok = (
        has_forbid and has_fresh
        and opts.get(CurlOpt.FORBID_REUSE) == 1
        and opts.get(CurlOpt.FRESH_CONNECT) == 1
        and len(opts) == 2
    )
    _log(ok, "TC-02", "no-reuse curl_options",
         f"forbid={has_forbid} fresh={has_fresh} opts_len={len(opts)}")


_TLS_MSG = ("Failed to perform, curl: (35) TLS connect error: "
            "error:00000000:invalid library (0):OPENSSL_internal:invalid library (0)")


class _FakeInner:
    """Giả AsyncSession inner — post() fail TLS `counter['fail']` lần rồi OK."""

    def __init__(self, counter: dict) -> None:
        self._counter = counter

    async def __aexit__(self, *_a) -> bool:
        return False

    async def post(self, *_a, **_k):
        self._counter["calls"] += 1
        if self._counter["fail"] > 0:
            self._counter["fail"] -= 1
            raise RuntimeError(_TLS_MSG)
        return "OK"

    async def boom(self, *_a, **_k):
        self._counter["calls"] += 1
        raise ValueError("non-tls boom")


def _make_session(counter: dict):
    """_RotatingSession với _open_inner bị patch → cài _FakeInner (no network)."""
    from web.upi_runner import _RotatingSession

    rs = _RotatingSession(("chrome145", "chrome142", "chrome136"), log=lambda _m: None)

    async def _fake_open(impersonate: str) -> None:
        rs._inner = _FakeInner(counter)

    rs._open_inner = _fake_open  # instance attr shadow (an toàn, bắt đầu '_')
    return rs


async def _run_async() -> None:
    # TC-03 — recover 2 lần rồi success (quota max=4).
    counter = {"fail": 2, "calls": 0}
    rs = _make_session(counter)
    await rs.__aenter__()
    try:
        res = await rs.post("x")
        ok = res == "OK" and counter["calls"] == 3 and rs._idx == 2
        _log(ok, "TC-03", "recover transient TLS rồi success",
             f"res={res} calls={counter['calls']} idx={rs._idx}")
    finally:
        await rs.__aexit__(None, None, None)

    # TC-04 — chain cạn (3 fail → idx tới 2 rồi giữ) vẫn không dead-end, success.
    counter = {"fail": 3, "calls": 0}
    rs = _make_session(counter)
    await rs.__aenter__()
    try:
        res = await rs.post("x")
        ok = res == "OK" and rs._idx == 2 and counter["calls"] == 4
        _log(ok, "TC-04", "chain cạn không dead-end",
             f"res={res} idx={rs._idx} calls={counter['calls']}")
    finally:
        await rs.__aexit__(None, None, None)

    # TC-05 — fail vượt quota (max=4) → raise.
    from web.upi_runner import _TLS_RECREATE_MAX_PER_CALL
    counter = {"fail": _TLS_RECREATE_MAX_PER_CALL + 5, "calls": 0}
    rs = _make_session(counter)
    await rs.__aenter__()
    try:
        try:
            await rs.post("x")
            _log(False, "TC-05", "vượt quota phải raise", "không raise")
        except RuntimeError:
            ok = counter["calls"] == _TLS_RECREATE_MAX_PER_CALL + 1
            _log(ok, "TC-05", "vượt quota raise đúng",
                 f"calls={counter['calls']} quota={_TLS_RECREATE_MAX_PER_CALL}")
    finally:
        await rs.__aexit__(None, None, None)

    # TC-06 — non-TLS error propagate ngay (calls==1, không recover).
    counter = {"fail": 0, "calls": 0}
    rs = _make_session(counter)
    await rs.__aenter__()
    try:
        try:
            await rs._call_with_retry("boom", "x")
            _log(False, "TC-06", "non-TLS phải raise ngay", "không raise")
        except ValueError:
            ok = counter["calls"] == 1
            _log(ok, "TC-06", "non-TLS raise ngay, không recover",
                 f"calls={counter['calls']}")
    finally:
        await rs.__aexit__(None, None, None)


def main() -> int:
    print("=== UPI _RotatingSession fix verification ===", flush=True)
    tc01_ast()
    tc02_curlopt()
    asyncio.run(_run_async())
    print(f"=== done — {_FAILS} fail(s) ===", flush=True)
    return 1 if _FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
