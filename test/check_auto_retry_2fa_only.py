"""Verify _maybe_auto_retry phân biệt 2FA-only vs full retry.

Bug trước: 2FA fail sau khi signup OK → auto-retry chạy lại toàn bộ signup
flow (login flow vì email đã reg → mất account).

Fix: detect `job.session_path and not job.secret` → set retry_2fa_only=True
khi schedule requeue, và preserve session_path + user_id trong DB.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web.manager import JobManager


def log(s: str) -> None:
    print(s, flush=True)


def _make_manager() -> JobManager:
    """Build manager với attribute tối thiểu cho _maybe_auto_retry."""
    m = JobManager.__new__(JobManager)
    m._auto_retry = True
    m._auto_retry_max = 5
    m._auto_retry_delay = 15.0
    m._shutting_down = False
    m._delayed_requeue_tasks = {}
    m.jobs = {}
    m._job_queue = MagicMock()
    m._broadcast_job = MagicMock()
    m._job_log = MagicMock(side_effect=lambda job, msg, **_: log(f"  job_log: {msg}"))
    m._persist_status = AsyncMock(return_value=True)
    m._schedule_delayed_requeue = MagicMock()
    return m


def _make_job(*, session_path: str | None, secret: str | None, error: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="job-1",
        retry_count=0,
        error=error,
        session_path=session_path,
        secret=secret,
        user_id="user-123" if session_path else None,
        status="error",
        started_at=1.0,
        finished_at=2.0,
    )


async def main() -> int:
    failures: list[str] = []

    log("\n=== TC-01: signup OK + 2FA fail → retry-2fa-only ===")
    m = _make_manager()
    job = _make_job(
        session_path="/runtime/sessions/foo.json",
        secret=None,
        error="2fa: enroll API HTTP 403",
    )
    requeued = await m._maybe_auto_retry(job)
    if not requeued:
        log("[FAIL] TC-01 — expected requeue=True")
        failures.append("TC-01")
    else:
        # _persist_status phải được gọi với session_path + user_id để giữ DB row
        kwargs = m._persist_status.call_args.kwargs
        if kwargs.get("session_path") != "/runtime/sessions/foo.json":
            log(f"[FAIL] TC-01 — session_path không preserve: kwargs={kwargs}")
            failures.append("TC-01-persist")
        elif kwargs.get("user_id") != "user-123":
            log(f"[FAIL] TC-01 — user_id không preserve: kwargs={kwargs}")
            failures.append("TC-01-user_id")
        # _schedule_delayed_requeue phải nhận retry_2fa_only=True
        elif m._schedule_delayed_requeue.call_args.kwargs.get("retry_2fa_only") is not True:
            log(f"[FAIL] TC-01 — retry_2fa_only flag missing: {m._schedule_delayed_requeue.call_args}")
            failures.append("TC-01-flag")
        # In-memory session_path không bị clear
        elif job.session_path != "/runtime/sessions/foo.json":
            log(f"[FAIL] TC-01 — in-memory session_path bị clear: {job.session_path!r}")
            failures.append("TC-01-mem")
        else:
            log("[PASS] TC-01 — retry-2fa-only đúng (preserve session, flag set)")

    log("\n=== TC-02: signup fail (no session_path) → full retry ===")
    m = _make_manager()
    job = _make_job(
        session_path=None,
        secret=None,
        error="signup: OTP timeout",
    )
    requeued = await m._maybe_auto_retry(job)
    if not requeued:
        log("[FAIL] TC-02 — expected requeue=True")
        failures.append("TC-02")
    else:
        kwargs = m._persist_status.call_args.kwargs
        # Full retry: KHÔNG preserve session_path/user_id
        if "session_path" in kwargs:
            log(f"[FAIL] TC-02 — full retry không nên preserve session_path: {kwargs}")
            failures.append("TC-02-preserve")
        elif m._schedule_delayed_requeue.call_args.kwargs.get("retry_2fa_only") is True:
            log(f"[FAIL] TC-02 — retry_2fa_only flag không nên set cho full retry")
            failures.append("TC-02-flag")
        elif job.session_path is not None or job.user_id is not None:
            log(f"[FAIL] TC-02 — full retry phải clear session/user in-memory")
            failures.append("TC-02-mem")
        else:
            log("[PASS] TC-02 — full retry đúng (clear state)")

    log("\n=== TC-03: signup OK + 2FA OK → KHÔNG retry (success path, không nên gọi đến) ===")
    m = _make_manager()
    job = _make_job(
        session_path="/runtime/sessions/foo.json",
        secret="JBSWY3DPEHPK3PXP",
        error="should_not_be_called",  # status="error" để vẫn vào hàm
    )
    # Note: thực tế _maybe_auto_retry chỉ gọi khi job.status="error".
    # Job có cả secret và session_path → chứng tỏ Phase 2 OK rồi, lỗi sau đó
    # (vd post-reg) → vẫn full retry vì session_path đã có nhưng có secret.
    requeued = await m._maybe_auto_retry(job)
    if requeued:
        # Khi có secret rồi, retry_2fa_only phải FALSE (không retry 2FA nữa).
        flag = m._schedule_delayed_requeue.call_args.kwargs.get("retry_2fa_only")
        if flag is True:
            log(f"[FAIL] TC-03 — đã có secret, không nên retry 2fa-only")
            failures.append("TC-03-flag")
        else:
            log("[PASS] TC-03 — có secret → retry full (không 2fa-only)")

    log("\n=== TC-04: retry_count >= max → không retry ===")
    m = _make_manager()
    m._auto_retry_max = 2
    job = _make_job(
        session_path="/runtime/sessions/foo.json",
        secret=None,
        error="2fa: enroll API HTTP 403",
    )
    job.retry_count = 2
    requeued = await m._maybe_auto_retry(job)
    if requeued:
        log("[FAIL] TC-04 — đã max retry, không nên requeue")
        failures.append("TC-04")
    else:
        log("[PASS] TC-04 — max retry hết → dừng")

    log(f"\n=== summary: {4 - len(failures)}/4 pass ===")
    if failures:
        log(f"FAILED: {', '.join(failures)}")
        return 1
    log("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
