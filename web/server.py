"""FastAPI server cho web UI gpt_signup_hybrid."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import get_token, require_token  # token-based auth
from .manager import get_manager, get_session_manager, get_link_manager, get_upi_manager, set_sse_mux
from .mail_modes import get_registry, serialize_for_api
from .sse_mux import SseMux
from payment_link import REGION_BILLING

_log = logging.getLogger(__name__)

# ── Unified SSE Multiplexer singleton ──
_sse_mux = SseMux()
set_sse_mux(_sse_mux)  # Inject into manager module (avoids circular import)


def get_sse_mux() -> SseMux:
    """Return the module-level SseMux singleton."""
    return _sse_mux


_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _asset_version() -> str:
    """Build a lightweight cache-busting token from static file mtimes."""
    latest_mtime = 0
    for path in _STATIC_DIR.glob("*"):
        if path.is_file():
            latest_mtime = max(latest_mtime, path.stat().st_mtime_ns)
    return str(latest_mtime or 1)


app = FastAPI(title="gpt_signup_hybrid web UI", version="0.1.0")

# Module-level engine reference for graceful shutdown
_engine = None

# ── Launch flags truyền từ CLI → server ──
# QUAN TRỌNG: CLI import module này dưới tên `web.server`, còn uvicorn chạy app
# qua import string `gpt_signup_hybrid.web.server` → đây là HAI module object
# KHÁC NHAU trong sys.modules (cùng file, khác tên). Vì vậy KHÔNG thể chỉ set
# biến module-level từ CLI rồi mong route đọc được. Phải truyền qua os.environ
# (env kế thừa qua cả subprocess khi --reload). Biến module-level giữ làm cache.
_ENV_LOOPBACK_BIND = "GSH_WEB_LOOPBACK_BIND"
_ENV_HIDE_REG = "GSH_WEB_HIDE_REG"
_ENV_DISABLE_AUTH = "GSH_WEB_DISABLE_AUTH"

# Track whether server is bound to loopback (safe to embed token in HTML)
_is_loopback_bind: bool = True

# Track "hide Reg" launch mode — khi True, frontend ẩn tab Reg (vẫn giữ
# Get Session / UPI QR / Settings). Dùng để giao máy cho người khác mà không
# cho chạy Reg. Đây là launch flag (giống host/port), KHÔNG persist Settings Store.
_hide_reg: bool = False

# Track "disable auth" launch mode — khi True, BỎ QUA token check cho /api/*.
# ⚠️ INSECURE: bất kỳ ai reach được server (kể cả qua tunnel public) đều xem
# được credentials + điều khiển job. Chỉ bật khi hiểu rõ rủi ro (opt-in flag).
_disable_auth: bool = False


def set_loopback_bind(is_loopback: bool) -> None:
    """Gọi từ CLI trước khi start server để set bind mode."""
    global _is_loopback_bind  # noqa: PLW0603
    _is_loopback_bind = is_loopback
    os.environ[_ENV_LOOPBACK_BIND] = "1" if is_loopback else "0"


def set_hide_reg(hide_reg: bool) -> None:
    """Gọi từ CLI trước khi start server để bật chế độ ẩn tab Reg."""
    global _hide_reg  # noqa: PLW0603
    _hide_reg = hide_reg
    os.environ[_ENV_HIDE_REG] = "1" if hide_reg else "0"


def set_disable_auth(disable_auth: bool) -> None:
    """Gọi từ CLI trước khi start server để TẮT token auth (insecure, opt-in)."""
    global _disable_auth  # noqa: PLW0603
    _disable_auth = disable_auth
    os.environ[_ENV_DISABLE_AUTH] = "1" if disable_auth else "0"


def _is_loopback_bind_enabled() -> bool:
    """Env ưu tiên (vượt ranh giới module-identity), fallback biến module."""
    val = os.environ.get(_ENV_LOOPBACK_BIND)
    if val is not None:
        return val == "1"
    return _is_loopback_bind


def _hide_reg_enabled() -> bool:
    """Env ưu tiên (vượt ranh giới module-identity), fallback biến module."""
    val = os.environ.get(_ENV_HIDE_REG)
    if val is not None:
        return val == "1"
    return _hide_reg


def _disable_auth_enabled() -> bool:
    """Env ưu tiên (vượt ranh giới module-identity), fallback biến module."""
    val = os.environ.get(_ENV_DISABLE_AUTH)
    if val is not None:
        return val == "1"
    return _disable_auth


@app.on_event("startup")
async def on_startup():
    """Initialize SQLite engine + repos and pass job_repo to JobManager."""
    global _engine
    from db import get_engine, get_repos

    _engine = get_engine()
    combo_repo, job_repo, session_repo = get_repos(_engine)

    # ── Settings hydration (R9) — TRƯỚC recovery ──
    # Load settings từ DB 1 lần, truyền vào managers qua apply_settings().
    # Phải chạy trước recover_jobs() để job_timeout/proxy/headless đúng khi
    # worker bắt đầu process recovered jobs.
    from db.repositories import RepositoryError

    settings_repo = _get_settings_repo()
    try:
        all_settings = settings_repo.list()
    except RepositoryError:
        _log.warning("Settings load failed at startup, using defaults")
        all_settings = {}

    # Initialize manager with all repos (triggers recovery)
    get_manager(job_repo=job_repo, combo_repo=combo_repo, session_repo=session_repo)
    # Initialize session + link managers with job_repo (triggers recovery)
    get_session_manager(job_repo=job_repo)
    get_link_manager(job_repo=job_repo)
    # UPI manager — in-memory only, không cần job_repo.
    get_upi_manager()

    # Hydrate managers với settings từ DB (R9.1, R9.2, R9.3)
    # Workers đã được schedule bởi _ensure_workers() nhưng chưa execute
    # (event loop chưa yield) → apply_settings trước khi worker chạy thực tế.
    get_manager().apply_settings(all_settings)
    get_session_manager().apply_settings(all_settings)
    get_link_manager().apply_settings(all_settings)
    get_upi_manager().apply_settings(all_settings)
    # Telegram notifier — hydrate config (token/chat_id/notify toggle) từ DB.
    from .telegram_notifier import get_telegram_notifier
    get_telegram_notifier().apply_settings(all_settings)

    # Cloudflare Quick Tunnel — hydrate enabled từ DB; auto-start nếu bật.
    # CLI đã set local endpoint (host/port) trước khi gọi uvicorn.run.
    from .cloudflare_tunnel import get_cloudflare_tunnel
    tunnel = get_cloudflare_tunnel()
    tunnel.apply_settings(all_settings)
    if tunnel.enabled:
        # Schedule task — start_async đợi URL nên không block startup.
        asyncio.create_task(tunnel.start_async(), name="cloudflare-tunnel-start")

    _log.info("startup: SQLite engine initialized, settings hydrated, job recovery done")

    # ── Register SseMux snapshot functions (Requirements 5.1, 5.2, 5.3) ──
    # Each lambda captures the manager/buffer reference and builds the snapshot
    # matching the format already used by the legacy per-channel SSE endpoints.
    from .icloud_routes import get_hme_log_buffer, get_autoreg_log_buffer

    manager = get_manager()
    sm = get_session_manager()
    lm = get_link_manager()
    um = get_upi_manager()

    _sse_mux.register_snapshot("reg", lambda: [{
        "type": "snapshot",
        "max_concurrent": manager.max_concurrent,
        "headless": manager.headless,
        "debug": manager.debug,
        "job_timeout": manager.job_timeout,
        "post_reg_get_session": manager.post_reg_get_session,
        "post_reg_get_link": manager.post_reg_get_link,
        "post_reg_link_region": manager.post_reg_link_region,
        "auto_retry": manager.auto_retry,
        "auto_retry_max": manager.auto_retry_max,
        "auto_retry_delay": manager.auto_retry_delay,
        "use_proxy": manager.use_proxy,
        "jobs": manager.list_jobs(),
    }])

    _sse_mux.register_snapshot("session", lambda: [{
        "type": "snapshot",
        "max_concurrent": sm.max_concurrent,
        "job_timeout": sm.job_timeout,
        "jobs": sm.list_jobs(),
    }])

    _sse_mux.register_snapshot("link", lambda: [{
        "type": "snapshot",
        "max_concurrent": lm.max_concurrent,
        "job_timeout": lm.job_timeout,
        "region": lm.region,
        "jobs": lm.list_jobs(),
    }])

    _sse_mux.register_snapshot("upi", lambda: [{
        "type": "snapshot",
        "max_concurrent": um.max_concurrent,
        "job_timeout": um.job_timeout,
        "approve_retries": um.approve_retries,
        "approve_retry_delay": um.approve_retry_delay,
        "restart_threshold": um.restart_threshold,
        "max_restarts": um.max_restarts,
        "jobs": um.list_jobs(),
    }])

    def _hme_log_snapshot() -> list[dict]:
        buf = get_hme_log_buffer()
        if buf is None:
            return []
        return [e.model_dump() for e in buf.snapshot()]

    _sse_mux.register_snapshot("hme_log", _hme_log_snapshot)

    def _autoreg_log_snapshot() -> list[dict]:
        buf = get_autoreg_log_buffer()
        if buf is None:
            return []
        return [e.model_dump() for e in buf.snapshot()]

    _sse_mux.register_snapshot("autoreg_log", _autoreg_log_snapshot)

    # ── Multi-worker guard cho HmeRunner singleton (icloud-runner-loop) ──
    # ``web/icloud_routes.py`` dùng module-level singleton ``_runner`` /
    # ``_log_buffer`` — KHÔNG share giữa worker processes. Nếu deploy
    # uvicorn/gunicorn với --workers >= 2: POST /run/* tới worker A,
    # GET /status tới worker B → state mismatch.
    #
    # Phát hiện qua env phổ biến:
    #   - WEB_CONCURRENCY (gunicorn / uvicorn standard)
    #   - GUNICORN_CMD_ARGS (gunicorn pre-fork)
    #   - UVICORN_WORKERS (uvicorn config)
    #
    # Hành vi: warn (không fail-fast) — user có thể vẫn muốn deploy
    # multi-worker cho các tab khác (signup/session/link), miễn là KHÔNG
    # dùng tab iCloud HME concurrent. Cross-process RunnerLock vẫn cover
    # case race ở pool reserve nên không phá data; chỉ UI trông inconsistent.
    import os as _os

    _worker_count_hints = []
    for env_key in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        val = _os.environ.get(env_key, "").strip()
        if val:
            try:
                if int(val) > 1:
                    _worker_count_hints.append(f"{env_key}={val}")
            except ValueError:
                pass
    if _worker_count_hints:
        _log.warning(
            "Multi-worker deployment detected (%s) but icloud-runner-loop "
            "uses module-level singleton. Stop/Status endpoints có thể "
            "rơi vào worker khác → state mismatch UI. Cross-process "
            "RunnerLock vẫn chặn race ở DB pool. Khuyến nghị: chạy --workers 1 "
            "cho deployment dùng tab iCloud HME, hoặc tách iCloud sang "
            "service riêng.",
            ", ".join(_worker_count_hints),
        )


# ─── Auth middleware ───────────────────────────────────────────────────
# Token-based auth gates all /api/* routes.


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Gate /api/* routes bằng token. Static + index không cần token."""
    path = request.url.path
    # Skip auth cho gopay-check endpoint (extension gọi trực tiếp, không có token)
    # và khi --no-auth được bật (opt-in insecure mode).
    if (
        not _disable_auth_enabled()
        and path.startswith("/api")
        and not path.startswith("/api/gopay-check/")
    ):
        try:
            require_token(request)
        except HTTPException as exc:
            return JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers=exc.headers,
            )
    response = await call_next(request)
    return response


