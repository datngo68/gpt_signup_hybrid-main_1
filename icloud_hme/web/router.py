"""[DEPRECATED] FastAPI router factory cho icloud-hme-pool R10 (bản đầu).

⚠️  RUNTIME KHÔNG MOUNT FILE NÀY ⚠️

Module này được giữ lại CHỈ vì ``test/check_router_run_auth.py`` import
``build_router`` để verify factory pattern. Mọi endpoint runtime (bao gồm
7 endpoint ``/api/icloud/run/*``) đã migrate sang ``web/icloud_routes.py``
(``build_icloud_router``) — mount qua ``web/server.py`` ở line:

    app.include_router(build_icloud_router())

Lý do migrate:
    - Auth single source of truth: ``web/auth.py:require_token`` middleware
      gate toàn bộ ``/api/*`` (env ``GPT_SIGNUP_WEB_TOKEN``, X-API-Token /
      cookie / ?token=).
    - File cũ dùng Bearer auth riêng + env riêng ``ICLOUD_API_AUTH_TOKEN``
      → 2 token độc lập gây confusing setup.
    - Module-level singleton (``_runner``, ``_log_buffer``, ``_runner_factory``)
      ở ``web/icloud_routes.py`` đơn giản hơn pattern ``services dict`` ở
      đây — không cần inject từ caller mỗi request.

Khi nào xoá hẳn:
    - Khi ``test/check_router_run_auth.py`` được rewrite để test thẳng
      ``web/icloud_routes.py:build_icloud_router`` (cần stub Settings +
      service layer ở module level).
    - Sau đó xoá file này + ``icloud_hme/web/auth.py`` + cleanup re-export
      ở ``icloud_hme/web/__init__.py``.

Refs (history):
    requirements.md icloud-hme-pool R10.1–R10.15
    requirements.md icloud-runner-loop R9.1–R9.5, R4.2, R10.6
    design.md §Components / Web Endpoints
    tasks.md icloud-runner-loop task 5.3, 5.4

Endpoints (theo design.md §Web_API table):
    GET    /api/icloud/pool/status              — IcloudPoolManager.status_report
    GET    /api/icloud/audit?...                — list audit events

Runner endpoints (icloud-runner-loop task 5.3, 5.4):
    POST   /api/icloud/run                      — start runner (R9.1, R4.2)
    POST   /api/icloud/run/stop                 — stop runner (R9.2)
    POST   /api/icloud/run/pause                — pause runner (R9.3)
    POST   /api/icloud/run/resume               — resume runner (R9.4)
    GET    /api/icloud/run/status               — runner snapshot (R9.5)
    GET    /api/icloud/run/log                  — paginated log fetch (R9.6)
    GET    /api/icloud/run/log/stream           — SSE stream log events (R9.7)

Auth (R10.10a / R9.8): mọi endpoint require ``Authorization: Bearer <token>``.

    - ``/pool/status`` + ``/audit``: per-endpoint ``dependencies=
      [Depends(auth_dep)]``.
    - ``/run/*`` (7 endpoint): wire qua sub-router ``runner_router`` với
      ``dependencies=[Depends(auth_dep)]`` ở cấp router (task 5.6) — single
      source of truth, không duplicate ở từng endpoint. Sub-router include
      vào main router trước khi return.

NOTE: ``build_router(services)`` factory wire dependencies. Khi runner cần
rebuild (retry_interval body khác ``runner.retry_interval`` hiện tại) →
gọi ``services["runner_factory"](retry_interval)`` rồi swap
``services["runner"]`` (singleton replacement). Các handler đọc
``services["runner"]`` qua ``Depends(get_runner)`` (lookup mỗi request,
không cache) nên swap có hiệu lực ngay.

Job layer endpoints đã xoá theo R11.5 — workflow điều phối qua ``HmeRunner``.

Implementation note (FastAPI + PEP 563):
    KHÔNG dùng ``from __future__ import annotations`` trong module này.
    FastAPI ``get_type_hints`` cần resolve ``RunRequest`` ở module globalns
    để nhận diện body parameter (vs query). Nếu deferred-annotation, body
    sẽ bị treat thành query → 422 ``missing field``. Top-level import
    ``RunRequest`` + non-deferred annotation đảm bảo FastAPI bind đúng.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from .schemas import RunRequest, RunStatus


async def _runner_task_wrapper(
    runner: Any,
    action: str,
    params: dict,
    log_buffer: Any,
) -> None:
    """Wrap ``runner.start()`` để exception KHÔNG bị asyncio nuốt im lặng.

    Cùng pattern với ``web/icloud_routes.py:_runner_task_wrapper`` — push
    event error vào LogBuffer + log stderr khi runner.start() raise. Giữ
    ở module này để file deprecated vẫn safe nếu có ai dùng.
    """
    import sys
    import traceback

    try:
        await runner.start(action=action, params=params)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
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
        print(
            f"[icloud_hme.web.router] runner task crashed (action={action}): "
            f"{type(exc).__name__}: {exc}\n{tb}",
            file=sys.stderr,
        )


def build_router(services: dict) -> Any:
    """Build APIRouter wired with services dict.

    Args:
        services: dict cần keys (subset tuỳ tính năng cần expose):
            - ``pool_manager``, ``audit_repo`` — pool/audit endpoints.
            - ``runner`` — current ``HmeRunner`` singleton (swap khi rebuild).
            - ``log_buffer`` — ``LogBuffer`` singleton.
            - ``runner_factory`` — callable ``(retry_interval: int | None)
              -> HmeRunner`` để rebuild Runner khi user truyền
              ``retry_interval`` khác hiện tại (R9 design note).

    Returns:
        FastAPI ``APIRouter(prefix='/api/icloud')`` đã wire sẵn handlers.

    Behavior:
        - Mọi endpoint đều phụ thuộc Bearer auth (R10.10a, R9.8).
        - Endpoint ``/run/*`` chỉ wire khi services có đủ ``runner`` +
          ``log_buffer`` — tránh AttributeError khi consumer cũ chỉ cần
          pool/audit.
    """
    try:
        from fastapi import APIRouter, Depends, HTTPException, Query, Request
        from fastapi.responses import StreamingResponse
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "FastAPI chưa cài. pip install fastapi>=0.110"
        ) from exc

    from .auth import AuthError, verify_bearer_token, verify_query_token

    router = APIRouter(prefix="/api/icloud", tags=["icloud-hme"])

    async def auth_dep(request: Request) -> None:
        """Bearer token check dependency."""
        try:
            verify_bearer_token(request.headers.get("Authorization"))
        except AuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail)

    async def auth_dep_sse(request: Request) -> None:
        """Auth dependency cho SSE: header Bearer ưu tiên, fallback ``?token=``.

        Browser ``EventSource`` không set header tùy chỉnh; cho phép truyền
        token qua query param chỉ ở endpoint SSE (task 5.5 / R9.8). Thứ tự:
            1. Header ``Authorization: Bearer <token>`` — nếu có sẽ verify
               theo flow chuẩn; thiếu/sai 401, không fallback.
            2. Nếu KHÔNG có header → check query ``?token=<token>``.
            3. Nếu cả hai đều thiếu → 401 từ ``verify_query_token``.

        Vẫn fail-fast 503 khi env ``ICLOUD_API_AUTH_TOKEN`` unset.
        """
        try:
            header = request.headers.get("Authorization")
            if header:
                verify_bearer_token(header)
                return
            verify_query_token(request.query_params.get("token"))
        except AuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail)

    # ── pool/status (R10.1) ──────────────────────────────────────────────
    if "pool_manager" in services:

        @router.get("/pool/status", dependencies=[Depends(auth_dep)])
        async def get_pool_status() -> dict:
            pool_mgr = services["pool_manager"]
            report = pool_mgr.status_report()
            return {
                "by_status": report.by_status,
                "profiles": [
                    {
                        "apple_id": p.apple_id,
                        "status": p.status,
                        "hme_count": p.hme_count,
                        "quota_remaining": p.quota_remaining,
                        "limited_until": p.limited_until.isoformat() + "Z"
                        if p.limited_until
                        else None,
                        "quota_retry_until": p.quota_retry_until.isoformat() + "Z"
                        if p.quota_retry_until
                        else None,
                        "last_used_at": p.last_used_at.isoformat() + "Z"
                        if p.last_used_at
                        else None,
                        "last_error": p.last_error,
                    }
                    for p in report.profiles
                ],
                "emails_by_status": report.emails_by_status,
                "quota_soft_cap_per_account": report.quota_soft_cap_per_account,
                "total_quota_remaining": report.total_quota_remaining,
                "low_capacity": report.low_capacity,
                "quota_full_count": report.quota_full_count,
                "quota_full_profiles": report.quota_full_profiles,
            }

    # ── audit (R10.x) ────────────────────────────────────────────────────
    if "audit_repo" in services:

        @router.get("/audit", dependencies=[Depends(auth_dep)])
        async def list_audit(
            apple_id: Optional[str] = None,
            event_type: Optional[str] = None,
            since: Optional[str] = None,
            limit: int = 100,
        ) -> list:
            audit_repo = services["audit_repo"]
            return audit_repo.list(
                apple_id=apple_id,
                event_type=event_type,
                since=since,
                limit=limit,
            )

    # ── icloud-runner-loop endpoints (task 5.3, 5.6) ─────────────────────
    if "runner" in services and "log_buffer" in services:

        def get_runner():
            """DI helper: lookup runner mỗi request (không cache).

            Cho phép swap singleton qua ``services["runner"] = new_runner``
            khi rebuild theo ``retry_interval`` mới (R9 design note).
            """
            return services["runner"]

        # Task 5.6 (R9.8): router-level Bearer auth cho mọi endpoint /run/*.
        # Single source of truth — bỏ duplicate ``dependencies=[Depends
        # (auth_dep)]`` ở từng endpoint. ``runner_router`` không có prefix
        # riêng; khi include vào ``router`` (prefix /api/icloud) full path
        # sẽ là /api/icloud/run/...
        runner_router = APIRouter(dependencies=[Depends(auth_dep)])

        @runner_router.post("/run")
        async def api_run_start(req: RunRequest) -> dict:
            """Start runner — spawn task, không await tới khi loop kết thúc.

            Behavior (R9.1, R4.2, R10.6):
                - Nếu ``runner.is_running == True`` → 409
                  ``{"error": "already_running"}``.
                - Nếu ``req.retry_interval`` khác ``runner.retry_interval``
                  hiện tại → gọi ``services["runner_factory"]
                  (req.retry_interval)`` để build Runner mới rồi swap
                  ``services["runner"]`` (singleton replacement).
                - Clear ``LogBuffer`` (R10.6) trước khi spawn task.
                - ``asyncio.create_task(runner.start(...))`` — KHÔNG await.
                - Return 200 ``{"ok": True, "action": req.action}``.
            """
            runner = services["runner"]
            if runner.is_running:
                # R4.2 / R9.1: HTTP 409 + body chứa key "error".
                raise HTTPException(
                    status_code=409,
                    detail={"error": "already_running"},
                )

            # Rebuild Runner instance nếu retry_interval khác — singleton
            # replacement qua ``runner_factory`` injected bởi caller.
            if (
                req.retry_interval is not None
                and req.retry_interval != runner.retry_interval
            ):
                factory = services.get("runner_factory")
                if factory is None:
                    # Fail-fast: thiếu factory → 500, KHÔNG silent ignore.
                    raise HTTPException(
                        status_code=500,
                        detail={
                            "error": "runner_factory_missing",
                            "message": (
                                "retry_interval khác current runner; cần "
                                "services['runner_factory'] để rebuild."
                            ),
                        },
                    )
                runner = factory(req.retry_interval)
                services["runner"] = runner  # swap singleton

            # R10.6: clear buffer + reset seq trước mỗi run mới.
            services["log_buffer"].clear()

            # R9.1: spawn task + KHÔNG await; response trả ngay.
            # Wrap để exception trong ``runner.start()`` được log + push
            # vào LogBuffer thay vì asyncio nuốt im lặng.
            asyncio.create_task(
                _runner_task_wrapper(
                    runner, req.action, req.params, services["log_buffer"]
                )
            )
            return {"ok": True, "action": req.action}

        @runner_router.post("/run/stop")
        async def api_run_stop(runner=Depends(get_runner)) -> dict:
            """Stop runner — non-blocking signal cancel (R9.2)."""
            runner.stop()
            return {"ok": True}

        @runner_router.post("/run/pause")
        async def api_run_pause(runner=Depends(get_runner)) -> dict:
            """Pause runner (R9.3)."""
            runner.pause()
            return {"ok": True}

        @runner_router.post("/run/resume")
        async def api_run_resume(runner=Depends(get_runner)) -> dict:
            """Resume runner sau pause (R9.4)."""
            runner.resume()
            return {"ok": True}

        @runner_router.get(
            "/run/status",
            response_model=RunStatus,
        )
        async def api_run_status(runner=Depends(get_runner)) -> RunStatus:
            """Snapshot trạng thái Runner hiện tại (R9.5).

            Build từ runner properties:
                - ``running``         ← ``runner.is_running``
                - ``action``          ← ``runner.current_action`` (None khi idle)
                - ``cycle``           ← ``runner.cycle_count`` (>= 0)
                - ``stats``           ← ``{created, errors, skipped}`` từ
                  ``runner.stats`` (RunnerStats dataclass)
                - ``retry_interval``  ← ``runner.retry_interval``
                - ``next_cycle_at``   ← ISO 8601 UTC string convert từ
                  ``runner.next_cycle_at`` (epoch float). None khi đang
                  trong cycle hoặc idle.

            Response model ``RunStatus`` (Pydantic) tự validate shape;
            không gọi Runner method nào ngoài property read-only nên
            an toàn race-free với loop đang chạy.
            """
            next_at = runner.next_cycle_at
            if next_at is None:
                next_iso: Optional[str] = None
            else:
                # epoch float → ISO 8601 UTC ("YYYY-MM-DDTHH:MM:SS.ffffff+00:00")
                next_iso = datetime.fromtimestamp(
                    next_at, tz=timezone.utc
                ).isoformat()

            stats = runner.stats
            return RunStatus(
                running=runner.is_running,
                action=runner.current_action,
                cycle=runner.cycle_count,
                stats={
                    "created": stats.created,
                    "errors": stats.errors,
                    "skipped": stats.skipped,
                },
                retry_interval=runner.retry_interval,
                next_cycle_at=next_iso,
            )

        # ── GET /run/log (R9.6) — paginated fetch từ LogBuffer ──────────
        @runner_router.get("/run/log")
        async def api_run_log(
            offset: int = Query(default=0, ge=0),
            limit: int = Query(default=200, ge=1, le=1000),
        ) -> dict:
            """Fetch slice các LogEvent có ``seq > offset``, tối đa ``limit``.

            Behavior (R9.6):
                - Snapshot từ ``LogBuffer.snapshot()`` (list bản sao).
                - Filter ``event.seq > offset`` — client gửi offset = seq
                  cuối cùng đã nhận; lần đầu gửi 0 để lấy từ đầu.
                - Take first ``limit`` events sau filter (deque ordered theo
                  thứ tự push, seq monotonic → đã sorted).
                - ``next_offset``: max(seq) trong events trả về, hoặc giữ
                  nguyên ``offset`` nếu rỗng (client poll tiếp với offset
                  cũ → idempotent).

            Defaults:
                - ``offset = 0`` (lấy từ đầu).
                - ``limit = 200`` clamp ``1..1000`` — đủ để UI fetch đợt
                  đầu mà không tải hết 10k entry; client tăng ``limit``
                  tối đa 1000 nếu cần backfill nhiều.

            Response:
                ``{"events": [LogEvent.model_dump()...], "next_offset": int}``
            """
            buffer = services["log_buffer"]
            entries = buffer.snapshot()
            # Filter + take first limit. Deque ordered theo seq tăng dần
            # (push tăng _seq mỗi lần) → slice tuần tự là OK.
            selected = []
            for ev in entries:
                if ev.seq > offset:
                    selected.append(ev)
                    if len(selected) >= limit:
                        break

            if selected:
                next_offset = selected[-1].seq
            else:
                next_offset = offset

            return {
                "events": [ev.model_dump() for ev in selected],
                "next_offset": next_offset,
            }

        # ── GET /run/log/stream (R9.7) — SSE stream LogEvent ────────────
        # Đăng ký trực tiếp trên ``router`` chính (KHÔNG qua ``runner_router``)
        # vì FastAPI ``dependencies`` ở route-level là APPEND chứ không
        # OVERRIDE router-level. Nếu vẫn nằm trong ``runner_router`` thì
        # ``auth_dep`` (Bearer-only) sẽ reject 401 trước khi
        # ``auth_dep_sse`` có cơ hội check query ``?token=``.
        @router.get(
            "/run/log/stream",
            dependencies=[Depends(auth_dep_sse)],
        )
        async def api_run_log_stream() -> StreamingResponse:
            """Server-Sent Events stream LogEvent từ LogBuffer.

            Behavior (R9.7):
                - Subscribe qua ``LogBuffer.subscribe()`` (async iterator);
                  replay history trước, sau đó stream event mới push.
                - Format mỗi event: ``data: <model_dump_json>\\n\\n``
                  (SSE spec: empty line phân tách event).
                - Khi client disconnect → ASGI cancel async generator →
                  ``subscribe()`` finally block discard subscriber khỏi
                  ``_subscribers`` set (no leak).
                - Content-Type ``text/event-stream``.

            Auth (task 5.5 / R9.8):
                - Header ``Authorization: Bearer <token>`` khi client gọi
                  được (curl, fetch + custom header).
                - Hoặc ``?token=<token>`` query param cho browser
                  ``EventSource`` (không set header tùy chỉnh được).

            Response headers (chống proxy buffering):
                - ``Cache-Control: no-cache`` — browser/CDN không cache stream.
                - ``X-Accel-Buffering: no`` — vô hiệu nginx/Cloudflare buffer
                  mặc định để event tới UI ngay.
                - ``Connection: keep-alive`` — giữ socket mở cho stream.
            """
            buffer = services["log_buffer"]

            async def event_stream():
                async for event in buffer.subscribe():
                    yield f"data: {event.model_dump_json()}\n\n"

            return StreamingResponse(
                event_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                },
            )

        # Task 5.6: include sub-router /run/* với router-level Bearer auth
        # vào main router. Mọi endpoint /run/* sẽ inherit dependency
        # ``Depends(auth_dep)`` — single source of truth, no duplicate.
        router.include_router(runner_router)

    return router


__all__ = ["build_router"]
