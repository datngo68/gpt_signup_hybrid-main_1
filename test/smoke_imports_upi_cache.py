"""Smoke import + smoke API surface cho UPI session cache.

Chạy: .venv/bin/python test/smoke_imports_upi_cache.py
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GSH_DB_PATH", str(ROOT / "runtime" / "data.db"))

_fails = 0


def _check(ok: bool, tag: str, desc: str, detail: str = "") -> None:
    global _fails
    status = "PASS" if ok else "FAIL"
    if not ok:
        _fails += 1
    print(f"[{status}] {tag} — {desc} :: {detail}", flush=True)


def main() -> int:
    # Import core modules.
    modules = [
        "web.upi_session_cache",
        "web.upi_runner",
        "web.manager",
        "web.server",
    ]
    for i, m in enumerate(modules, 1):
        try:
            importlib.import_module(m)
            _check(True, f"IMP-{i:02d}", f"import {m}", "ok")
        except Exception as exc:  # noqa: BLE001
            _check(False, f"IMP-{i:02d}", f"import {m}", f"{type(exc).__name__}: {exc}")

    # Smoke save/load/clear cycle với cache_dir tạm.
    from web.upi_session_cache import UpiSessionCache  # noqa: E402

    with tempfile.TemporaryDirectory() as tmp:
        cache = UpiSessionCache(instance_id="smoke", runtime_dir=Path(tmp))
        email = "alice+test@example.com"
        cookies = [
            {"name": "__Secure-next-auth.session-token", "value": "x" * 32, "domain": ".chatgpt.com"},
            {"name": "cf_clearance", "value": "y" * 16, "domain": ".chatgpt.com"},
        ]
        cache.save(email, cookies=cookies, access_token="ACC_TOKEN", proxy=None)
        rec = cache.load(email)
        _check(rec is not None and rec["access_token"] == "ACC_TOKEN",
               "CACHE-01", "save+load round-trip", f"rec={rec is not None}")

        # email-mismatch guard: load với email khác phải trả None ngay cả nếu file tồn tại.
        # Path đã hash theo email, nên file khác email → load() khác path → None tự nhiên.
        _check(cache.load("other@example.com") is None,
               "CACHE-02", "load email khác → None")

        # clear_all xoá hết.
        n = cache.clear_all()
        _check(n == 1 and cache.load(email) is None,
               "CACHE-03", "clear_all xoá hết", f"cleared={n}")

        # revalidate_and_load: TTL gate (record cũ giả lập).
        # Patch fetch_session_via_http qua monkeypatch.
        import web.upi_session_cache as upi_cache_mod

        async def _fake_fetch(*, cookies, proxy=None, timeout=30.0, impersonate=None):
            return {"accessToken": "REMINTED", "user": {"email": email}}

        cache.save(email, cookies=cookies, access_token="OLD", proxy=None)
        upi_cache_mod.fetch_session_via_http = _fake_fetch  # monkeypatch
        out = asyncio.run(cache.revalidate_and_load(email))
        _check(isinstance(out, dict) and out.get("accessToken") == "REMINTED"
               and "__cookies" in out,
               "CACHE-04", "revalidate_and_load OK → mint token mới + gắn __cookies")

        # Clear single record.
        ok = cache.clear(email)
        _check(ok and cache.load(email) is None,
               "CACHE-05", "clear(email) → file xoá")

        # revalidate miss khi không có record.
        out2 = asyncio.run(cache.revalidate_and_load(email))
        _check(out2 is None, "CACHE-06", "revalidate miss → None")

    print(f"\n=== {'ALL PASS' if _fails == 0 else str(_fails) + ' FAIL'} ===", flush=True)
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