# ─────────────────────────────────────────────────────────────────────
# API
# ─────────────────────────────────────────────────────────────────────


class AddJobsRequest(BaseModel):
    combos: str = Field(..., description="Textarea content, nhiều combo cách nhau bằng newline.")
    default_password: str | None = Field(
        default=None,
        description="Password mặc định cho tất cả job. Nếu null → random.",
    )
    mail_mode: str = Field(
        default="icloud_v3",
        description=(
            "Mail mode: 'icloud_v3' (default), 'outlook', 'worker', "
            "'gmail_advanced', hoặc 'china_icloud'."
        ),
    )
    reg_mode: str = Field(
        default="browser",
        description="Registration mode: 'browser' (anti-detect, default) hoặc 'hybrid' (curl_cffi Firefox + Camoufox sentinel, recommended).",
    )
    email_logs_url: str | None = Field(
        default=None,
        description="[worker] Worker API URL.",
    )
    email_api_key: str | None = Field(
        default=None,
        description="[worker] Bearer token (VIEW_TOKEN).",
    )


class SetConfigRequest(BaseModel):
    # Reg cap [1, 30] (yêu cầu sản phẩm). Schema không set le để FE gửi value
    # > 30 vẫn được handler clamp (không trả 422), giữ tương thích các phiên
    # cũ khi dropdown từng share giữa các tab.
    max_concurrent: int | None = Field(default=None, ge=1)
    headless: bool | None = Field(default=None)
    debug: bool | None = Field(default=None)
    job_timeout: float | None = Field(default=None, ge=30, le=600)
    post_reg_get_session: bool | None = None
    post_reg_get_link: bool | None = None
    post_reg_link_region: str | None = Field(
        default=None,
        description="Region cho post-reg get-link (VN | ID | IN | US).",
    )
    auto_retry: bool | None = None
    auto_retry_max: int | None = Field(default=None, ge=1, le=10)
    auto_retry_delay: float | None = Field(default=None, ge=5, le=120)
    use_proxy: bool | None = Field(
        default=None,
        description="Bật/tắt áp dụng proxy pool cho Reg jobs. False = chạy direct.",
    )


@app.get("/api/jobs")
async def list_jobs() -> JSONResponse:
    manager = get_manager()
    return JSONResponse({
        "max_concurrent": manager.max_concurrent,
        "headless": manager.headless,
        "debug": manager.debug,
        "job_timeout": manager.job_timeout,
        "post_reg_link_region": manager.post_reg_link_region,
        "jobs": manager.list_jobs(),
    })


@app.get("/api/jobs/secrets")
async def get_jobs_secrets() -> JSONResponse:
    """Trả secrets (password/secret/first_code/session_path) cho mọi job.

    Auth gate đã cover bởi middleware. Endpoint riêng để list jobs default
    không leak secrets nếu caller chỉ subscribe SSE.
    """
    manager = get_manager()
    return JSONResponse({"secrets": manager.get_secrets_map()})


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> JSONResponse:
    manager = get_manager()
    data = manager.get_job(job_id)
    if data is None:
        raise HTTPException(404, "job not found")
    return JSONResponse(data)


@app.get("/api/jobs/{job_id}/log")
async def get_job_log(job_id: str) -> JSONResponse:
    manager = get_manager()
    if job_id not in manager.jobs:
        raise HTTPException(404, "job not found")
    return JSONResponse({"job_id": job_id, "log": manager.get_log(job_id)})


@app.post("/api/jobs")
async def add_jobs(payload: AddJobsRequest) -> JSONResponse:
    # Validate mail_mode
    if payload.mail_mode not in get_registry():
        raise HTTPException(422, f"unknown mail_mode: {payload.mail_mode}")

    # Build worker_config nếu mode = worker
    worker_config = None
    if payload.mail_mode == "worker":
        url = (payload.email_logs_url or "").strip()
        if not url.startswith(("http://", "https://")):
            raise HTTPException(422, "email_logs_url must start with http:// or https://")
        worker_config = {"logs_url": url, "api_key": (payload.email_api_key or "").strip()}

    combos = payload.combos.splitlines()
    manager = get_manager()
    jobs = manager.add_jobs(
        combos,
        default_password=payload.default_password,
        mail_mode=payload.mail_mode,
        worker_config=worker_config,
        reg_mode=payload.reg_mode,
    )
    return JSONResponse({"added": len(jobs), "jobs": [j.to_dict() for j in jobs]})


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str) -> JSONResponse:
    manager = get_manager()
    if job_id not in manager.jobs:
        raise HTTPException(404, "job not found")
    ok = await manager.retry_job(job_id)
    if not ok:
        raise HTTPException(503, "storage temporarily unavailable")
    return JSONResponse({"ok": True})


class RerunLinkRequest(BaseModel):
    region: str | None = Field(
        default=None,
        description="Region override (VN | ID | IN | US). Nếu omit → dùng region snapshot của job.",
    )


@app.post("/api/jobs/{job_id}/rerun-link")
async def rerun_link_job(job_id: str, payload: RerunLinkRequest | None = None) -> JSONResponse:
    """Re-fetch payment link cho 1 Reg job đã có session.

    Đọc access_token từ session.json đã save, không re-login.
    Job phải có session_path và không đang running.
    """
    manager = get_manager()
    if job_id not in manager.jobs:
        raise HTTPException(404, "job not found")
    region = payload.region if payload else None
    ok = await manager.rerun_link_for_job(job_id, region=region)
    if not ok:
        raise HTTPException(409, "job không thể rerun (đang chạy hoặc thiếu session)")
    return JSONResponse({"ok": True})


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str) -> JSONResponse:
    manager = get_manager()
    if job_id not in manager.jobs:
        raise HTTPException(404, "job not found")
    ok = manager.remove_job(job_id)
    if not ok:
        raise HTTPException(500, "failed to delete job from storage")
    return JSONResponse({"ok": True})


@app.post("/api/jobs/stop-all")
async def stop_all_jobs() -> JSONResponse:
    """Cancel tất cả jobs đang running/queued."""
    manager = get_manager()
    stopped = await manager.stop_all()
    return JSONResponse({"stopped": stopped})


@app.post("/api/jobs/clear-finished")
async def clear_finished_jobs() -> JSONResponse:
    """Xoá tất cả jobs đã xong khỏi memory (giải phóng RAM)."""
    manager = get_manager()
    removed = manager.clear_finished()
    if removed < 0:
        raise HTTPException(503, "storage temporarily unavailable")
    return JSONResponse({"removed": removed})


@app.post("/api/jobs/clear-all")
async def clear_all_jobs() -> JSONResponse:
    """Xoá TẤT CẢ jobs (mọi status) khỏi memory và SQLite."""
    manager = get_manager()
    removed = await manager.clear_all()
    if removed < 0:
        raise HTTPException(503, "storage temporarily unavailable")
    return JSONResponse({"removed": removed})


@app.post("/api/jobs/retry-failed")
async def retry_failed_jobs() -> JSONResponse:
    """Retry tất cả jobs có status error hoặc cancelled."""
    manager = get_manager()
    retried = await manager.retry_failed()
    return JSONResponse({"retried": retried})


@app.get("/api/config")
async def get_config() -> JSONResponse:
    manager = get_manager()
    return JSONResponse({
        "max_concurrent": manager.max_concurrent,
        "headless": manager.headless,
        "debug": manager.debug,
        "job_timeout": manager.job_timeout,
        "post_reg_get_session": manager.post_reg_get_session,
        "post_reg_get_link": manager.post_reg_get_link,
        "post_reg_link_region": manager.post_reg_link_region,
        "auto_retry": manager.auto_retry,
        "auto_retry_max": manager.auto_retry_max,
        "auto_retry_delay": manager.auto_retry_delay,
        "use_proxy": manager.use_proxy,
    })


