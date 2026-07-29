#!/usr/bin/env python3
"""Smoke test: UpiJobManager._plus_cache flow.

Cover:
    TC-01: Empty cache → add_jobs tạo UpiJob status='queued' bình thường.
    TC-02: Cache hit → add_jobs tạo UpiJob status='success' với plan_check.from_cache=True,
           KHÔNG enqueue, log [plus-cache] hit.
    TC-03: Case-insensitive key (email lowercase).
    TC-04: clear_plus_cache(email) xoá entry tồn tại → True.
    TC-05: clear_plus_cache(email) email không tồn tại → False.
    TC-06: get_secrets_map() trả map đầy đủ {email, password, secret} per job.

KHÔNG đụng DB / không spawn worker / không gọi API thật. Chỉ test in-memory state.

Chạy:
    python3 test/smoke_upi_plus_cache.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

# Stub dependencies trước khi import manager — manager.py có nhiều import nặng
# (playwright, db engine) không cần cho test in-memory cache.
import types  # noqa: E402

# Import qua package name (pattern chuẩn của repo) — KHÔNG dùng `from web import ...`
# vì web là sub-package của gpt_signup_hybrid_new (relative import beyond top-level).
from gpt_signup_hybrid_new.web import manager as mgr_mod  # noqa: E402


def make_manager():
    """Tạo UpiJobManager fresh, vô hiệu _ensure_workers để skip event loop."""
    m = mgr_mod.UpiJobManager(max_concurrent=1)

    # Stub _ensure_workers — test không cần worker thật.
    m._ensure_workers = lambda: None  # type: ignore[method-assign]

    # Stub _broadcast_job — manager.py call SSE mux singleton; KHÔNG init server
    # nên _sse_mux=None gây error. Replace bằng no-op.
    m._broadcast_job = lambda job: None  # type: ignore[method-assign]
    m._broadcast = lambda payload: None  # type: ignore[method-assign]

    return m


def tc01_empty_cache_normal_flow():
    m = make_manager()
    jobs = m.add_jobs(["a@x.com|pwA|secA", "b@x.com|pwB|secB"])
    assert len(jobs) == 2, f"expected 2 jobs, got {len(jobs)}"
    for j in jobs:
        assert j.status == "queued", f"expected queued, got {j.status}"
        assert j.plan_check is None, f"expected no plan_check, got {j.plan_check}"
    print("[PASS] TC-01 empty cache → 2 queued jobs, no plan_check", flush=True)


def tc02_cache_hit_skip_flow():
    m = make_manager()
    # Seed cache cho 1 email.
    m._plus_cache["a@x.com"] = {
        "plan": "plus",
        "verified_at": 1700000000,
        "source": "check_plan",
        "active_proxy": None,
    }

    jobs = m.add_jobs(["a@x.com|pwA|secA", "b@x.com|pwB|secB"])
    assert len(jobs) == 2

    a, b = jobs
    assert a.email == "a@x.com"
    assert a.status == "success", f"a should be success, got {a.status}"
    assert a.finished_at is not None, "a finished_at should be set"
    assert a.plan_check is not None and a.plan_check["is_plus"] is True
    assert a.plan_check.get("from_cache") is True
    assert a.plan_check.get("plan") == "plus"

    assert b.status == "queued", f"b should be queued, got {b.status}"
    assert b.plan_check is None

    # log line phải có [plus-cache] hit cho a
    has_log = any("[plus-cache] hit" in ln for ln in a.log_lines)
    assert has_log, f"expected '[plus-cache] hit' in a.log_lines, got {a.log_lines}"
    print("[PASS] TC-02 cache hit → success + from_cache + skip enqueue", flush=True)


def tc03_case_insensitive_key():
    m = make_manager()
    m._plus_cache["foo@bar.com"] = {
        "plan": "plus", "verified_at": 1, "source": "check_plan", "active_proxy": None,
    }
    jobs = m.add_jobs(["FOO@BAR.com|pw|sec"])
    assert len(jobs) == 1
    assert jobs[0].status == "success", f"expected success, got {jobs[0].status}"
    print("[PASS] TC-03 cache key case-insensitive (FOO@BAR.com matches foo@bar.com)", flush=True)


def tc04_clear_plus_cache_hit():
    m = make_manager()
    m._plus_cache["a@x.com"] = {"plan": "plus", "verified_at": 1, "source": "check_plan"}
    res = m.clear_plus_cache("A@X.com")
    assert res is True, f"expected True, got {res}"
    assert "a@x.com" not in m._plus_cache, "entry should be removed"
    print("[PASS] TC-04 clear_plus_cache hit → True + entry removed", flush=True)


def tc05_clear_plus_cache_miss():
    m = make_manager()
    res = m.clear_plus_cache("ghost@nope.com")
    assert res is False, f"expected False, got {res}"
    print("[PASS] TC-05 clear_plus_cache miss → False", flush=True)


def tc06_get_secrets_map():
    m = make_manager()
    jobs = m.add_jobs(["a@x.com|pwA|secA", "b@x.com|pwB|"])
    smap = m.get_secrets_map()
    assert len(smap) == 2, f"expected 2 entries, got {len(smap)}"
    a_id = jobs[0].id
    b_id = jobs[1].id
    assert smap[a_id] == {"email": "a@x.com", "password": "pwA", "secret": "secA"}
    # secret rỗng → None (parse logic add_jobs)
    assert smap[b_id]["password"] == "pwB"
    assert smap[b_id]["secret"] is None
    print("[PASS] TC-06 get_secrets_map returns email/password/secret per job", flush=True)


def tc07_check_plan_writes_cache():
    """Mock check_plan flow — chỉ test phần write-through cache, không gọi
    HTTP. Set up job có cookies, monkey-patch fetch_session_via_http +
    fetch_account_entitlement → simulate is_plus=True → assert cache updated."""
    m = make_manager()
    # Tạo job giả lập đã login OK (cookies non-empty).
    jobs = m.add_jobs(["plus@me.com|pw|sec"])
    j = jobs[0]
    j._session_cookies = [{"name": "x", "value": "y"}]
    j._access_token = "fake_token"

    # Patch session_phase imports trong module bằng stub.
    import sys as _sys
    fake_mod = types.ModuleType("gpt_signup_hybrid_new.session_phase")

    async def fake_session(**kw):  # noqa: ARG001
        return {"expires": "2099-01-01T00:00:00Z", "accessToken": "tk"}

    async def fake_entitlement(**kw):  # noqa: ARG001
        return {"plan": "plus", "is_plus": True}

    class _SE(Exception): pass
    fake_mod.fetch_session_via_http = fake_session
    fake_mod.fetch_account_entitlement = fake_entitlement
    fake_mod.SessionError = _SE
    # check_plan body: `from ..session_phase import ...` (relative 2 cấp từ
    # gpt_signup_hybrid_new.web.manager → gpt_signup_hybrid_new.session_phase).
    _sys.modules["gpt_signup_hybrid_new.session_phase"] = fake_mod
    # Patch attribute trên parent package để relative import resolve đúng.
    pkg = _sys.modules.get("gpt_signup_hybrid_new")
    if pkg is not None:
        pkg.session_phase = fake_mod  # type: ignore[attr-defined]

    res = asyncio.run(m.check_plan(j.id))
    assert res["ok"] is True, f"expected ok=True, got {res}"
    assert res["is_plus"] is True
    assert "plus@me.com" in m._plus_cache, f"cache should contain email, got {list(m._plus_cache.keys())}"
    entry = m._plus_cache["plus@me.com"]
    assert entry["plan"] == "plus"
    assert entry["source"] == "check_plan"
    print("[PASS] TC-07 check_plan write-through _plus_cache when is_plus=True", flush=True)


def tc08_check_plan_self_heal():
    """is_plus=False khi cache đang có → cache phải bị xoá (self-heal Q-B)."""
    m = make_manager()
    jobs = m.add_jobs(["churned@me.com|pw|sec"])
    j = jobs[0]
    j._session_cookies = [{"name": "x", "value": "y"}]
    j._access_token = "tk"
    # Pre-seed cache để giả lập acc đã từng Plus, giờ rớt.
    m._plus_cache["churned@me.com"] = {
        "plan": "plus", "verified_at": 1, "source": "check_plan", "active_proxy": None,
    }

    import sys as _sys
    fake_mod = types.ModuleType("gpt_signup_hybrid_new.session_phase")

    async def fake_session(**kw):  # noqa: ARG001
        return {"expires": None, "accessToken": "tk"}

    async def fake_entitlement(**kw):  # noqa: ARG001
        return {"plan": "free", "is_plus": False}

    class _SE(Exception): pass
    fake_mod.fetch_session_via_http = fake_session
    fake_mod.fetch_account_entitlement = fake_entitlement
    fake_mod.SessionError = _SE
    _sys.modules["gpt_signup_hybrid_new.session_phase"] = fake_mod
    pkg = _sys.modules.get("gpt_signup_hybrid_new")
    if pkg is not None:
        pkg.session_phase = fake_mod  # type: ignore[attr-defined]

    res = asyncio.run(m.check_plan(j.id))
    assert res["is_plus"] is False
    assert "churned@me.com" not in m._plus_cache, \
        f"cache should be cleared after recheck failed, got {m._plus_cache}"
    print("[PASS] TC-08 check_plan self-heal: is_plus=False → cache entry deleted", flush=True)


def main():
    tests = [
        tc01_empty_cache_normal_flow,
        tc02_cache_hit_skip_flow,
        tc03_case_insensitive_key,
        tc04_clear_plus_cache_hit,
        tc05_clear_plus_cache_miss,
        tc06_get_secrets_map,
        tc07_check_plan_writes_cache,
        tc08_check_plan_self_heal,
    ]
    failures = 0
    for tc in tests:
        try:
            tc()
        except AssertionError as exc:
            print(f"[FAIL] {tc.__name__} :: {exc}", flush=True)
            failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {tc.__name__} :: {type(exc).__name__}: {exc}", flush=True)
            failures += 1
    print(
        f"\n{len(tests) - failures}/{len(tests)} passed"
        + (f" — {failures} failures" if failures else ""),
        flush=True,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
