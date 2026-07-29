"""Behavioral test cho UpiJobManager.check_plan — live entitlement + fallback.

Đây KHÔNG phải pytest (image không có pytest/pytest-asyncio) và KHÔNG phải
harness static `tNN→int` (check_upi_plan_check.py chỉ static-grep). File này
exercise luồng THẬT của check_plan với network stub để bắt regression wiring
live/fallback — thứ mà static-grep không thấy.

Chạy:
    python test/test_check_plan_live.py        # (trong container/venv có deps)

Monkeypatch: check_plan dùng `from ..session_phase import ...` (local import lúc
gọi) nên gán attr lên module `gpt_signup_hybrid_new.session_phase` là đủ override.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

import gpt_signup_hybrid_new.session_phase as sp  # noqa: E402
from gpt_signup_hybrid_new.session_phase import SessionError  # noqa: E402
from gpt_signup_hybrid_new.web.manager import UpiJob, UpiJobManager  # noqa: E402

_REQUIRED_KEYS = {"ok", "plan", "is_plus", "expires", "checked_at", "error"}


def _make_job(*, cookies=True, token="eyJlogintoken"):
    job = UpiJob(id="t", email="a@b.c", password="p")
    if cookies:
        job._session_cookies = [{"name": "x", "value": "y", "domain": "chatgpt.com"}]
    job._access_token = token
    return job


def _install(um, job):
    um.jobs[job.id] = job
    um.order.append(job.id)


def _set_stubs(*, session_ret=None, session_exc=None, ent_ret=None, ent_exc=None):
    """Gán stub lên module session_phase (check_plan import từ đây lúc gọi)."""
    calls = {"session": 0, "entitlement": 0}

    async def stub_session(**kwargs):
        calls["session"] += 1
        if session_exc is not None:
            raise session_exc
        return session_ret

    async def stub_entitlement(**kwargs):
        calls["entitlement"] += 1
        if ent_exc is not None:
            raise ent_exc
        return ent_ret

    sp.fetch_session_via_http = stub_session
    sp.fetch_account_entitlement = stub_entitlement
    return calls


def t12_check_plan_uses_live() -> int:
    # Session cache nói "free" (stale) nhưng entitlement live nói "plus".
    _set_stubs(
        session_ret={
            "accessToken": "eyJsessiontoken",
            "expires": "2026-07-17T00:00:00Z",
            "account": {"planType": "free"},
        },
        ent_ret={"plan": "plus", "is_plus": True,
                 "has_active_subscription": True, "expires": "2026-08-01"},
    )
    um = UpiJobManager(max_concurrent=1)
    job = _make_job()
    _install(um, job)
    res = asyncio.run(um.check_plan("t"))

    if set(res.keys()) != _REQUIRED_KEYS:
        print(f"[FAIL] t12 :: dict keys đổi {set(res.keys())}", flush=True)
        return 1
    if not (res["ok"] is True and res["plan"] == "plus" and res["is_plus"] is True):
        print(f"[FAIL] t12 :: live không thắng cache → {res!r}", flush=True)
        return 1
    # expires = session-expiry (M5), KHÔNG phải subscription expires_at.
    if res["expires"] != "2026-07-17T00:00:00Z":
        print(f"[FAIL] t12 :: expires sai nguồn (phải session, got {res['expires']!r})", flush=True)
        return 1
    if job.plan_check is not res:
        print("[FAIL] t12 :: không cache vào job.plan_check", flush=True)
        return 1
    print("[PASS] t12 :: live entitlement thắng session cache + expires=session + cached", flush=True)
    return 0


def t13_check_plan_fallback() -> int:
    # Live raise SessionError → fallback đọc plan từ session cache (free).
    calls = _set_stubs(
        session_ret={
            "accessToken": "eyJsessiontoken",
            "expires": "2026-07-17T00:00:00Z",
            "account": {"planType": "free"},
        },
        ent_exc=SessionError("HTTP 403"),
    )
    um = UpiJobManager(max_concurrent=1)
    job = _make_job()
    _install(um, job)
    res = asyncio.run(um.check_plan("t"))

    if not (res["ok"] is True and res["plan"] == "free" and res["is_plus"] is False):
        print(f"[FAIL] t13 :: fallback sai → {res!r}", flush=True)
        return 1
    if calls["entitlement"] != 1:
        print(f"[FAIL] t13 :: entitlement phải được gọi 1 lần, got {calls['entitlement']}", flush=True)
        return 1
    # Live fail KHÔNG raise ra ngoài (fail-soft giữ nguyên).
    print("[PASS] t13 :: live fail → fallback session free, không raise", flush=True)
    return 0


def t14_check_plan_no_cookies() -> int:
    # Thiếu cookies → fail-soft trước khi fetch (regression guard).
    calls = _set_stubs(session_ret={}, ent_ret={})
    um = UpiJobManager(max_concurrent=1)
    job = _make_job(cookies=False)
    _install(um, job)
    res = asyncio.run(um.check_plan("t"))

    if res["ok"] is not False or "cookies" not in (res["error"] or ""):
        print(f"[FAIL] t14 :: thiếu cookies phải fail-soft → {res!r}", flush=True)
        return 1
    if calls["session"] != 0 or calls["entitlement"] != 0:
        print(f"[FAIL] t14 :: không được fetch khi thiếu cookies, got {calls}", flush=True)
        return 1
    print("[PASS] t14 :: thiếu cookies → fail-soft, không fetch", flush=True)
    return 0


def t15_plus_label_only() -> int:
    # Live trả Pro active → strict Plus-only ⇒ is_plus False (badge không xanh).
    _set_stubs(
        session_ret={"accessToken": "eyJ", "expires": None, "account": {"planType": "free"}},
        ent_ret={"plan": "pro", "is_plus": False,
                 "has_active_subscription": True, "expires": None},
    )
    um = UpiJobManager(max_concurrent=1)
    job = _make_job()
    _install(um, job)
    res = asyncio.run(um.check_plan("t"))
    if not (res["ok"] is True and res["plan"] == "pro" and res["is_plus"] is False):
        print(f"[FAIL] t15 :: Pro active không được tính Plus → {res!r}", flush=True)
        return 1
    print("[PASS] t15 :: Pro active → is_plus False (strict Plus-only)", flush=True)
    return 0


def main() -> int:
    print("=== test_check_plan_live ===", flush=True)
    tests = [
        t12_check_plan_uses_live,
        t13_check_plan_fallback,
        t14_check_plan_no_cookies,
        t15_plus_label_only,
    ]
    fails = 0
    for t in tests:
        try:
            rc = t()
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {t.__name__} :: exception {exc!r}", flush=True)
            rc = 1
        if rc:
            fails += 1
    print(f"=== done :: {len(tests) - fails}/{len(tests)} pass ===", flush=True)
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