@app.post("/api/config")
async def set_config(payload: SetConfigRequest) -> JSONResponse:
    manager = get_manager()
    sm = get_session_manager()
    lm = get_link_manager()
    # Clamp 1 lần — dùng cho cả manager apply và write-through Settings Store.
    # Reg cap [1, 30] (yêu cầu sản phẩm). FE đã render dropdown đúng cap, ở
    # đây chỉ defensive clamp khi client cũ hoặc API bên ngoài gửi value lệch.
    max_concurrent_clamped: int | None = (
        max(1, min(payload.max_concurrent, 30))
        if payload.max_concurrent is not None else None
    )
    if payload.max_concurrent is not None:
        try:
            manager.set_max_concurrent(max_concurrent_clamped)  # type: ignore[arg-type]
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    if payload.headless is not None:
        manager.set_headless(payload.headless)
        # Lan headless sang Session + Link manager (giống pattern proxy)
        sm.set_headless(payload.headless)
        lm.set_headless(payload.headless)
    if payload.debug is not None:
        manager.set_debug(payload.debug)
        # Lan debug sang Session + Link manager (giống pattern proxy/headless)
        sm.set_debug(payload.debug)
        lm.set_debug(payload.debug)
    if payload.job_timeout is not None:
        try:
            manager.set_job_timeout(payload.job_timeout)
            sm.set_job_timeout(payload.job_timeout)
            lm.set_job_timeout(payload.job_timeout)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    if payload.post_reg_get_session is not None:
        manager.set_post_reg_get_session(payload.post_reg_get_session)
    if payload.post_reg_get_link is not None:
        manager.set_post_reg_get_link(payload.post_reg_get_link)
    if payload.post_reg_link_region is not None:
        try:
            manager.set_post_reg_link_region(payload.post_reg_link_region)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    if payload.auto_retry is not None:
        manager.set_auto_retry(
            payload.auto_retry,
            max_retries=payload.auto_retry_max,
            delay=payload.auto_retry_delay,
        )
        sm.set_auto_retry(
            payload.auto_retry,
            max_retries=payload.auto_retry_max,
            delay=payload.auto_retry_delay,
        )
        lm.set_auto_retry(
            payload.auto_retry,
            max_retries=payload.auto_retry_max,
            delay=payload.auto_retry_delay,
        )
    if payload.use_proxy is not None:
        manager.set_use_proxy(payload.use_proxy)

    # ── Write-through to Settings_Store (R6.1, R6.2, R6.7) ──
    from db.repositories import RepositoryError

    settings_dict: dict[str, Any] = {}
    if max_concurrent_clamped is not None:
        settings_dict["reg.max_concurrent"] = max_concurrent_clamped
    if payload.headless is not None:
        settings_dict["reg.headless"] = payload.headless
    if payload.debug is not None:
        settings_dict["reg.debug"] = payload.debug
    if payload.job_timeout is not None:
        settings_dict["reg.job_timeout"] = int(payload.job_timeout)
    if payload.post_reg_get_session is not None:
        settings_dict["reg.post_reg_get_session"] = payload.post_reg_get_session
    if payload.post_reg_get_link is not None:
        settings_dict["reg.post_reg_get_link"] = payload.post_reg_get_link
    if payload.post_reg_link_region is not None:
        settings_dict["reg.post_reg_link_region"] = payload.post_reg_link_region
    if payload.auto_retry is not None:
        settings_dict["reg.auto_retry"] = payload.auto_retry
    if payload.auto_retry_max is not None:
        settings_dict["reg.auto_retry_max"] = payload.auto_retry_max
    if payload.auto_retry_delay is not None:
        settings_dict["reg.auto_retry_delay"] = int(payload.auto_retry_delay)
    if payload.use_proxy is not None:
        settings_dict["reg.use_proxy"] = payload.use_proxy

    response_body = {
        "max_concurrent": manager.max_concurrent,
        "headless": manager.headless,
        "debug": manager.debug,
        "job_timeout": manager.job_timeout,
        "post_reg_get_session": manager.post_reg_get_session,
        "post_reg_get_link": manager.post_reg_get_link,
        "post_reg_link_region": manager.post_reg_link_region,
        "auto_retry": manager.auto_retry,
        "auto_retry_max": manager.auto_retry_max,
        "auto_retry_delay": manager.auto_retry_delay,
        "use_proxy": manager.use_proxy,
    }

    if settings_dict:
        try:
            _get_settings_repo().bulk_set(settings_dict)
        except RepositoryError as e:
            _log.warning("write-through settings failed: %s", e)
            response_body["settings_persist_error"] = str(e)

    return JSONResponse(response_body)


@app.get("/api/mail-modes")
async def list_mail_modes() -> JSONResponse:
    """Trả danh sách mail modes cho UI render selector + config panels."""
    return JSONResponse({"modes": serialize_for_api()})


# ─────────────────────────────────────────────────────────────────────
# Proxy test
# ─────────────────────────────────────────────────────────────────────


# ── Proxy pool (rotation nhiều proxy) ──────────────────────────────────


async def _probe_one_proxy(proxy: str | None) -> dict[str, Any]:
    """Probe 1 proxy → {proxy, ok, public_ip, detail}.

    Dùng endpoint dual-stack để KHÔNG báo nhầm dead khi proxy egress IPv6:
      1. ``chatgpt.com/cdn-cgi/trace`` — target thật (Cloudflare), trả egress IP
         qua field ``ip=``. IPv4 + IPv6 đều OK. Nếu reach được → proxy dùng được
         cho tool.
      2. Fallback ``api64.ipify.org`` — dual-stack IP echo (api.ipify.org cũ là
         IPv4-only → ConnectError với proxy IPv6).
    Chỉ cần 1 endpoint reachable → coi là live.
    """
    import time as _time
    import httpx as _httpx
    from .proxy_format import materialize_proxy

    timeout = _httpx.Timeout(connect=10.0, read=15.0, write=10.0, pool=10.0)
    client_kwargs: dict[str, Any] = {"timeout": timeout, "follow_redirects": False}
    if proxy:
        # Pool lưu raw line/template → materialize concrete URL cho httpx (F-D).
        # `proxy` (raw line) giữ nguyên làm pool key cho mark_dead/mark_alive.
        try:
            client_kwargs["proxy"] = materialize_proxy(proxy)
        except ValueError:
            return {"proxy": proxy, "ok": False, "public_ip": None, "detail": "bad format"}

    public_ip: str | None = None
    ok = False
    detail = ""
    last_err = ""

    try:
        async with _httpx.AsyncClient(**client_kwargs) as client:
            # ── Probe 1: target thật chatgpt.com (Cloudflare trace) ──
            t0 = _time.monotonic()
            try:
                r = await client.get("https://chatgpt.com/cdn-cgi/trace")
                elapsed = (_time.monotonic() - t0) * 1000
                if r.status_code < 500:
                    ok = True
                    # Parse "ip=<egress>" từ format key=value\n
                    for line in r.text.splitlines():
                        if line.startswith("ip="):
                            public_ip = line[3:].strip()
                            break
                    detail = f"chatgpt.com HTTP {r.status_code} in {elapsed:.0f}ms"
                else:
                    last_err = f"chatgpt.com HTTP {r.status_code}"
            except Exception as exc:  # noqa: BLE001
                last_err = f"chatgpt.com {type(exc).__name__}: {exc!r}"

            # ── Probe 2 (fallback): dual-stack IP echo ──
            if not ok:
                t1 = _time.monotonic()
                try:
                    r = await client.get("https://api64.ipify.org?format=json")
                    elapsed = (_time.monotonic() - t1) * 1000
                    if r.status_code < 500:
                        ok = True
                        try:
                            public_ip = r.json().get("ip")
                        except Exception:
                            pass
                        detail = f"ipify HTTP {r.status_code} in {elapsed:.0f}ms"
                    else:
                        last_err = f"ipify HTTP {r.status_code}"
                except Exception as exc:  # noqa: BLE001
                    last_err = f"ipify {type(exc).__name__}: {exc!r}"
    except Exception as exc:  # noqa: BLE001
        last_err = f"{type(exc).__name__}: {exc!r}"

    if not ok:
        detail = last_err or "unreachable"

    # `detail`/`{exc!r}` có thể nhúng URL materialized (creds/SID) → sanitize trước
    # khi trả UI (F-E). `proxy` giữ raw line (UI match theo string + đã ở textarea).
    from .proxy_format import sanitize_proxy_text
    return {
        "proxy": proxy, "ok": ok, "public_ip": public_ip,
        "detail": sanitize_proxy_text(detail),
    }


class SaveProxyPoolRequest(BaseModel):
    proxies: list[str] = Field(
        default_factory=list,
        description="Danh sách proxy URL để xoay vòng. Empty = tắt pool.",
    )
    rotation_mode: str = Field(
        default="round_robin",
        description="round_robin | random | probe",
    )


class TestAllProxyRequest(BaseModel):
    proxies: list[str] | None = Field(
        default=None,
        description="Danh sách proxy cần test. None = dùng pool đã lưu.",
    )


@app.get("/api/proxy/pool")
async def get_proxy_pool_config() -> JSONResponse:
    """Trả cấu hình pool đã lưu (DB) + trạng thái runtime (live/dead)."""
    from .proxy_pool import get_proxy_pool

    repo = _get_settings_repo()
    stored = repo.get("proxy.pool") or []
    mode = repo.get("proxy.rotation_mode") or "round_robin"
    pool = get_proxy_pool()
    return JSONResponse({
        "proxies": stored,
        "rotation_mode": mode,
        "runtime": pool.status(),
    })


@app.post("/api/proxy/pool")
async def save_proxy_pool(payload: SaveProxyPoolRequest) -> JSONResponse:
    """Lưu pool vào Settings Store + reconfigure runtime ProxyPool (write-through)."""
    from db.repositories import RepositoryError
    from .proxy_pool import get_proxy_pool, normalize_proxies

    mode = payload.rotation_mode if payload.rotation_mode in ("round_robin", "random", "probe") else "round_robin"
    proxies = normalize_proxies(payload.proxies)

    # Reconfigure runtime trước (reset dead-set để proxy mới được thử lại).
    pool = get_proxy_pool()
    pool.configure(proxies, mode=mode)
    pool.reset_dead()

    # Write-through Settings Store (single source of truth).
    persist_error: str | None = None
    try:
        _get_settings_repo().bulk_set({
            "proxy.pool": proxies,
            "proxy.rotation_mode": mode,
        })
    except RepositoryError as exc:
        persist_error = str(exc)
        _log.warning("write-through proxy.pool failed: %s", exc)

    body: dict[str, Any] = {
        "proxies": proxies,
        "rotation_mode": mode,
        "runtime": pool.status(),
    }
    if persist_error:
        body["settings_persist_error"] = persist_error
    return JSONResponse(body)


