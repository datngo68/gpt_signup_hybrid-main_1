"""Web routes cho icloud-hme-pool tab — task 30/31/32 (R10).

Refs:
    requirements.md R10.1–R10.19
    design.md §Components / Web_API
    tasks.md task 30, 31, 32

Mount điểm: ``app.include_router(build_icloud_router())`` trong server.py.

Public endpoints prefix ``/api/icloud/*`` (auth qua middleware hiện có
require_token).

Dependency injection: dùng module-level lazy singletons get_pool_mgr() /
get_hme_mgr() để tránh init eager khi server start (iCloud feature có thể
không dùng).

Cleanup ngoài scope spec icloud-runner-loop: 7 endpoint /run/* được migrate
từ ``icloud_hme/web/router.py`` (router cũ dùng Bearer auth riêng) vào đây
để chạy chung middleware ``require_token`` của ``web/server.py``. Auth qua
single source of truth, không duplicate token cho icloud module.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Top-level import schemas của icloud-runner-loop. FastAPI ``get_type_hints``
# resolve annotation string từ module globals — phải nằm ở top-level (KHÔNG
# import lazy trong factory) để body parameter được bind đúng kiểu Pydantic.
# ``from __future__ import annotations`` chỉ ảnh hưởng cách chuỗi-hoá
# annotation, không cấm import name vào module globals.
from icloud_hme.web.schemas import RunRequest, RunStatus
from autoreg.schemas import AutoRegStartRequest
from .runner_config_store import (
    RunnerConfig,
    RunnerConfigError,
    RunnerConfigStore,
)


# Module-level lazy singletons
_pool_mgr = None
_hme_mgr = None
_checker = None
_pool_repo = None
_audit_repo = None
_recorder = None
_settings = None
_add_profile_svc = None  # R14 — Add_Profile_Flow service
_open_profile_svc = None  # R15 — Open_Profile_Flow service

# Runner-loop singletons (icloud-runner-loop, migrate từ icloud_hme/web/router.py)
_runner = None  # type: ignore[var-annotated]
_log_buffer = None  # type: ignore[var-annotated]
_runner_factory = None  # type: ignore[var-annotated]
_runner_config_store: RunnerConfigStore | None = None

# AutoReg GPT singletons (lazy init on first autoreg API call)
_autoreg_runner = None  # type: ignore[var-annotated]
_autoreg_log_buffer = None  # type: ignore[var-annotated]


async def _runner_task_wrapper(
    runner: Any,
    action: str,
    params: dict,
    log_buffer: Any,
    runner_lock: Any = None,
) -> None:
    """Wrap ``runner.start()`` để exception KHÔNG bị nuốt im lặng.

    Khi spawn qua ``asyncio.create_task(runner.start(...))`` trực tiếp,
    nếu ``start()`` raise (e.g. service layer fatal hoặc bug code),
    asyncio chỉ log ``"Task exception was never retrieved"`` sau GC →
    UI badge vẫn hiển thị RUNNING dù runner đã chết, user không biết.

    Wrapper này:
        1. Push event level=error vào LogBuffer → UI thấy ngay qua SSE.
        2. Log traceback ra stderr cho dev/operator soi.
        3. Release ``runner_lock`` (nếu có) ở ``finally`` để process khác
           (CLI hoặc worker khác) có thể start lại sau crash.
        4. KHÔNG re-raise — task complete bình thường để asyncio không
           warn (state đã reset ở ``runner.start()`` finally block).
    """
    import sys
    import traceback

    try:
        await runner.start(action=action, params=params)
    except asyncio.CancelledError:
        # Task bị cancel chủ động (e.g. server shutdown) — re-raise để
        # asyncio nhận biết, không log error vì đây là cancel hợp lệ.
        raise
    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        # 1. UI notification qua LogBuffer — best-effort, không re-raise nếu
        # buffer chính nó cũng lỗi.
        try:
            await log_buffer.push(
                "error",
                f"Runner task crashed: {type(exc).__name__}: {exc}",
                {
                    "action": action,
                    "error_type": type(exc).__name__,
                    "traceback": tb,
                },
            )
        except Exception:  # noqa: BLE001
            pass
        # 2. stderr cho operator soi traceback đầy đủ.
        print(
            f"[icloud_routes] runner task crashed (action={action}): "
            f"{type(exc).__name__}: {exc}\n{tb}",
            file=sys.stderr,
        )
        # KHÔNG re-raise: runner.start() finally block đã reset
        # _running=False, current_action=None — state consistent.
    finally:
        # Cross-process lock release — luôn run dù success/error/cancel.
        # ``runner_lock.release()`` idempotent, an toàn gọi 2 lần.
        if runner_lock is not None:
            try:
                runner_lock.release()
            except Exception:  # noqa: BLE001
                pass


def _init_services() -> None:
    """Lazy init iCloud services khi endpoint đầu tiên được gọi."""
    global _pool_mgr, _hme_mgr, _checker
    global _pool_repo, _audit_repo, _recorder, _settings, _add_profile_svc, _open_profile_svc
    global _runner, _log_buffer, _runner_factory, _runner_config_store

    if _pool_mgr is not None:
        return

    from config import load_settings
    from db import get_engine
    from db.repositories import (
        AuditLogRepository,
        IcloudPoolRepository,
    )
    from icloud_hme.add_profile import AddProfileService
    from icloud_hme.checker import ProfileChecker
    from icloud_hme.generator import HmeGenerator
    from icloud_hme.manager import HmeManager
    from icloud_hme.open_profile import OpenProfileService
    from icloud_hme.pool import IcloudPoolManager
    from icloud_hme.recorder import Recorder
    from icloud_hme.runner import HmeRunner
    from icloud_hme.web.log_buffer import LogBuffer, make_web_log_callback

    _settings = load_settings()
    engine = get_engine()
    _pool_repo = IcloudPoolRepository(engine)
    _audit_repo = AuditLogRepository(engine)

    _pool_mgr = IcloudPoolManager(
        _pool_repo,
        _audit_repo,
        limited_ttl_hours=_settings.icloud_limited_ttl_hours,
        quota_retry_minutes=_settings.icloud_quota_retry_minutes,
        hme_quota_limit=_settings.icloud_hme_quota_limit,
        log=None,
    )
    _hme_mgr = HmeManager(
        _pool_mgr, _pool_repo, _audit_repo, log=None
    )
    _checker = ProfileChecker(
        _pool_mgr, _pool_repo, _audit_repo, log=None
    )
    _recorder = Recorder(
        runtime_dir=_settings.runtime_dir,
        audit_repo=_audit_repo,
        retention_days=_settings.icloud_recording_retention_days,
        log=None,
    )
    _add_profile_svc = AddProfileService(
        runtime_dir=_settings.runtime_dir,
        pool_repo=_pool_repo,
        audit_repo=_audit_repo,
        timeout_sec=_settings.icloud_add_profile_timeout_sec,
        # Log ra stderr — khi user gặp lỗi `apple_id_not_extractable` server
        # sẽ in chi tiết cookies + webAuth.dsInfo cho dev/operator soi.
        log=lambda msg: print(f"[add_profile] {msg}", file=__import__("sys").stderr),
    )
    _open_profile_svc = OpenProfileService(
        runtime_dir=_settings.runtime_dir,
        pool_repo=_pool_repo,
        audit_repo=_audit_repo,
        timeout_sec=_settings.icloud_open_profile_timeout_sec,
        log=lambda msg: print(f"[open_profile] {msg}", file=__import__("sys").stderr),
    )

    # ── Runner loop wiring (migrate từ icloud_hme/web/router.py) ────────
    # LogBuffer là singleton 1 process; SSE subscriber cùng share buffer.
    _log_buffer = LogBuffer()
    _log_callback = make_web_log_callback(_log_buffer)

    # Wire SseMux for unified SSE fan-out (task 3.5)
    from .server import get_sse_mux

    _log_buffer.set_sse_mux(get_sse_mux(), "hme_log")

    # Generator dùng cho Runner action "generate". Tạo riêng (không dùng lại
    # cho service layer khác) — generator chỉ chứa logic tạo HME, không trùng
    # với HmeManager.
    _generator = HmeGenerator(
        _pool_mgr,
        _pool_repo,
        _audit_repo,
        race_retry_max=_settings.icloud_hme_race_retry_max,
        profile_parallelism=_settings.icloud_hme_profile_parallelism,
        infinite_wait_max_sec=_settings.icloud_infinite_wait_max_sec,
        hme_quota_limit=_settings.icloud_hme_quota_limit,
        log=None,
    )

    # Capture vào closure để factory build runner mới mỗi khi user đổi
    # retry_interval (Web_API contract: rebuild instance + swap singleton).
    _services = {
        "generator": _generator,
        "checker": _checker,
        "hme_manager": _hme_mgr,
        "pool_manager": _pool_mgr,
        "settings": _settings,
        "log_callback": _log_callback,
    }

    def runner_factory(retry_interval: int | None) -> "HmeRunner":
        """Build new ``HmeRunner`` với optional retry_interval override.

        ``retry_interval=None`` → Runner đọc default từ
        ``Settings.icloud_retry_interval`` (R7.1 / R7.2).
        """
        return HmeRunner(
            generator=_services["generator"],
            checker=_services["checker"],
            hme_manager=_services["hme_manager"],
            pool_manager=_services["pool_manager"],
            settings=_services["settings"],
            log_callback=_services["log_callback"],
            retry_interval=retry_interval,
        )

    _runner_factory = runner_factory

    # Persisted runner form config (action / count / retry / label / note).
    # Đặt trước khi build _runner để dùng retry_interval đã lưu (nếu có) —
    # tránh build runner lần 1 với default rồi rebuild ngay khi UI gửi
    # request đầu (lãng phí + race với _runner.is_running check).
    _runner_config_store = RunnerConfigStore(_settings.runtime_dir)
    saved_cfg, cfg_err = _runner_config_store.load_or_default()
    if cfg_err is not None:
        import sys

        print(
            f"[icloud_routes] runner_config corrupt — fallback default: "
            f"{cfg_err}. Path: {_runner_config_store.path}",
            file=sys.stderr,
        )
    _runner = runner_factory(saved_cfg.retry_interval)

    # Cleanup orphan profile_dir tạm từ process trước (R14.12).
    try:
        removed = _add_profile_svc.cleanup_orphan_on_startup()
        if removed:
            # Best-effort log — không có logger, dùng print stderr.
            import sys

            print(
                f"[icloud_routes] cleanup_orphan removed {removed} "
                f"add_profile session(s)",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001 — KHÔNG fail init
        import sys

        print(
            f"[icloud_routes] cleanup_orphan_on_startup fail: {exc!r}",
            file=sys.stderr,
        )


def get_autoreg_runner():
    """Return the module-level AutoRegRunner singleton (may be None if never initialized)."""
    return _autoreg_runner


def get_hme_log_buffer():
    """Return the HME LogBuffer singleton (may be None if never initialized).

    Used by SseMux snapshot registration.
    """
    return _log_buffer


def get_autoreg_log_buffer():
    """Return the AutoReg LogBuffer singleton (may be None if never initialized).

    Used by SseMux snapshot registration.
    """
    return _autoreg_log_buffer


def _init_autoreg() -> None:
    """Lazy init AutoRegRunner + LogBuffer + ChatGptAccountRepository.

    Gọi lần đầu từ autoreg endpoint. Pattern giống _init_services nhưng
    riêng scope autoreg — tránh init khi user không dùng feature này.
    """
    global _autoreg_runner, _autoreg_log_buffer

    if _autoreg_runner is not None:
        return

    from autoreg.runner import AutoRegRunner
    from db import get_engine
    from db.repositories import ChatGptAccountRepository
    from icloud_hme.web.log_buffer import LogBuffer, make_web_log_callback

    _autoreg_log_buffer = LogBuffer()
    _log_callback = make_web_log_callback(_autoreg_log_buffer)

    # Wire SseMux for unified SSE fan-out (task 3.5)
    from .server import get_sse_mux

    _autoreg_log_buffer.set_sse_mux(get_sse_mux(), "autoreg_log")

    engine = get_engine()
    account_repo = ChatGptAccountRepository(engine)

    _autoreg_runner = AutoRegRunner(
        log_callback=_log_callback,
        account_repo=account_repo,
    )


# Pydantic request models


class BulkLifecycleRequest(BaseModel):
    emails: list[str] = Field(default_factory=list, max_length=500)
    dry_run: bool = Field(default=False)


class UpdateMetaBulkRequest(BaseModel):
    items: list[dict] = Field(default_factory=list, max_length=500)
    dry_run: bool = Field(default=False)


class MarkUsedRequest(BaseModel):
    used_for: str = Field(..., min_length=1, max_length=200)


class AddProfileSaveRequest(BaseModel):
    """Body cho POST /profiles/add/{session_id}/save (R14.3, R14 update).

    apple_id field optional — user nhập tay khi auto-extract fail.
    Nếu KHÔNG truyền hoặc empty, server sẽ thử auto-extract qua page.evaluate
    + cookies (best-effort, không reliable).
    """

    apple_id: str | None = Field(default=None, max_length=320)


def _serialize_profile_snapshot(p) -> dict:
    return {
        "apple_id": p.apple_id,
        "status": p.status,
        "hme_count": p.hme_count,
        "quota_remaining": p.quota_remaining,
        "limited_until": p.limited_until.isoformat() + "Z" if p.limited_until else None,
        "quota_retry_until": p.quota_retry_until.isoformat() + "Z" if p.quota_retry_until else None,
        "last_used_at": p.last_used_at.isoformat() + "Z" if p.last_used_at else None,
        "last_error": p.last_error,
    }


def _serialize_lifecycle(result) -> dict:
    return {
        "requested": result.requested,
        "succeeded": result.succeeded,
        "skipped": result.skipped,
        "remaining": result.remaining,
        "failed": result.failed,
        "dry_run": result.dry_run,
    }


def _format_iso_dt(value: datetime | None) -> str | None:
    """Format datetime → ISO 8601 UTC + suffix Z (Timestamp_Format P30).

    None → None. Naive datetime được coi là UTC.
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def build_icloud_router() -> APIRouter:
    """Build APIRouter cho /api/icloud/* — wire to lazy singletons."""
    router = APIRouter(prefix="/api/icloud", tags=["icloud-hme"])

    # ── pool/status (R10.1) ─────────────────────────────────────────────
    @router.get("/pool/status")
    async def get_pool_status() -> JSONResponse:
        _init_services()
        report = _pool_mgr.status_report()
        return JSONResponse(
            {
                "by_status": report.by_status,
                "profiles": [_serialize_profile_snapshot(p) for p in report.profiles],
                "emails_by_status": report.emails_by_status,
                "quota_soft_cap_per_account": report.quota_soft_cap_per_account,
                "total_quota_remaining": report.total_quota_remaining,
                "low_capacity": report.low_capacity,
                "quota_full_count": report.quota_full_count,
                "quota_full_profiles": report.quota_full_profiles,
            }
        )

    # ── profiles list (R10.2) ────────────────────────────────────────────
    @router.get("/profiles")
    async def list_profiles(status: str | None = None) -> JSONResponse:
        _init_services()
        accounts = await asyncio.to_thread(_pool_repo.list_all)
        if status:
            accounts = [a for a in accounts if a.status == status]
        return JSONResponse(
            [
                {
                    "apple_id": a.apple_id,
                    "profile_dir": str(a.profile_dir) if a.profile_dir else None,
                    "status": a.status,
                    "hme_count": a.hme_count,
                    "limited_until": a.limited_until.isoformat() + "Z"
                    if a.limited_until
                    else None,
                    "quota_retry_until": a.quota_retry_until.isoformat() + "Z"
                    if a.quota_retry_until
                    else None,
                    "last_used_at": a.last_used_at.isoformat() + "Z"
                    if a.last_used_at
                    else None,
                    "last_error": a.last_error,
                }
                for a in accounts
            ]
        )

    # ── profile add (R14: Camoufox-based interactive add) ──────────────
    @router.post("/profiles/add/start")
    async def add_profile_start() -> JSONResponse:
        _init_services()
        from icloud_hme.add_profile import AddProfileError

        try:
            session = await _add_profile_svc.start()
        except AddProfileError as exc:
            if exc.reason == "add_profile_in_progress":
                return JSONResponse(
                    {
                        "error": exc.reason,
                        "active_session_id": exc.session_id,
                        "message": str(exc),
                    },
                    status_code=409,
                )
            return JSONResponse(
                {
                    "error": exc.reason,
                    "message": str(exc),
                    "session_id": exc.session_id,
                },
                status_code=500,
            )
        return JSONResponse(
            {
                "session_id": session.session_id,
                "started_at": _format_iso_dt(session.started_at),
                "profile_dir": str(session.profile_dir_temp),
            }
        )

    @router.post("/profiles/add/{session_id}/save")
    async def add_profile_save(
        session_id: str,
        body: AddProfileSaveRequest | None = None,
    ) -> JSONResponse:
        _init_services()
        from icloud_hme.add_profile import AddProfileError

        # Body optional — user có thể POST không kèm body khi muốn fallback
        # auto-extract. FastAPI parse body=None nếu Content-Length=0.
        hint = body.apple_id if body is not None else None

        try:
            session = await _add_profile_svc.save(
                session_id, apple_id_hint=hint
            )
        except AddProfileError as exc:
            status_code = {
                "apple_id_not_extractable": 400,
                "apple_id_mismatch": 400,
                "cookies_not_ready": 400,
                "apple_id_already_exists": 409,
                "move_failed": 500,
                "session_not_found": 404,
                "invalid_state": 409,
            }.get(exc.reason, 500)
            return JSONResponse(
                {
                    "error": exc.reason,
                    "message": str(exc),
                    "session_id": session_id,
                },
                status_code=status_code,
            )
        return JSONResponse(
            {
                "session_id": session.session_id,
                "apple_id": session.apple_id,
                "status": "active",
            }
        )

    @router.post("/profiles/add/{session_id}/cancel")
    async def add_profile_cancel(session_id: str) -> JSONResponse:
        _init_services()
        from icloud_hme.add_profile import AddProfileError

        try:
            session = await _add_profile_svc.cancel(session_id)
        except AddProfileError as exc:
            status_code = 404 if exc.reason == "session_not_found" else 500
            return JSONResponse(
                {
                    "error": exc.reason,
                    "message": str(exc),
                    "session_id": session_id,
                },
                status_code=status_code,
            )
        return JSONResponse(
            {
                "session_id": session.session_id,
                "status": session.state.value,
            }
        )

    @router.get("/profiles/add/{session_id}/status")
    async def add_profile_status(session_id: str) -> JSONResponse:
        _init_services()
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        from icloud_hme.add_profile import AddProfileError

        try:
            session = _add_profile_svc.status(session_id)
        except AddProfileError as exc:
            return JSONResponse(
                {
                    "error": exc.reason,
                    "message": str(exc),
                    "session_id": session_id,
                },
                status_code=404,
            )
        ended = session.ended_at or _dt.now(_tz.utc).replace(tzinfo=None)
        duration_sec = (ended - session.started_at).total_seconds()
        return JSONResponse(
            {
                "session_id": session.session_id,
                "state": session.state.value,
                "started_at": _format_iso_dt(session.started_at),
                "ended_at": _format_iso_dt(session.ended_at)
                if session.ended_at
                else None,
                "apple_id": session.apple_id,
                "error": session.error,
                "error_reason": session.error_reason,
                "duration_seconds": duration_sec,
            }
        )

    # ── profile open (R15: Camoufox-based interactive open existing profile) ─
    @router.post("/profiles/{apple_id}/open/start")
    async def open_profile_start(apple_id: str) -> JSONResponse:
        _init_services()
        from icloud_hme.exceptions import OpenProfileError

        try:
            session = await _open_profile_svc.start(apple_id)
        except OpenProfileError as exc:
            status_code = {
                "profile_not_found": 404,
                "profile_locked": 409,
                "open_profile_in_progress": 409,
            }.get(exc.reason, 500)
            body = {
                "error": exc.reason,
                "message": str(exc),
                "session_id": exc.session_id,
                "apple_id": exc.apple_id,
            }
            if exc.reason == "open_profile_in_progress":
                body["active_session_id"] = exc.session_id
                body["active_apple_id"] = exc.apple_id
            return JSONResponse(body, status_code=status_code)
        return JSONResponse(
            {
                "session_id": session.session_id,
                "apple_id": session.apple_id,
                "started_at": _format_iso_dt(session.started_at),
                "previous_status": session.previous_status,
            }
        )

    @router.post("/profiles/{apple_id}/open/{session_id}/save")
    async def open_profile_save(apple_id: str, session_id: str) -> JSONResponse:
        _init_services()
        from icloud_hme.exceptions import OpenProfileError

        # Validate apple_id trong path khớp session.apple_id (chống user tab cũ
        # gọi nhầm session_id của apple_id khác — R15 design §17 endpoint table).
        try:
            current = _open_profile_svc.status(session_id)
        except OpenProfileError as exc:
            return JSONResponse(
                {
                    "error": exc.reason,
                    "message": str(exc),
                    "session_id": session_id,
                    "apple_id": apple_id,
                },
                status_code=404,
            )
        if current.apple_id != apple_id.strip().lower():
            return JSONResponse(
                {
                    "error": "apple_id_mismatch",
                    "message": (
                        f"session_id={session_id} thuộc apple_id="
                        f"{current.apple_id!r}, không khớp path apple_id="
                        f"{apple_id!r}"
                    ),
                    "session_id": session_id,
                },
                status_code=422,
            )

        try:
            session = await _open_profile_svc.save(session_id)
        except OpenProfileError as exc:
            status_code = {
                "cookies_not_ready": 400,
                "session_not_found": 404,
                "invalid_state": 409,
            }.get(exc.reason, 500)
            return JSONResponse(
                {
                    "error": exc.reason,
                    "message": str(exc),
                    "session_id": session_id,
                    "apple_id": apple_id,
                },
                status_code=status_code,
            )
        return JSONResponse(
            {
                "session_id": session.session_id,
                "apple_id": session.apple_id,
                "status": "active",
                "matched_cookies": session.matched_cookies,
                "previous_status": session.previous_status,
            }
        )

    @router.post("/profiles/{apple_id}/open/{session_id}/close")
    async def open_profile_close(apple_id: str, session_id: str) -> JSONResponse:
        _init_services()
        from icloud_hme.exceptions import OpenProfileError

        try:
            session = await _open_profile_svc.close(session_id)
        except OpenProfileError as exc:
            status_code = 404 if exc.reason == "session_not_found" else 500
            return JSONResponse(
                {
                    "error": exc.reason,
                    "message": str(exc),
                    "session_id": session_id,
                    "apple_id": apple_id,
                },
                status_code=status_code,
            )
        return JSONResponse(
            {
                "session_id": session.session_id,
                "apple_id": session.apple_id,
                "status": session.state.value,  # 'closed' (hoặc state hiện tại nếu idempotent)
                "previous_status_unchanged": True,
            }
        )

    @router.get("/profiles/{apple_id}/open/{session_id}/status")
    async def open_profile_status(apple_id: str, session_id: str) -> JSONResponse:
        _init_services()
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        from icloud_hme.exceptions import OpenProfileError

        try:
            session = _open_profile_svc.status(session_id)
        except OpenProfileError as exc:
            return JSONResponse(
                {
                    "error": exc.reason,
                    "message": str(exc),
                    "session_id": session_id,
                    "apple_id": apple_id,
                },
                status_code=404,
            )
        ended = session.ended_at or _dt.now(_tz.utc).replace(tzinfo=None)
        duration_sec = (ended - session.started_at).total_seconds()
        return JSONResponse(
            {
                "session_id": session.session_id,
                "apple_id": session.apple_id,
                "state": session.state.value,
                "started_at": _format_iso_dt(session.started_at),
                "ended_at": _format_iso_dt(session.ended_at)
                if session.ended_at
                else None,
                "matched_cookies": session.matched_cookies,
                "previous_status": session.previous_status,
                "error": session.error,
                "error_reason": session.error_reason,
                "duration_seconds": duration_sec,
            }
        )

    # ── profile delete (R10.4) ──────────────────────────────────────────
    @router.delete("/profiles/{apple_id}")
    async def delete_profile(apple_id: str) -> JSONResponse:
        _init_services()
        result = _pool_mgr.delete_profile(apple_id)
        return JSONResponse(
            {
                "apple_id": result.apple_id,
                "deleted": result.deleted,
                "profile_dir_removed": result.profile_dir_removed,
                "hme_count_at_delete": result.hme_count_at_delete,
                "reason": result.reason,
            }
        )

    # ── emails list (R10.5, R10.15) ─────────────────────────────────────
    @router.get("/emails")
    async def list_emails(
        status: str | None = None,
        apple_id: str | None = None,
        label: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JSONResponse:
        _init_services()

        # Wrap DB calls in to_thread để không block event loop khi
        # AutoRegRunner đang write (SQLite busy lock / WAL checkpoint).
        rows = await asyncio.to_thread(
            _pool_repo.list_emails,
            status=status,
            apple_id=apple_id,
            label=label,
            limit=limit,
            offset=offset,
        )
        total = await asyncio.to_thread(
            _pool_repo.count_emails,
            status=status,
            apple_id=apple_id,
            label=label,
        )
        return JSONResponse({
            "rows": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
        })

    # ── emails single lifecycle ─────────────────────────────────────────
    @router.post("/emails/{email}/deactivate")
    async def deactivate_email(email: str, dry_run: bool = False) -> JSONResponse:
        _init_services()
        result = await _hme_mgr.deactivate(email, dry_run=dry_run)
        return JSONResponse(_serialize_lifecycle(result))

    @router.post("/emails/{email}/reactivate")
    async def reactivate_email(email: str, dry_run: bool = False) -> JSONResponse:
        _init_services()
        result = await _hme_mgr.reactivate(email, dry_run=dry_run)
        return JSONResponse(_serialize_lifecycle(result))

    @router.delete("/emails/{email}")
    async def delete_email(email: str, dry_run: bool = False) -> JSONResponse:
        _init_services()
        result = await _hme_mgr.delete(email, dry_run=dry_run)
        return JSONResponse(_serialize_lifecycle(result))

    @router.post("/emails/{email}/mark-used")
    async def mark_used(email: str, req: MarkUsedRequest) -> JSONResponse:
        _init_services()
        result = await _hme_mgr.mark_used(email, used_for=req.used_for)
        return JSONResponse(_serialize_lifecycle(result))

    # ── emails bulk lifecycle ──────────────────────────────────────────
    @router.post("/emails/bulk/deactivate")
    async def deactivate_bulk(req: BulkLifecycleRequest) -> JSONResponse:
        _init_services()
        result = await _hme_mgr.deactivate_bulk(req.emails, dry_run=req.dry_run)
        return JSONResponse(_serialize_lifecycle(result))

    @router.post("/emails/bulk/reactivate")
    async def reactivate_bulk(req: BulkLifecycleRequest) -> JSONResponse:
        _init_services()
        result = await _hme_mgr.reactivate_bulk(req.emails, dry_run=req.dry_run)
        return JSONResponse(_serialize_lifecycle(result))

    @router.post("/emails/bulk/delete")
    async def delete_bulk(req: BulkLifecycleRequest) -> JSONResponse:
        _init_services()
        result = await _hme_mgr.delete_bulk(req.emails, dry_run=req.dry_run)
        return JSONResponse(_serialize_lifecycle(result))

    @router.post("/emails/bulk/update-meta")
    async def update_meta_bulk(req: UpdateMetaBulkRequest) -> JSONResponse:
        _init_services()
        result = await _hme_mgr.update_meta_bulk(req.items, dry_run=req.dry_run)
        return JSONResponse(_serialize_lifecycle(result))

    # ── sync (R9.12) ────────────────────────────────────────────────────
    @router.post("/sync/{apple_id}")
    async def sync_apple_id(
        apple_id: str,
        dry_run: bool = Query(False, description="Preview diff không ghi DB."),
    ) -> JSONResponse:
        """list_sync 1 profile.

        Args:
            apple_id: profile cần sync.
            dry_run: True → tính diff KHÔNG ghi DB hay audit, trả counters
                để UI preview impact (B7 — review fix). Default False.
        """
        _init_services()
        diff = await _hme_mgr.list_sync(apple_id, dry_run=dry_run)
        return JSONResponse(
            {
                "apple_id": diff.apple_id,
                "inserted_active": diff.inserted_active,
                "inserted_inactive": diff.inserted_inactive,
                "db_marked_deactivated": diff.db_marked_deactivated,
                "db_marked_deleted": diff.db_marked_deleted,
                "db_marked_reactivated": diff.db_marked_reactivated,
                "unchanged": diff.unchanged,
                "dry_run": dry_run,
            }
        )

    # ── jobs ─────────────────────────────────────────────────────────────
    # Note: Job layer đã xoá hoàn toàn theo spec icloud-runner-loop.
    # Các endpoint cũ /jobs/*, POST /emails/generate đã removed; runner loop
    # mới chạy nội bộ qua icloud_hme.runner — UI/CLI tương ứng đã migrate.

    # ── audit ────────────────────────────────────────────────────────────
    @router.get("/audit")
    async def list_audit(
        apple_id: str | None = None,
        event_type: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> JSONResponse:
        _init_services()
        rows = _audit_repo.list(
            apple_id=apple_id,
            event_type=event_type,
            since=since,
            limit=limit,
        )
        return JSONResponse(rows)

    # ── runner loop /run/* (migrate từ icloud_hme/web/router.py) ────────
    # Auth: middleware ``require_token`` của web/server.py đã cover toàn bộ
    # ``/api/*`` (header X-API-Token / cookie / ?token=). Single source of
    # truth — KHÔNG dùng Bearer riêng. SSE EventSource đi qua ?token= được
    # web/auth.py support sẵn.

    @router.post("/run")
    async def api_run_start(req: RunRequest) -> JSONResponse:
        """Start runner (R9.1, R4.2, R10.6).

        Behavior:
            - ``runner.is_running == True`` → 409 ``{"error": "already_running"}``.
            - ``req.retry_interval`` khác current → rebuild Runner qua
              ``_runner_factory`` rồi swap singleton.
            - Clear ``_log_buffer`` (R10.6) trước khi spawn task.
            - ``asyncio.create_task(runner.start(...))`` — KHÔNG await.
        """
        _init_services()
        global _runner

        if _runner.is_running:
            # R4.2 / R9.1: HTTP 409 + body chứa key "error".
            raise HTTPException(
                status_code=409,
                detail={"error": "already_running"},
            )

        # Rebuild Runner instance nếu retry_interval khác (singleton swap).
        if (
            req.retry_interval is not None
            and req.retry_interval != _runner.retry_interval
        ):
            if _runner_factory is None:
                raise HTTPException(
                    status_code=500,
                    detail={
                        "error": "runner_factory_missing",
                        "message": (
                            "retry_interval khác current runner; "
                            "_runner_factory chưa init."
                        ),
                    },
                )
            _runner = _runner_factory(req.retry_interval)

        # R10.6: clear buffer + reset seq trước mỗi run mới.
        _log_buffer.clear()

        # Cross-process single-instance lock (icloud_hme/runner_lock.py):
        # chặn race khi CLI generate + Web POST /run cùng acquire pool.
        # Acquire ngay TRƯỚC khi spawn task — nếu lock fail thì 409, KHÔNG
        # spawn task (UI sẽ retry sau). Lock release ở wrapper finally
        # block để cover cả success/error/cancel/server-shutdown path.
        from icloud_hme.runner_lock import RunnerLock, RunnerLockError

        lock = RunnerLock(_settings.runtime_dir)
        try:
            lock.acquire()
        except RunnerLockError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "runner_lock_held",
                    "message": str(exc),
                    "existing_pid": exc.existing_pid,
                    "lock_path": str(exc.lock_path),
                },
            ) from None

        # R9.1: spawn task + KHÔNG await; response trả ngay.
        # Wrap qua _runner_task_wrapper để bất kỳ exception nào trong
        # ``runner.start()`` (e.g. service layer fatal) đều được:
        #   1. Push vào LogBuffer level "error" để UI thấy ngay.
        #   2. Log ra stderr để dev/operator soi traceback.
        #   3. Release runner_lock dù success/error.
        # Nếu KHÔNG wrap, asyncio chỉ log "Task exception was never
        # retrieved" sau GC → UI badge giữ RUNNING dù runner đã chết.
        asyncio.create_task(
            _runner_task_wrapper(
                _runner, req.action, req.params, _log_buffer, runner_lock=lock
            )
        )
        return JSONResponse({"ok": True, "action": req.action})

    @router.post("/run/stop")
    async def api_run_stop() -> JSONResponse:
        """Stop runner — non-blocking signal cancel (R9.2)."""
        _init_services()
        _runner.stop()
        return JSONResponse({"ok": True})

    @router.post("/run/pause")
    async def api_run_pause() -> JSONResponse:
        """Pause runner (R9.3)."""
        _init_services()
        _runner.pause()
        return JSONResponse({"ok": True})

    @router.post("/run/resume")
    async def api_run_resume() -> JSONResponse:
        """Resume runner sau pause (R9.4)."""
        _init_services()
        _runner.resume()
        return JSONResponse({"ok": True})

    @router.get("/run/status", response_model=RunStatus)
    async def api_run_status() -> RunStatus:
        """Snapshot trạng thái Runner hiện tại (R9.5).

        Build từ runner properties; convert ``next_cycle_at`` epoch float
        → ISO 8601 UTC string (None khi đang trong cycle hoặc idle).
        """
        _init_services()
        next_at = _runner.next_cycle_at
        next_iso: Optional[str]
        if next_at is None:
            next_iso = None
        else:
            next_iso = datetime.fromtimestamp(
                next_at, tz=timezone.utc
            ).isoformat()

        stats = _runner.stats
        return RunStatus(
            running=_runner.is_running,
            action=_runner.current_action,
            cycle=_runner.cycle_count,
            stats={
                "created": stats.created,
                "errors": stats.errors,
                "skipped": stats.skipped,
            },
            retry_interval=_runner.retry_interval,
            next_cycle_at=next_iso,
            current_apple_id=_runner.current_apple_id,
            profile_states=_runner.profile_states,
        )

    @router.get("/run/log")
    async def api_run_log(
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> JSONResponse:
        """Fetch slice các LogEvent có ``seq > offset``, tối đa ``limit`` (R9.6).

        Response: ``{"events": [LogEvent.model_dump()...], "next_offset": int}``.
        """
        _init_services()
        entries = _log_buffer.snapshot()
        # Deque ordered theo seq tăng dần — slice tuần tự là OK.
        selected = []
        for ev in entries:
            if ev.seq > offset:
                selected.append(ev)
                if len(selected) >= limit:
                    break

        next_offset = selected[-1].seq if selected else offset
        return JSONResponse(
            {
                "events": [ev.model_dump() for ev in selected],
                "next_offset": next_offset,
            }
        )

    # [REMOVED] GET /run/log/stream — legacy SSE endpoint for hme_log channel.
    # Replaced by unified GET /api/sse (SseMux channel "hme_log").
    # LogBuffer.subscribe() and push() kept intact for SseMux hook.

    # ── runner form config persist (UI form không reset khi reload) ─────
    # Lưu action / count_per_cycle / retry_interval / label / note ra
    # ``<runtime_dir>/icloud/runner_config.json`` (RunnerConfigStore).
    # Mục đích: UI reload tab hoặc backend restart → form vẫn giữ giá trị
    # user đã set. Backend cũng dùng retry_interval ở đây để build _runner
    # ngay từ _init_services() (xem code ở trên).
    @router.get("/run/config")
    async def api_run_config_get() -> JSONResponse:
        _init_services()
        cfg, err = _runner_config_store.load_or_default()
        body = cfg.to_dict()
        if err is not None:
            # Nếu file tồn tại nhưng corrupt, vẫn trả default + kèm warn để
            # UI có thể hiển thị badge "config reset".
            body["_warning"] = f"config_corrupt: {err}"
        return JSONResponse(body)

    @router.put("/run/config")
    async def api_run_config_put(payload: dict[str, Any]) -> JSONResponse:
        """Persist runner form config.

        Body khớp schema ``RunnerConfig.from_dict``. Validation strict: extra
        key, sai type, retry_interval < 10, count_per_cycle <= 0 → 400.
        """
        _init_services()
        try:
            cfg = RunnerConfig.from_dict(payload)
        except RunnerConfigError as exc:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_config", "message": str(exc)},
            ) from None
        try:
            _runner_config_store.save(cfg)
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail={"error": "save_failed", "message": str(exc)},
            ) from None

        # ── Write-through to Settings_Store (R6.3) ──
        from db import get_engine, get_settings_repo
        from db.repositories import RepositoryError

        response_body = cfg.to_dict()
        settings_dict: dict[str, Any] = {
            "hme.runner.action": cfg.action,
            "hme.runner.count_per_cycle": cfg.count_per_cycle,
            "hme.runner.retry_interval": cfg.retry_interval,
            "hme.runner.label": cfg.label,
            "hme.runner.note": cfg.note,
        }
        try:
            repo = get_settings_repo(get_engine())
            repo.bulk_set(settings_dict)
        except RepositoryError as e:
            import logging
            logging.getLogger(__name__).warning(
                "write-through settings failed (run/config): %s", e,
            )
            response_body["settings_persist_error"] = str(e)

        return JSONResponse(response_body)

    # ── AutoReg GPT endpoints (/autoreg/*) ─────────────────────────────
    # Pattern mirrors /run/* above: lazy singleton, SSE via LogBuffer,
    # start/stop lifecycle. Auth qua middleware require_token (SSE via ?token=).

    @router.post("/autoreg/start")
    async def autoreg_start(body: AutoRegStartRequest) -> JSONResponse:
        """Start AutoRegRunner (R9.1). Returns 409 if already running."""
        _init_autoreg()

        if _autoreg_runner.is_running:
            return JSONResponse(
                {"error": "already running"},
                status_code=409,
            )

        from autoreg.runner import AutoRegConfig

        # Load shared reg config từ Settings store (proxy, headless, timeout, retry)
        from db import get_engine, get_settings_repo
        from db.repositories import RepositoryError

        try:
            _repo = get_settings_repo(get_engine())
            all_settings = _repo.list()
        except RepositoryError:
            all_settings = {}

        # Resolve default_password: UI form → Settings reg.default_password → ""
        password = body.default_password
        if not password:
            password = all_settings.get("reg.default_password") or ""

        # Resolve logs_url / api_key: autoreg form → Settings mail_mode.worker_config → ""
        logs_url = body.logs_url
        api_key = body.api_key
        if not logs_url or not api_key:
            worker_cfg = all_settings.get("mail_mode.worker_config")
            if isinstance(worker_cfg, dict):
                if not logs_url:
                    logs_url = worker_cfg.get("logs_url", "")
                if not api_key:
                    api_key = worker_cfg.get("api_key", "")

        # Resolve reg_mode từ Settings — KHÔNG có override từ API body (autoreg
        # giữ pipeline mode đồng nhất per-process). Default "browser" để backward
        # compat khi key chưa từng set.
        reg_mode = str(all_settings.get("reg_mode.current") or "browser")
        if reg_mode not in ("browser", "hybrid"):
            reg_mode = "browser"

        config = AutoRegConfig(
            concurrency=body.concurrency,
            poll_interval=body.poll_interval,
            default_password=password,
            logs_url=logs_url,
            api_key=api_key,
            # Shared config từ Settings store (reg.* namespace)
            headless=all_settings.get("reg.headless", True),
            job_timeout=float(all_settings.get("reg.job_timeout", 240)),
            auto_retry=bool(all_settings.get("reg.auto_retry", False)),
            auto_retry_max=int(all_settings.get("reg.auto_retry_max", 3)),
            auto_retry_delay=float(all_settings.get("reg.auto_retry_delay", 30)),
            reg_mode=reg_mode,
            # Default True khi setting chưa có (backward compat: pool đã hoạt
            # động khi key này được thêm vào allowlist). User tắt = autoreg
            # bypass pool, mirror JobManager._use_proxy cho UI manual reg.
            use_proxy=bool(all_settings.get("reg.use_proxy", True)),
        )
        await _autoreg_runner.start(config)

        # ── Write-through to Settings_Store (R6.4) ──
        settings_dict = {
            "autoreg.concurrency": body.concurrency,
            "autoreg.poll_interval": body.poll_interval,
            "autoreg.logs_url": body.logs_url or None,
            "autoreg.api_key": body.api_key or None,
        }
        response_body: dict = {"ok": True}
        try:
            _repo.bulk_set(settings_dict)
        except RepositoryError as e:
            import logging
            logging.getLogger(__name__).warning(
                "write-through autoreg settings failed: %s", e,
            )
            response_body["settings_persist_error"] = str(e)

        return JSONResponse(response_body)

    @router.post("/autoreg/stop")
    async def autoreg_stop() -> JSONResponse:
        """Stop AutoRegRunner — non-blocking signal cancel (R9.2)."""
        _init_autoreg()
        _autoreg_runner.stop()
        return JSONResponse({"stopped": True})

    @router.get("/autoreg/status")
    async def autoreg_status() -> JSONResponse:
        """Snapshot trạng thái AutoRegRunner (R9.3)."""
        _init_autoreg()
        stats = _autoreg_runner.stats
        return JSONResponse({
            "running": _autoreg_runner.is_running,
            "processed": stats.processed,
            "success": stats.success,
            "errors": stats.errors,
            "current_cycle": _autoreg_runner.current_cycle,
        })

    # [REMOVED] GET /autoreg/stream — legacy SSE endpoint for autoreg_log channel.
    # Replaced by unified GET /api/sse (SseMux channel "autoreg_log").
    # LogBuffer.subscribe() and push() kept intact for SseMux hook.

    @router.get("/autoreg/accounts")
    async def autoreg_accounts(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
    ) -> JSONResponse:
        """Paginated list ChatGPT accounts (R9.5)."""
        _init_autoreg()

        from db import get_engine
        from db.repositories import ChatGptAccountRepository

        engine = get_engine()
        repo = ChatGptAccountRepository(engine)
        items, total = repo.list_accounts(page=page, page_size=page_size)
        return JSONResponse({"items": items, "total": total})

    return router


__all__ = ["build_icloud_router", "get_hme_log_buffer", "get_autoreg_log_buffer"]
