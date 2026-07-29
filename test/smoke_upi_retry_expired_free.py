#!/usr/bin/env python3
"""Smoke test: UpiJobManager.retry_expired_free filter logic.

Cover predicate (success + qr_expired + plan_check.ok && !is_plus):

    TC-01: Mix 6 trạng thái khác nhau → chỉ pick đúng 2 jobs match.
    TC-02: Empty jobs map → returns 0.
    TC-03: Cached Plus job (from_cache=True, is_plus=True) → bị skip.
    TC-04: QR chưa expired (qr_expires_at > now) → skip dù free.
    TC-05: plan_check.ok=False (check fail) → skip để khỏi retry sớm.

Chạy:
    .venv/bin/python3 test/smoke_upi_retry_expired_free.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from gpt_signup_hybrid_new.web import manager as mgr_mod  # noqa: E402


def make_manager():
    m = mgr_mod.UpiJobManager(max_concurrent=1)
    m._ensure_workers = lambda: None  # type: ignore[method-assign]
    m._broadcast_job = lambda job: None  # type: ignore[method-assign]
    m._broadcast = lambda payload: None  # type: ignore[method-assign]
    return m


def add_job(m, *, email, status, qr_expires_at=None, plan_check=None):
    """Helper: tạo UpiJob trực tiếp, bypass parsing/queue."""
    import uuid
    jid = uuid.uuid4().hex[:12]
    job = mgr_mod.UpiJob(
        id=jid, email=email, password="pw", secret="sec",
        status=status,
        qr_expires_at=qr_expires_at,
        plan_check=plan_check,
        finished_at=time.time() if status == "success" else None,
    )
    m.jobs[jid] = job
    m.order.append(jid)
    return jid, job


def tc01_mixed_states_pick_only_match():
    m = make_manager()
    now = time.time()
    expired = int(now - 60)   # 1 phút trước
    future = int(now + 300)   # 5 phút sau

    # MATCH: success + expired + plan_check.ok=True + is_plus=False
    a, _ = add_job(m, email="a@x.com", status="success",
                    qr_expires_at=expired,
                    plan_check={"ok": True, "is_plus": False, "plan": "free"})

    # MATCH 2: cùng pattern
    b, _ = add_job(m, email="b@x.com", status="success",
                    qr_expires_at=expired,
                    plan_check={"ok": True, "is_plus": False, "plan": "free"})

    # SKIP — đã lên Plus
    add_job(m, email="plus@x.com", status="success",
            qr_expires_at=expired,
            plan_check={"ok": True, "is_plus": True, "plan": "plus"})

    # SKIP — chưa expired
    add_job(m, email="future@x.com", status="success",
            qr_expires_at=future,
            plan_check={"ok": True, "is_plus": False, "plan": "free"})

    # SKIP — chưa check plan
    add_job(m, email="nocheck@x.com", status="success",
            qr_expires_at=expired,
            plan_check=None)

    # SKIP — status='error' (dùng Retry Failed thay)
    add_job(m, email="err@x.com", status="error",
            qr_expires_at=expired,
            plan_check={"ok": True, "is_plus": False})

    # Stub retry_job để không thực sự enqueue (test predicate, không test retry).
    retried_ids = []
    async def fake_retry(jid):
        retried_ids.append(jid)
        return True
    m.retry_job = fake_retry  # type: ignore[method-assign]

    n = asyncio.run(m.retry_expired_free())
    assert n == 2, f"expected 2 retried, got {n}"
    assert set(retried_ids) == {a, b}, \
        f"expected only {{a,b}}, got {retried_ids}"
    print("[PASS] TC-01 6-state mix → exactly 2 match (a, b) retry", flush=True)


def tc02_empty_jobs():
    m = make_manager()
    n = asyncio.run(m.retry_expired_free())
    assert n == 0
    print("[PASS] TC-02 empty jobs map → 0 retried", flush=True)


def tc03_cached_plus_skipped():
    m = make_manager()
    now = time.time()
    add_job(m, email="cached@x.com", status="success",
            qr_expires_at=int(now - 60),
            plan_check={"ok": True, "is_plus": True, "plan": "plus", "from_cache": True})

    n = asyncio.run(m.retry_expired_free())
    assert n == 0, f"cached Plus should be skipped, got {n}"
    print("[PASS] TC-03 cached Plus job (is_plus=True) → skipped", flush=True)


def tc04_qr_not_expired_skipped():
    m = make_manager()
    now = time.time()
    add_job(m, email="not-yet@x.com", status="success",
            qr_expires_at=int(now + 600),  # còn 10 phút
            plan_check={"ok": True, "is_plus": False})

    m.retry_job = lambda jid: asyncio.sleep(0, result=True)  # type: ignore[method-assign]
    n = asyncio.run(m.retry_expired_free())
    assert n == 0
    print("[PASS] TC-04 QR chưa expired → skipped (free nhưng còn hạn)", flush=True)


def tc05_plan_check_fail_skipped():
    m = make_manager()
    now = time.time()
    add_job(m, email="checkerr@x.com", status="success",
            qr_expires_at=int(now - 60),
            plan_check={"ok": False, "is_plus": False, "error": "session error"})

    n = asyncio.run(m.retry_expired_free())
    assert n == 0, "plan_check.ok=False → không retry vì chưa biết Plus thật"
    print("[PASS] TC-05 plan_check.ok=False → skipped (chưa verify được)", flush=True)


def main():
    tests = [
        tc01_mixed_states_pick_only_match,
        tc02_empty_jobs,
        tc03_cached_plus_skipped,
        tc04_qr_not_expired_skipped,
        tc05_plan_check_fail_skipped,
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