@app.post("/api/proxy/test-all")
async def test_all_proxy(payload: TestAllProxyRequest) -> JSONResponse:
    """Test song song nhiều proxy. Trả kết quả từng proxy + mark live/dead vào pool.

    Nếu ``proxies`` rỗng/None → test danh sách pool đã lưu trong DB.
    """
    from .proxy_pool import get_proxy_pool, normalize_proxies

    if payload.proxies is not None:
        targets = normalize_proxies(payload.proxies)
    else:
        targets = normalize_proxies(_get_settings_repo().get("proxy.pool") or [])

    if not targets:
        return JSONResponse({"results": [], "live": 0, "dead": 0, "total": 0})

    results = await asyncio.gather(*[_probe_one_proxy(p) for p in targets])

    # Cập nhật dead-set runtime theo kết quả test (chỉ với proxy thuộc pool).
    pool = get_proxy_pool()
    live = 0
    for item in results:
        if item["ok"]:
            live += 1
            pool.mark_alive(item["proxy"])
        else:
            pool.mark_dead(item["proxy"])

    return JSONResponse({
        "results": results,
        "live": live,
        "dead": len(results) - live,
        "total": len(results),
    })


# ─────────────────────────────────────────────────────────────────────
# Unified SSE Endpoint (all channels multiplexed)
# ─────────────────────────────────────────────────────────────────────


@app.api_route("/api/sse", methods=["GET", "POST"])
async def unified_sse(request: Request) -> StreamingResponse:
    """Single unified SSE endpoint for all channels.

    Hỗ trợ cả GET (EventSource) lẫn POST. POST bắt buộc khi chạy sau Cloudflare
    Quick Tunnel: cloudflared buffer SSE qua GET và chỉ flush khi server đóng
    connection (cloudflare/cloudflared#1449); POST thì stream real-time. FE
    dùng fetch(POST)+ReadableStream, gửi token qua header X-API-Token.
    """
    sub_id, queue = _sse_mux.subscribe()

    async def gen():
        try:
            # 1. Send snapshots for all 6 channels
            snapshots = _sse_mux.generate_snapshots()
            for snap in snapshots:
                yield f"data: {json.dumps(snap)}\n\n"

            # 2. Stream live events with heartbeat
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=5.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                except (asyncio.CancelledError, GeneratorExit):
                    break
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            _sse_mux.unsubscribe(sub_id)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            # no-transform chặn Cloudflare nén/transform response — compression
            # layer ở edge là nguyên nhân gom buffer SSE (cloudflared#1496).
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )





@app.on_event("shutdown")
async def on_shutdown():
    """Graceful shutdown: mark running jobs for recovery, close DB, then clear SSE."""
    # 1. Mark running jobs as queued for recovery on next startup + cancel workers
    manager = get_manager()
    manager.shutdown()

    # Shutdown session + link managers (cancel workers)
    sm = get_session_manager()
    sm.shutdown()
    lm = get_link_manager()
    lm.shutdown()
    um = get_upi_manager()
    um.shutdown()

    # Stop AutoRegRunner if it was initialized and is running
    from .icloud_routes import get_autoreg_runner

    autoreg_runner = get_autoreg_runner()
    if autoreg_runner is not None and autoreg_runner.is_running:
        autoreg_runner.stop()
        _log.info("shutdown: AutoRegRunner stopped")

    # Stop Cloudflare Tunnel — graceful, swallow lỗi để không block shutdown.
    try:
        from .cloudflare_tunnel import get_cloudflare_tunnel
        await get_cloudflare_tunnel().stop_async()
        _log.info("shutdown: cloudflare tunnel stopped")
    except Exception as exc:  # noqa: BLE001
        _log.warning("shutdown: stop cloudflare tunnel failed: %s", exc)

    # 2. Close SQLite engine (wait for in-flight transactions)
    if _engine is not None:
        _engine.close()
        _log.info("shutdown: SQLite engine closed")

    # 3. (Legacy SSE subscriber queues removed — unified SseMux handles cleanup
    # via unsubscribe() in each connection's finally block.)


# ─────────────────────────────────────────────────────────────────────
# Session API (Get Session feature)
# ─────────────────────────────────────────────────────────────────────


class AddSessionJobsRequest(BaseModel):
    combos: str = Field(..., description="email|password|secret per line")
    reg_mode: str = Field(default="browser", description="'browser' (default) or 'pure_request'")


class SetSessionConfigRequest(BaseModel):
    # Bỏ le=10 — handler clamp về 10 trước khi apply (xem set_session_config).
    max_concurrent: int | None = Field(default=None, ge=1)
    job_timeout: float | None = Field(default=None, ge=30, le=600)


@app.get("/api/session/jobs")
async def list_session_jobs() -> JSONResponse:
    sm = get_session_manager()
    return JSONResponse({
        "max_concurrent": sm.max_concurrent,
        "job_timeout": sm.job_timeout,
        "jobs": sm.list_jobs(),
    })


@app.get("/api/session/jobs/secrets")
async def get_session_jobs_secrets() -> JSONResponse:
    """Trả map ``job_id → {email, password, secret}`` cho mọi Session job.

    Frontend render 2 Output panes (Free/Plus) ở format
    ``email|password|secret`` — secret KHÔNG nằm trong ``job.to_dict()`` /
    SSE broadcast (tránh leak qua snapshot). Pattern y hệt
    ``/api/upi/jobs/secrets``.

    Phải đăng ký TRƯỚC route ``{job_id}`` để FastAPI không match
    ``secrets`` thành path param.
    """
    sm = get_session_manager()
    return JSONResponse({"secrets": sm.get_secrets_map()})


@app.get("/api/session/jobs/{job_id}")
async def get_session_job(job_id: str) -> JSONResponse:
    sm = get_session_manager()
    data = sm.get_job(job_id)
    if data is None:
        raise HTTPException(404, "job not found")
    return JSONResponse(data)


@app.get("/api/session/jobs/{job_id}/log")
async def get_session_job_log(job_id: str) -> JSONResponse:
    sm = get_session_manager()
    if job_id not in sm.jobs:
        raise HTTPException(404, "job not found")
    return JSONResponse({"job_id": job_id, "log": sm.get_log(job_id)})


@app.post("/api/session/jobs")
async def add_session_jobs(payload: AddSessionJobsRequest) -> JSONResponse:
    combos = payload.combos.splitlines()
    sm = get_session_manager()
    jobs = sm.add_jobs(combos, reg_mode=payload.reg_mode)
    return JSONResponse({"added": len(jobs), "jobs": [j.to_dict() for j in jobs]})


@app.post("/api/session/jobs/{job_id}/retry")
async def retry_session_job(job_id: str) -> JSONResponse:
    sm = get_session_manager()
    if job_id not in sm.jobs:
        raise HTTPException(404, "job not found")
    ok = await sm.retry_job(job_id)
    if not ok:
        raise HTTPException(503, "storage temporarily unavailable")
    return JSONResponse({"ok": True})


@app.delete("/api/session/jobs/{job_id}")
async def delete_session_job(job_id: str) -> JSONResponse:
    sm = get_session_manager()
    if job_id not in sm.jobs:
        raise HTTPException(404, "job not found")
    ok = sm.remove_job(job_id)
    if not ok:
        raise HTTPException(500, "failed to delete job from storage")
    return JSONResponse({"ok": True})


@app.post("/api/session/jobs/stop-all")
async def stop_all_session_jobs() -> JSONResponse:
    sm = get_session_manager()
    stopped = await sm.stop_all()
    return JSONResponse({"stopped": stopped})


@app.post("/api/session/jobs/clear-finished")
async def clear_finished_session_jobs() -> JSONResponse:
    sm = get_session_manager()
    removed = sm.clear_finished()
    if removed < 0:
        raise HTTPException(503, "storage temporarily unavailable")
    return JSONResponse({"removed": removed})


@app.get("/api/session/config")
async def get_session_config() -> JSONResponse:
    sm = get_session_manager()
    return JSONResponse({
        "max_concurrent": sm.max_concurrent,
        "job_timeout": sm.job_timeout,
    })


@app.post("/api/session/config")
async def set_session_config(payload: SetSessionConfigRequest) -> JSONResponse:
    sm = get_session_manager()
    if payload.max_concurrent is not None:
        try:
            # Silent clamp về [1, 30] (Session max).
            clamped = max(1, min(payload.max_concurrent, 30))
            sm.set_max_concurrent(clamped)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    if payload.job_timeout is not None:
        try:
            sm.set_job_timeout(payload.job_timeout)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    return JSONResponse({
        "max_concurrent": sm.max_concurrent,
        "job_timeout": sm.job_timeout,
    })





# ─────────────────────────────────────────────────────────────────────
# Link API (Get Payment Link feature)
# ─────────────────────────────────────────────────────────────────────


class AddLinkJobsRequest(BaseModel):
    combos: str = Field(..., description="Input text — format depends on mode")
    mode: str = Field(default="combo", description="combo | session_json | access_token")
    region: str = Field(default="VN", description="Region: VN | ID | IN | US")
    reg_mode: str = Field(default="browser", description="'browser' (default) or 'pure_request'")


class SetLinkConfigRequest(BaseModel):
    # Bỏ le=10 — handler clamp về 10 trước khi apply (xem set_link_config).
    max_concurrent: int | None = Field(default=None, ge=1)
    job_timeout: float | None = Field(default=None, ge=30, le=600)
    region: str | None = Field(
        default=None,
        description="Region: VN | ID | IN | US",
    )


@app.post("/api/link/jobs")
async def add_link_jobs(payload: AddLinkJobsRequest) -> JSONResponse:
    mode = payload.mode
    if mode not in ("combo", "session_json", "access_token"):
        raise HTTPException(400, f"invalid mode: {mode}")
    region = payload.region.upper()
    if region not in REGION_BILLING:
        raise HTTPException(400, f"invalid region: {payload.region}. Must be one of: {list(REGION_BILLING.keys())}")
    lines = payload.combos.splitlines()
    lm = get_link_manager()
    jobs = lm.add_jobs(lines, mode=mode, region=region, reg_mode=payload.reg_mode)  # type: ignore[arg-type]
    return JSONResponse({"added": len(jobs), "jobs": [j.to_dict() for j in jobs]})


@app.get("/api/link/jobs")
async def list_link_jobs() -> JSONResponse:
    lm = get_link_manager()
    return JSONResponse({
        "max_concurrent": lm.max_concurrent,
        "job_timeout": lm.job_timeout,
        "region": lm.region,
        "jobs": lm.list_jobs(),
    })


@app.get("/api/link/config")
async def get_link_config() -> JSONResponse:
    lm = get_link_manager()
    return JSONResponse({
        "max_concurrent": lm.max_concurrent,
        "job_timeout": lm.job_timeout,
        "region": lm.region,
    })


@app.post("/api/link/config")
async def set_link_config(payload: SetLinkConfigRequest) -> JSONResponse:
    lm = get_link_manager()
    if payload.max_concurrent is not None:
        try:
            # Silent clamp về [1, 10] (Link max).
            clamped = max(1, min(payload.max_concurrent, 10))
            lm.set_max_concurrent(clamped)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    if payload.job_timeout is not None:
        try:
            lm.set_job_timeout(payload.job_timeout)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    if payload.region is not None:
        try:
            lm.set_region(payload.region.upper())
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    return JSONResponse({
        "max_concurrent": lm.max_concurrent,
        "job_timeout": lm.job_timeout,
        "region": lm.region,
    })


@app.post("/api/link/jobs/stop-all")
async def stop_all_link_jobs() -> JSONResponse:
    lm = get_link_manager()
    cancelled = await lm.stop_all()
    return JSONResponse({"cancelled": cancelled})


@app.post("/api/link/jobs/clear-finished")
async def clear_finished_link_jobs() -> JSONResponse:
    lm = get_link_manager()
    removed = lm.clear_finished()
    if removed < 0:
        raise HTTPException(503, "storage temporarily unavailable")
    return JSONResponse({"removed": removed})


@app.get("/api/link/jobs/{job_id}")
async def get_link_job(job_id: str) -> JSONResponse:
    lm = get_link_manager()
    data = lm.get_job(job_id)
    if data is None:
        raise HTTPException(404, "job not found")
    return JSONResponse(data)


class RetryLinkRequest(BaseModel):
    region: str | None = Field(
        default=None,
        description="Region override (VN | ID | IN | US). Nếu omit → giữ region gốc của job.",
    )


@app.post("/api/link/jobs/{job_id}/retry")
async def retry_link_job(job_id: str, payload: RetryLinkRequest | None = None) -> JSONResponse:
    lm = get_link_manager()
    job = lm.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if job.status == "running":
        raise HTTPException(409, "job đang chạy, không thể retry")
    region = payload.region if payload else None
    try:
        ok = await lm.retry_job(job_id, region=region)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not ok:
        raise HTTPException(503, "storage temporarily unavailable")
    return JSONResponse({"ok": True})


@app.delete("/api/link/jobs/{job_id}")
async def delete_link_job(job_id: str) -> JSONResponse:
    lm = get_link_manager()
    if job_id not in lm.jobs:
        raise HTTPException(404, "job not found")
    ok = lm.remove_job(job_id)
    if not ok:
        raise HTTPException(500, "failed to delete job from storage")
    return JSONResponse({"ok": True})


@app.post("/api/link/jobs/{job_id}/gopay-link")
async def get_gopay_link(job_id: str) -> JSONResponse:
    """Lấy trial checkout link cho job ID.

    Với job có access token, trả payment_link trial IDR 0 và gopay_link=None.
    Với job cũ không còn token, thử dùng payment_link đã lưu để lấy Midtrans legacy.
    """
    from payment_link import (
        get_gopay_midtrans_url,
        get_gopay_url_from_access_token,
        GopayLinkError,
        PaymentLinkError,
    )

    lm = get_link_manager()
    job = lm.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if job.region != "ID":
        raise HTTPException(400, "gopay-link chỉ hỗ trợ region=ID")
    if not job.payment_link and not job._access_token:
        raise HTTPException(400, "job chưa có payment_link — chạy get link trước")

    from .manager import run_with_proxy_rotation
    try:
        if job._access_token:
            access_token = job._access_token

            async def _run(proxy: str | None) -> tuple[str, str | None]:
                return await get_gopay_url_from_access_token(
                    access_token,
                    proxy=proxy,
                )
        else:
            payment_link = job.payment_link

            async def _run(proxy: str | None) -> tuple[str, str | None]:
                midtrans_url = await get_gopay_midtrans_url(
                    payment_link,
                    proxy=proxy,
                )
                return payment_link, midtrans_url
        payment_url, midtrans_url = await run_with_proxy_rotation(_run)
    except (GopayLinkError, PaymentLinkError) as exc:
        raise HTTPException(502, f"gopay-link failed: {exc}")

    if job._access_token:
        job.payment_link = payment_url
        lm._broadcast_job(job)

    return JSONResponse({
        "payment_link": payment_url,
        "gopay_link": midtrans_url,
    })


@app.post("/api/link/jobs/{job_id}/refresh-gopay-link")
async def refresh_gopay_link(job_id: str) -> JSONResponse:
    """Chạy lại: lấy Stripe trial link mới từ session.

    Dùng khi link cũ expired. Cần job có _access_token (mode session_json/access_token)
    hoặc job đã success có thể retry.
    """
    from payment_link import (
        get_gopay_url_from_access_token,
        GopayLinkError,
        PaymentLinkError,
    )

    lm = get_link_manager()
    job = lm.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if job.region != "ID":
        raise HTTPException(400, "refresh-gopay-link chỉ hỗ trợ region=ID")

    # Cần access_token để lấy link mới
    access_token = job._access_token
    if not access_token:
        raise HTTPException(400, "job không có access_token — chỉ hỗ trợ mode session_json/access_token")

    from .manager import run_with_proxy_rotation
    try:
        async def _run(proxy: str | None) -> tuple[str, str | None]:
            return await get_gopay_url_from_access_token(
                access_token,
                proxy=proxy,
            )

        new_payment_url, midtrans_url = await run_with_proxy_rotation(_run)
    except (GopayLinkError, PaymentLinkError) as exc:
        raise HTTPException(502, f"refresh stripe link failed: {exc}")

    # Cập nhật payment_link của job
    job.payment_link = new_payment_url
    lm._broadcast_job(job)

    return JSONResponse({
        "payment_link": new_payment_url,
        "gopay_link": midtrans_url,
    })





# ─────────────────────────────────────────────────────────────────────
# Settings API (unified-settings-store R5)
# ─────────────────────────────────────────────────────────────────────


# Module-level SettingsRepository — khởi tạo lazy (sau startup)
_settings_repo = None


def _get_settings_repo():
    """Trả về SettingsRepository instance, lazy-init từ _engine."""
    global _settings_repo  # noqa: PLW0603
    if _settings_repo is None:
        from db import get_settings_repo
        _settings_repo = get_settings_repo(_engine)
    return _settings_repo


class BulkSetRequest(BaseModel):
    items: dict = Field(..., description="Mapping {key: value} to set atomically.")


class SetValueRequest(BaseModel):
    value: Any = Field(..., description="JSON-serializable value to store.")


@app.get("/api/settings")
async def list_settings(request: Request) -> JSONResponse:
    """R5.1: List all settings, optionally filtered by prefix."""
    prefix = request.query_params.get("prefix", None)
    repo = _get_settings_repo()
    settings = repo.list(prefix=prefix or None)
    return JSONResponse({"settings": settings})


@app.get("/api/settings/{key:path}")
async def get_setting(key: str) -> JSONResponse:
    """R5.2: Get single setting by key. 404 if not found."""
    repo = _get_settings_repo()
    value = repo.get(key)
    if value is None:
        # Distinguish between "key exists with null value" vs "key not found"
        conn = repo._engine.raw_connection()
        row = conn.execute(
            "SELECT 1 FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "key not found")
    return JSONResponse({"key": key, "value": value})


@app.put("/api/settings/{key:path}")
async def set_setting(key: str, payload: SetValueRequest) -> JSONResponse:
    """R5.3: Set a single setting. 422 on validation/whitelist error."""
    from db.repositories import RepositoryError
    repo = _get_settings_repo()
    try:
        repo.set(key, payload.value)
    except RepositoryError as exc:
        raise HTTPException(422, str(exc))
    return JSONResponse({"key": key, "value": payload.value})


@app.delete("/api/settings/{key:path}")
async def delete_setting(key: str) -> JSONResponse:
    """R5.4: Delete a single setting. 404 if not found, 422 on whitelist error."""
    from db.repositories import RepositoryError
    repo = _get_settings_repo()
    try:
        deleted = repo.delete(key)
    except RepositoryError as exc:
        raise HTTPException(422, str(exc))
    if not deleted:
        raise HTTPException(404, "key not found")
    return JSONResponse({"deleted": True})


@app.post("/api/settings/bulk")
async def bulk_set_settings(payload: BulkSetRequest) -> JSONResponse:
    """R5.5: Atomic bulk set. 422 on validation/whitelist error."""
    from db.repositories import RepositoryError
    repo = _get_settings_repo()
    try:
        repo.bulk_set(payload.items)
    except RepositoryError as exc:
        raise HTTPException(422, str(exc))
    return JSONResponse({"updated": len(payload.items)})


# ── Import from localStorage (R7) ──────────────────────────────────────


class ImportFromLocalStorageRequest(BaseModel):
    localstorage: dict[str, str] = Field(
        ..., description="Snapshot {original_ls_key: raw_string_value}"
    )


@app.post("/api/settings/import-from-localstorage")
async def import_from_localstorage(payload: ImportFromLocalStorageRequest) -> JSONResponse:
    """R7: One-shot migration từ localStorage + runner_config.json → settings DB.

    - Parse localStorage values theo key mapping (design §7)
    - Đọc runner_config.json server-side nếu tồn tại
    - Chỉ ghi key chưa tồn tại trong DB (skip existing) — R7.4
    - Toàn bộ ghi trong 1 Atomic_Transaction — R7.8, R11.2
    - Rename runner_config.json → .bak sau commit thành công — R7.6
    - Handle corrupt runner_config.json → skip file, thêm runner_config_error — R7.7
    """
    import os as _os
    from db.repositories import RepositoryError
    from .runner_config_store import RunnerConfig, RunnerConfigError

    repo = _get_settings_repo()
    ls = payload.localstorage

    # ── 1. Parse localStorage values → flat dict {db_key: python_value} ──
    parsed: dict[str, Any] = {}
    client_keys_imported: set[str] = set()  # track which LS keys → success

    # gpt_reg.settings → reg.*
    _parse_gpt_reg_settings(ls, parsed, client_keys_imported)
    # gpt_reg.mail_mode → mail_mode.current
    _parse_simple_string(ls, "gpt_reg.mail_mode", "mail_mode.current", parsed, client_keys_imported)
    # gpt_reg.worker_config → mail_mode.worker_config (JSON object)
    _parse_json_object(ls, "gpt_reg.worker_config", "mail_mode.worker_config", parsed, client_keys_imported)
    # gpt_reg.active_tab → ui.active_tab
    _parse_simple_string(ls, "gpt_reg.active_tab", "ui.active_tab", parsed, client_keys_imported)
    # autoreg.config.v1 → autoreg.*
    _parse_autoreg_config(ls, parsed, client_keys_imported)
    # hme.privacy.mask.v1 → hme.privacy_mask (bool from "0"/"1")
    _parse_bool_string(ls, "hme.privacy.mask.v1", "hme.privacy_mask", parsed, client_keys_imported)
    # gpt_reg.link.mode → ui.link_mode
    _parse_simple_string(ls, "gpt_reg.link.mode", "ui.link_mode", parsed, client_keys_imported)

    # ── 2. Read runner_config.json server-side (R7.3) ──
    runner_config_error: str | None = None
    runner_config_path: Path | None = None
    runner_config_bak: str | None = None

    from config import load_settings as _load_app_settings
    try:
        app_settings = _load_app_settings()
        runner_config_path = app_settings.runtime_dir / "icloud" / "runner_config.json"
    except Exception:
        runner_config_path = Path("runtime/icloud/runner_config.json")

    if runner_config_path and runner_config_path.exists():
        try:
            raw_text = runner_config_path.read_text(encoding="utf-8")
            raw_json = json.loads(raw_text)
            rc = RunnerConfig.from_dict(raw_json)
            # Map runner config fields → DB keys
            rc_dict = rc.to_dict()
            if "action" in rc_dict:
                parsed["hme.runner.action"] = rc_dict["action"]
            if "count_per_cycle" in rc_dict:
                parsed["hme.runner.count_per_cycle"] = rc_dict["count_per_cycle"]
            if "retry_interval" in rc_dict:
                parsed["hme.runner.retry_interval"] = rc_dict["retry_interval"]
            if "label" in rc_dict:
                parsed["hme.runner.label"] = rc_dict["label"]
            if "note" in rc_dict:
                parsed["hme.runner.note"] = rc_dict["note"]
        except (json.JSONDecodeError, RunnerConfigError, OSError) as exc:
            runner_config_error = str(exc)
            runner_config_path = None  # don't rename on error

    # ── 3. Atomic write: chỉ ghi key chưa tồn tại (R7.4, R7.8, R11.2) ──
    imported: list[str] = []
    skipped: list[str] = []

    try:
        with repo._engine.get_connection() as conn:
            for db_key, value in parsed.items():
                # Check if key already exists
                row = conn.execute(
                    "SELECT 1 FROM settings WHERE key = ?", (db_key,)
                ).fetchone()
                if row is not None:
                    skipped.append(db_key)
                    continue
                # Encode value
                encoded = json.dumps(
                    value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?)",
                    (db_key, encoded),
                )
                imported.append(db_key)

            # Audit log (R10.4) — 1 entry cho toàn bộ import
            conn.execute(
                """INSERT INTO icloud_audit_log (event_type, payload_json)
                   VALUES ('settings.import', ?)""",
                (json.dumps({"imported": imported, "skipped": skipped}),),
            )
    except Exception as exc:
        _log.error("import-from-localstorage DB error: %s", exc)
        raise HTTPException(500, f"import failed: {exc}")

    # ── 4. Rename runner_config.json → .bak sau commit thành công (R7.6) ──
    if runner_config_path and runner_config_path.exists() and runner_config_error is None:
        bak_path = runner_config_path.with_suffix(".json.bak")
        try:
            _os.replace(str(runner_config_path), str(bak_path))
            runner_config_bak = str(bak_path)
        except OSError as exc:
            _log.warning("Failed to rename runner_config.json → .bak: %s", exc)

    # ── 5. Build response (R7.5) ──
    # client_keys_to_remove = LS keys mà đã import thành công ít nhất 1 DB key
    client_keys_to_remove: list[str] = []
    for ls_key in client_keys_imported:
        if ls_key in ls:
            client_keys_to_remove.append(ls_key)

    response: dict[str, Any] = {
        "imported": imported,
        "skipped": skipped,
        "client_keys_to_remove": client_keys_to_remove,
        "renamed_runner_config_to": runner_config_bak,
    }
    if runner_config_error is not None:
        response["runner_config_error"] = runner_config_error

    return JSONResponse(response)


# ── Helpers cho import parsing ──────────────────────────────────────────


def _parse_gpt_reg_settings(
    ls: dict[str, str],
    out: dict[str, Any],
    client_keys: set[str],
) -> None:
    """Parse `gpt_reg.settings` JSON → reg.* keys."""
    raw = ls.get("gpt_reg.settings")
    if raw is None:
        return
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(obj, dict):
        return

    # Mapping: gpt_reg.settings field → DB key + optional transform
    _FIELD_MAP: dict[str, tuple[str, Any]] = {
        "mode": ("reg.mode", None),
        "headless": ("reg.headless", None),
        "debug": ("reg.debug", None),
        "default_password": ("reg.default_password", None),
        "job_timeout": ("reg.job_timeout", lambda v: int(v) if v is not None else None),
        "post_reg_get_session": ("reg.post_reg_get_session", None),
        "post_reg_get_link": ("reg.post_reg_get_link", None),
        "post_reg_link_region": ("reg.post_reg_link_region", None),
        "auto_retry_max": ("reg.auto_retry_max", lambda v: int(v) if v is not None else None),
    }

    mapped_any = False
    for field_name, (db_key, transform) in _FIELD_MAP.items():
        if field_name in obj:
            value = obj[field_name]
            if transform is not None:
                try:
                    value = transform(value)
                except (ValueError, TypeError):
                    continue
            out[db_key] = value
            mapped_any = True

    if mapped_any:
        client_keys.add("gpt_reg.settings")


def _parse_simple_string(
    ls: dict[str, str],
    ls_key: str,
    db_key: str,
    out: dict[str, Any],
    client_keys: set[str],
) -> None:
    """Parse simple string LS key → DB key."""
    raw = ls.get(ls_key)
    if raw is None:
        return
    # Store as-is (string value)
    out[db_key] = raw
    client_keys.add(ls_key)


def _parse_bool_string(
    ls: dict[str, str],
    ls_key: str,
    db_key: str,
    out: dict[str, Any],
    client_keys: set[str],
) -> None:
    """Parse "0"/"1" string → bool DB key."""
    raw = ls.get(ls_key)
    if raw is None:
        return
    out[db_key] = raw == "1"
    client_keys.add(ls_key)


def _parse_json_object(
    ls: dict[str, str],
    ls_key: str,
    db_key: str,
    out: dict[str, Any],
    client_keys: set[str],
) -> None:
    """Parse JSON object LS key → DB key."""
    raw = ls.get(ls_key)
    if raw is None:
        return
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(obj, dict):
        return
    out[db_key] = obj
    client_keys.add(ls_key)


def _parse_autoreg_config(
    ls: dict[str, str],
    out: dict[str, Any],
    client_keys: set[str],
) -> None:
    """Parse `autoreg.config.v1` JSON → autoreg.* keys."""
    raw = ls.get("autoreg.config.v1")
    if raw is None:
        return
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(obj, dict):
        return

    mapped_any = False
    if "concurrency" in obj:
        try:
            out["autoreg.concurrency"] = int(obj["concurrency"])
            mapped_any = True
        except (ValueError, TypeError):
            pass
    if "poll_interval" in obj:
        try:
            out["autoreg.poll_interval"] = int(obj["poll_interval"])
            mapped_any = True
        except (ValueError, TypeError):
            pass

    if mapped_any:
        client_keys.add("autoreg.config.v1")


# ─────────────────────────────────────────────────────────────────────
# Static UI
# ─────────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html_path = _STATIC_DIR / "index.html"
    # Chỉ embed token khi bind loopback — non-loopback yêu cầu user truyền token
    # qua URL ?token=... hoặc nhập thủ công (tránh leak token cho bất kỳ LAN client nào).
    embedded_token = get_token() if _is_loopback_bind_enabled() else ""
    html = (
        html_path.read_text(encoding="utf-8")
        .replace("__ASSET_VERSION__", _asset_version())
        .replace("__AUTH_TOKEN__", embedded_token)
        .replace("__HIDE_REG__", "1" if _hide_reg_enabled() else "0")
    )
    return HTMLResponse(html)


# ── GoPay Phone Checker: snap token endpoint ────────────────────────────────
# Extension gọi endpoint này với access_token → trả midtrans URL + snap token.

class GopaySnapRequest(BaseModel):
    access_token: str | None = None
    session_json: str | None = None


@app.post("/api/gopay-check/snap-token")
async def gopay_check_snap_token(payload: GopaySnapRequest) -> JSONResponse:
    """Lấy Midtrans snap token từ ChatGPT access_token.

    Flow: access_token → checkout → Stripe → Midtrans URL → extract snap token.
    """
    import re as _re
    from payment_link import (
        get_gopay_url_from_access_token,
        SessionExpiredError,
        CloudflareBlockedError,
        GopayLinkError,
        PaymentLinkError,
    )

    # Extract access_token
    access_token = payload.access_token
    if not access_token and payload.session_json:
        try:
            data = json.loads(payload.session_json)
            access_token = data.get("accessToken") or data.get("access_token")
        except (json.JSONDecodeError, TypeError):
            pass

    if not access_token:
        raise HTTPException(400, "Cần access_token hoặc session_json chứa accessToken")

    # Proxy xoay từ pool + tự loại proxy chết (network error). Cùng 1 proxy cho
    # cả 2 bước (checkout + GoPay) để giữ IP nhất quán. Pool rỗng → direct.
    from .manager import run_with_proxy_rotation

    async def _run(proxy: str | None) -> str:
        _payment_url, midtrans_url = await get_gopay_url_from_access_token(
            access_token,
            proxy=proxy,
        )
        if midtrans_url is None:
            raise GopayLinkError("trial checkout has no Midtrans snap token")
        return midtrans_url

    try:
        midtrans_url = await run_with_proxy_rotation(_run)
    except SessionExpiredError as exc:
        raise HTTPException(401, f"Session expired: {exc}")
    except CloudflareBlockedError as exc:
        raise HTTPException(403, f"Cloudflare blocked: {exc}")
    except GopayLinkError as exc:
        raise HTTPException(502, f"GoPay link failed: {exc}")
    except PaymentLinkError as exc:
        raise HTTPException(502, f"Checkout failed: {exc}")

    # Extract snap token (UUID) from URL
    token_match = _re.search(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        midtrans_url,
        _re.IGNORECASE,
    )
    if token_match is None:
        raise HTTPException(502, "Midtrans URL missing snap token UUID")
    snap_token = token_match.group(1)

    return JSONResponse({
        "success": True,
        "snap_token": snap_token,
        "midtrans_url": midtrans_url,
    })


# ─────────────────────────────────────────────────────────────────────
# UPI API (Get UPI QR feature)
# ─────────────────────────────────────────────────────────────────────


class AddUpiJobsRequest(BaseModel):
    combos: str = Field(..., description="email|password|secret per line")


class SetUpiConfigRequest(BaseModel):
    # Bỏ le=50 — handler clamp về 50 trước khi apply (xem set_upi_config).
    max_concurrent: int | None = Field(default=None, ge=1)
    job_timeout: float | None = Field(default=None, ge=60, le=7200)
    approve_retries: int | None = Field(default=None, ge=1, le=2000)
    approve_retry_delay: float | None = Field(default=None, ge=2, le=60)
    notify_enabled: bool | None = Field(default=None)
    restart_threshold: int | None = Field(default=None, ge=0, le=1000)
    max_restarts: int | None = Field(default=None, ge=0, le=2000)
    proxy_from_step: int | None = Field(default=None, ge=1, le=6)
    max_outer_cycles: int | None = Field(default=None, ge=1, le=5)
    relogin_block_streak: int | None = Field(default=None, ge=0, le=1000)
    # Login proxy URL RIÊNG (Step 1+2). Empty string = clear (luồng cũ).
    # Validate format chi tiết delegate cho UpiJobManager.set_login_proxy_url
    # (raise ValueError → 400) — Pydantic ở đây chỉ check length.
    login_proxy_url: str | None = Field(default=None, max_length=500)


class SetTelegramConfigRequest(BaseModel):
    bot_token: str | None = Field(default=None, max_length=200)
    chat_id: str | None = Field(default=None, max_length=64)


@app.get("/api/upi/jobs")
async def list_upi_jobs() -> JSONResponse:
    um = get_upi_manager()
    return JSONResponse({
        "max_concurrent": um.max_concurrent,
        "job_timeout": um.job_timeout,
        "approve_retries": um.approve_retries,
        "approve_retry_delay": um.approve_retry_delay,
        "restart_threshold": um.restart_threshold,
        "max_restarts": um.max_restarts,
        "jobs": um.list_jobs(),
    })


@app.get("/api/upi/jobs/{job_id}")
async def get_upi_job(job_id: str) -> JSONResponse:
    um = get_upi_manager()
    data = um.get_job(job_id)
    if data is None:
        raise HTTPException(404, "job not found")
    return JSONResponse(data)


@app.get("/api/upi/jobs/{job_id}/log")
async def get_upi_job_log(job_id: str) -> JSONResponse:
    um = get_upi_manager()
    if job_id not in um.jobs:
        raise HTTPException(404, "job not found")
    return JSONResponse({"job_id": job_id, "log": um.get_log(job_id)})


@app.get("/api/upi/jobs/{job_id}/qr")
async def get_upi_job_qr(job_id: str):
    """Trả về QR image (PNG/SVG) cho UI render <img src=...>."""
    from fastapi.responses import FileResponse

    um = get_upi_manager()
    path = um.get_qr_path(job_id)
    if path is None:
        raise HTTPException(404, "QR not available for this job")
    media_type = "image/svg+xml" if path.suffix.lower() == ".svg" else "image/png"
    return FileResponse(path, media_type=media_type)


@app.post("/api/upi/jobs")
async def add_upi_jobs(payload: AddUpiJobsRequest) -> JSONResponse:
    combos = payload.combos.splitlines()
    um = get_upi_manager()
    jobs = um.add_jobs(combos)
    return JSONResponse({"added": len(jobs), "jobs": [j.to_dict() for j in jobs]})


@app.post("/api/upi/jobs/{job_id}/retry")
async def retry_upi_job(job_id: str) -> JSONResponse:
    um = get_upi_manager()
    if job_id not in um.jobs:
        raise HTTPException(404, "job not found")
    ok = await um.retry_job(job_id)
    return JSONResponse({"ok": ok})


@app.get("/api/upi/jobs/secrets")
async def get_upi_jobs_secrets() -> JSONResponse:
    """Trả map job_id → {email, password, secret} cho mọi UPI job hiện hành.

    Frontend dùng để render Output pane (`email|password|secret`) cho job đã
    verify Plus — secret KHÔNG nằm trong job.to_dict() / SSE broadcast (cố ý
    để tránh leak qua snapshot). Auth đã cover bởi middleware.
    """
    um = get_upi_manager()
    return JSONResponse({"secrets": um.get_secrets_map()})


@app.delete("/api/upi/plus/{email:path}")
async def delete_upi_plus_cache(email: str) -> JSONResponse:
    """Xoá entry plus cache cho 1 email (case-insensitive).

    Dùng khi user xác nhận force-retry một acc đã verify Plus
    (Q-A flow: Dialog.confirm trên frontend → DELETE cache → POST retry).
    Path param ``{email:path}`` cho phép `@` và `.` không cần URL-encode.
    """
    um = get_upi_manager()
    removed = um.clear_plus_cache(email)
    return JSONResponse({"removed": removed, "email": email.lower()})


@app.post("/api/upi/jobs/{job_id}/check-session")
async def check_upi_job_session(job_id: str) -> JSONResponse:
    """Gọi /api/auth/session bằng cookies đã lưu để biết account còn Plus.

    Frontend gọi khi badge "HẾT HẠN" xuất hiện (QR expired) — kiểm tra giao
    dịch UPI có pump account lên Plus chưa. Trả luôn `plan_check` dict (không
    raise) để UI render badge PLUS/FREE bên cạnh.
    """
    um = get_upi_manager()
    if job_id not in um.jobs:
        raise HTTPException(404, "job not found")
    plan_check = await um.check_plan(job_id)
    return JSONResponse(plan_check)


@app.delete("/api/upi/jobs/{job_id}")
async def delete_upi_job(job_id: str) -> JSONResponse:
    um = get_upi_manager()
    if job_id not in um.jobs:
        raise HTTPException(404, "job not found")
    ok = um.remove_job(job_id)
    if not ok:
        raise HTTPException(500, "failed to delete job")
    return JSONResponse({"ok": True})


@app.post("/api/upi/jobs/stop-all")
async def stop_all_upi_jobs() -> JSONResponse:
    um = get_upi_manager()
    stopped = await um.stop_all()
    return JSONResponse({"stopped": stopped})


@app.post("/api/upi/jobs/clear-finished")
async def clear_finished_upi_jobs() -> JSONResponse:
    um = get_upi_manager()
    removed = um.clear_finished()
    return JSONResponse({"removed": removed})


@app.post("/api/upi/jobs/clear-all")
async def clear_all_upi_jobs() -> JSONResponse:
    """Xoá TẤT CẢ UPI jobs (mọi trạng thái). Cancel running, cleanup QR files."""
    um = get_upi_manager()
    removed = await um.clear_all()
    return JSONResponse({"removed": removed})


@app.delete("/api/upi/cookies")
async def clear_all_upi_cookies() -> JSONResponse:
    """Xoá toàn bộ cache cookie+token UPI của instance hiện tại.

    Lần chạy job UPI tiếp theo sẽ login lại từ đầu (không reuse). KHÔNG
    ảnh hưởng cookie/session in-memory của job đang chạy — chỉ xoá file cache.
    """
    from .upi_session_cache import UpiSessionCache
    n = UpiSessionCache.singleton().clear_all()
    return JSONResponse({"cleared": n})


@app.post("/api/upi/jobs/retry-failed")
async def retry_failed_upi_jobs() -> JSONResponse:
    """Retry tất cả UPI jobs có status error hoặc cancelled."""
    um = get_upi_manager()
    retried = await um.retry_failed()
    return JSONResponse({"retried": retried})


@app.post("/api/upi/jobs/retry-expired-free")
async def retry_expired_free_upi_jobs() -> JSONResponse:
    """Retry tất cả UPI jobs có QR hết hạn nhưng vẫn Free (chưa lên Plus).

    Điều kiện cụ thể: xem ``UpiJobManager.retry_expired_free`` docstring.
    Frontend gọi qua nút "Retry Expired+Free" ở header card-jobs (tab UPI).
    """
    um = get_upi_manager()
    retried = await um.retry_expired_free()
    return JSONResponse({"retried": retried})


@app.get("/api/upi/config")
async def get_upi_config() -> JSONResponse:
    um = get_upi_manager()
    from .telegram_notifier import get_telegram_notifier
    return JSONResponse({
        "max_concurrent": um.max_concurrent,
        "job_timeout": um.job_timeout,
        "approve_retries": um.approve_retries,
        "approve_retry_delay": um.approve_retry_delay,
        "restart_threshold": um.restart_threshold,
        "max_restarts": um.max_restarts,
        "proxy_from_step": um.proxy_from_step,
        "max_outer_cycles": um.max_outer_cycles,
        "relogin_block_streak": um.relogin_block_streak,
        "login_proxy_url": um.login_proxy_url,
        "notify_enabled": get_telegram_notifier().enabled,
    })


@app.post("/api/upi/config")
async def set_upi_config(payload: SetUpiConfigRequest) -> JSONResponse:
    um = get_upi_manager()
    from .telegram_notifier import get_telegram_notifier
    settings_writes: dict[str, Any] = {}
    if payload.max_concurrent is not None:
        try:
            # Silent clamp về [1, 200] (UPI max). Frontend mode dropdown share
            # giữa các tab; UPI tự cap nếu user truyền giá trị lớn hơn.
            clamped = max(1, min(payload.max_concurrent, 200))
            um.set_max_concurrent(clamped)
            settings_writes["upi.max_concurrent"] = clamped
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    if payload.job_timeout is not None:
        try:
            um.set_job_timeout(payload.job_timeout)
            settings_writes["upi.job_timeout"] = int(payload.job_timeout)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    if payload.approve_retries is not None:
        try:
            um.set_approve_retries(payload.approve_retries)
            settings_writes["upi.approve_retries"] = payload.approve_retries
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    if payload.approve_retry_delay is not None:
        try:
            um.set_approve_retry_delay(payload.approve_retry_delay)
            settings_writes["upi.approve_retry_delay"] = float(payload.approve_retry_delay)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    if payload.restart_threshold is not None:
        try:
            um.set_restart_threshold(payload.restart_threshold)
            settings_writes["upi.approve.restart_threshold"] = payload.restart_threshold
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    if payload.max_restarts is not None:
        try:
            um.set_max_restarts(payload.max_restarts)
            settings_writes["upi.approve.max_restarts"] = payload.max_restarts
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    if payload.proxy_from_step is not None:
        try:
            um.set_proxy_from_step(payload.proxy_from_step)
            settings_writes["upi.proxy_from_step"] = payload.proxy_from_step
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    if payload.max_outer_cycles is not None:
        try:
            um.set_max_outer_cycles(payload.max_outer_cycles)
            settings_writes["upi.max_outer_cycles"] = payload.max_outer_cycles
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    if payload.relogin_block_streak is not None:
        try:
            um.set_relogin_block_streak(payload.relogin_block_streak)
            settings_writes["upi.relogin_block_streak"] = payload.relogin_block_streak
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    if payload.login_proxy_url is not None:
        # ``None`` = field không gửi (Pydantic default), không đụng setting.
        # Empty string = user clear → set_login_proxy_url normalize về "".
        try:
            um.set_login_proxy_url(payload.login_proxy_url)
            settings_writes["upi.login_proxy_url"] = um.login_proxy_url
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    if payload.notify_enabled is not None:
        get_telegram_notifier().set_enabled(payload.notify_enabled)
        settings_writes["upi.notify_enabled"] = payload.notify_enabled
    # Write-through SQLite (best-effort — không break endpoint nếu DB fail).
    if settings_writes:
        try:
            settings_repo = _get_settings_repo()
            settings_repo.bulk_set(settings_writes)
        except Exception as exc:  # noqa: BLE001
            _log.warning("UPI config write-through failed: %s", exc)
    return JSONResponse({
        "max_concurrent": um.max_concurrent,
        "job_timeout": um.job_timeout,
        "approve_retries": um.approve_retries,
        "approve_retry_delay": um.approve_retry_delay,
        "restart_threshold": um.restart_threshold,
        "max_restarts": um.max_restarts,
        "proxy_from_step": um.proxy_from_step,
        "max_outer_cycles": um.max_outer_cycles,
        "relogin_block_streak": um.relogin_block_streak,
        "login_proxy_url": um.login_proxy_url,
        "notify_enabled": get_telegram_notifier().enabled,
    })


@app.get("/api/telegram/config")
async def get_telegram_config() -> JSONResponse:
    """Trả config Telegram hiện tại (để Settings tab hiển thị/sửa)."""
    from .telegram_notifier import get_telegram_notifier
    n = get_telegram_notifier()
    return JSONResponse({
        "bot_token": n.bot_token or "",
        "chat_id": n.chat_id or "",
        "configured": n.configured,
        "notify_enabled": n.enabled,
    })


@app.post("/api/telegram/config")
async def set_telegram_config(payload: SetTelegramConfigRequest) -> JSONResponse:
    """Lưu bot_token + chat_id → update notifier live + write-through DB."""
    from .telegram_notifier import get_telegram_notifier
    n = get_telegram_notifier()
    bot_token = (payload.bot_token or "").strip() or None
    chat_id = (payload.chat_id or "").strip() or None
    n.set_credentials(bot_token, chat_id)
    try:
        _get_settings_repo().bulk_set({
            "telegram.bot_token": bot_token,
            "telegram.chat_id": chat_id,
        })
    except Exception as exc:  # noqa: BLE001
        _log.warning("Telegram config write-through failed: %s", exc)
        return JSONResponse({"configured": n.configured, "persist_error": str(exc)})
    return JSONResponse({"configured": n.configured})


@app.post("/api/telegram/test")
async def test_telegram() -> JSONResponse:
    """Gửi 1 tin test để verify bot_token + chat_id."""
    from .telegram_notifier import TelegramNotifyError, get_telegram_notifier
    n = get_telegram_notifier()
    if not n.configured:
        raise HTTPException(400, "bot_token/chat_id chưa cấu hình")
    try:
        await n.send_test()
    except TelegramNotifyError as exc:
        raise HTTPException(400, str(exc))
    return JSONResponse({"ok": True})


@app.get("/api/telegram/debug")
async def debug_telegram() -> JSONResponse:
    """Trả về state hiện tại của notifier để chẩn đoán khi không gửi được tin."""
    from .telegram_notifier import get_telegram_notifier
    n = get_telegram_notifier()
    token = n.bot_token or ""
    return JSONResponse({
        "enabled": n.enabled,
        "configured": n.configured,
        "bot_token_present": bool(token),
        "bot_token_preview": (token[:8] + "..." + token[-4:]) if len(token) >= 16 else (token[:4] + "..." if token else ""),
        "chat_id": n.chat_id or "",
    })


# ─────────────────────────────────────────────────────────────────────
# Cloudflare Quick Tunnel
# ─────────────────────────────────────────────────────────────────────


class SetTunnelConfigRequest(BaseModel):
    enabled: bool = Field(..., description="Bật/tắt Cloudflare Quick Tunnel.")


@app.get("/api/tunnel/status")
async def get_tunnel_status() -> JSONResponse:
    """Trả status tunnel: enabled, status, url, error, log_tail."""
    from .cloudflare_tunnel import get_cloudflare_tunnel
    return JSONResponse(get_cloudflare_tunnel().to_status_dict())


@app.post("/api/tunnel/config")
async def set_tunnel_config(payload: SetTunnelConfigRequest) -> JSONResponse:
    """Bật/tắt tunnel + write-through DB.

    Khi enabled=True: spawn cloudflared, đợi URL (timeout 30s).
    Khi enabled=False: stop subprocess.
    Trả về status snapshot sau khi áp dụng.
    """
    from .cloudflare_tunnel import get_cloudflare_tunnel

    tunnel = get_cloudflare_tunnel()
    enabled = bool(payload.enabled)

    persist_error: str | None = None
    try:
        _get_settings_repo().bulk_set({"tunnel.cloudflare.enabled": enabled})
    except Exception as exc:  # noqa: BLE001
        _log.warning("tunnel config write-through failed: %s", exc)
        persist_error = str(exc)

    # Áp dụng state vào manager (giữ in-memory đồng bộ với DB).
    tunnel.apply_settings({"tunnel.cloudflare.enabled": enabled})

    if enabled:
        await tunnel.start_async()
    else:
        await tunnel.stop_async()

    body = tunnel.to_status_dict()
    if persist_error:
        body["settings_persist_error"] = persist_error
    return JSONResponse(body)


@app.post("/api/tunnel/restart")
async def restart_tunnel() -> JSONResponse:
    """Restart tunnel (URL mới được cấp). Chỉ có nghĩa khi enabled=True."""
    from .cloudflare_tunnel import get_cloudflare_tunnel
    tunnel = get_cloudflare_tunnel()
    if not tunnel.enabled:
        raise HTTPException(400, "tunnel chưa bật — bật ở /api/tunnel/config trước")
    await tunnel.restart_async()
    return JSONResponse(tunnel.to_status_dict())


@app.post("/api/upi/jobs/{job_id}/notify")
async def notify_upi_job(job_id: str) -> JSONResponse:
    """Trigger gửi Telegram cho 1 job đã success — bỏ qua check ``enabled``.

    Dùng để test/khắc phục khi user thấy job success nhưng tin Telegram không
    về: bypass mọi nhánh skip, gọi thẳng notify_upi_qr → trả lỗi cụ thể
    nếu fail (HTTP status, body của Telegram API).
    """
    from .telegram_notifier import TelegramNotifyError, get_telegram_notifier

    um = get_upi_manager()
    job = um.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if job.status != "success" or not job.qr_path:
        raise HTTPException(400, f"job chưa success hoặc không có QR (status={job.status}, qr={bool(job.qr_path)})")
    n = get_telegram_notifier()
    if not n.configured:
        raise HTTPException(400, "telegram chưa cấu hình bot_token/chat_id")
    # Bỏ qua flag enabled — đây là endpoint manual.
    n.set_enabled(True)
    try:
        await n.notify_upi_qr(
            email=job.email,
            password=job.password,
            secret=job.secret,
            amount=job.amount,
            qr_path=job.qr_path,
            qr_expires_at=job.qr_expires_at,
            checkout_session=job.checkout_session,
            return_url=job.return_url,
        )
    except TelegramNotifyError as exc:
        raise HTTPException(400, f"telegram fail: {exc}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"{type(exc).__name__}: {exc}")
    return JSONResponse({"ok": True})


# Mount static folder cho CSS/JS
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


# ── icloud-hme-pool tab (R10) — task 30/31/32 ──────────────────────────
# Lazy-mount router cho /api/icloud/*. Auth qua middleware hiện có
# (require_token) — same token với /api/jobs/*.
from .icloud_routes import build_icloud_router  # noqa: E402

app.include_router(build_icloud_router())
