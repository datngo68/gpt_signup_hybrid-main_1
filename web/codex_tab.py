"""In-memory Ver số queue; credentials never enter public snapshots."""
from __future__ import annotations

import asyncio
import json
import io
import math
import zipfile
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from .auth import require_token


def validate_concurrency(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= 10:
        raise ValueError("Concurrency phải là số nguyên 1..10")
    return value


def parse_combos(value: Any) -> list[tuple[str, str, str | None]]:
    if not isinstance(value, str) or not value.strip() or len(value) > 200_000:
        raise ValueError("Nhập 1..500 dòng email|password|totp_secret (tùy chọn)")
    rows = [line.strip() for line in value.splitlines() if line.strip()]
    if len(rows) > 500:
        raise ValueError("Tối đa 500 dòng")
    result = []
    for number, row in enumerate(rows, 1):
        fields = row.split("|")
        if (len(fields) not in (2, 3) or not re.fullmatch(r"[^\s@|]+@[^\s@|]+\.[^\s@|]+", fields[0])
                or not fields[1] or len(fields[1]) > 1024
                or (len(fields) == 3 and fields[2] and not re.fullmatch(r"[A-Za-z2-7]{16,128}=*", fields[2]))):
            raise ValueError(f"Dòng {number} không hợp lệ")
        result.append((fields[0], fields[1], fields[2] if len(fields) == 3 and fields[2] else None))
    return result


class CodexTab:
    def __init__(self) -> None:
        self.concurrency = 1
        self.jobs: dict[str, dict] = {}
        self.task: asyncio.Task | None = None
        self.stop_event = threading.Event()

    def apply_settings(self, settings: dict) -> None:
        self.concurrency = validate_concurrency(settings.get("codex.concurrency", 1))

    def snapshot(self) -> dict:
        return {"concurrency": self.concurrency, "running": bool(self.task and not self.task.done()),
                "jobs": [{key: job[key] for key in ("id", "email", "status", "phone_status", "logs", "download_ready")}
                         for job in self.jobs.values()]}

    def start(self, rows: list, concurrency: int) -> None:
        if self.task and not self.task.done():
            raise HTTPException(409, "Batch đang chạy")
        self.concurrency = validate_concurrency(concurrency)
        self.stop_event.clear()
        self.jobs = {}
        for email, password, secret in rows:
            ident = uuid.uuid4().hex
            self.jobs[ident] = dict(id=ident, email=email, credentials=(email, password, secret),
                                   status="queued", phone_status="unknown", logs=[], download_ready=False)
        self.task = asyncio.create_task(self._run())

    def stop(self) -> None:
        self.stop_event.set()
        for job in self.jobs.values():
            if job["status"] == "queued":
                job["status"] = "stopped"
                job.pop("credentials", None)
            elif job["status"] == "running":
                job["status"] = "stopping"

    async def shutdown(self) -> None:
        self.stop()
        if self.task:
            await asyncio.shield(self.task)

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue()
        for job in self.jobs.values():
            queue.put_nowait(job)
        # Dedicated pool keeps synchronous browser/SMS/HTTP work off the server loop.
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            async def worker() -> None:
                while not queue.empty():
                    job = queue.get_nowait()
                    if self.stop_event.is_set():
                        job["status"] = "stopped"
                        job.pop("credentials", None)
                        continue
                    job["status"] = "running"
                    credentials = job.pop("credentials")
                    try:
                        result = await loop.run_in_executor(pool, self._execute, credentials, job["id"], loop)
                        if self.stop_event.is_set():
                            job["status"] = "stopped"
                        else:
                            job.update(status="success", result=result, download_ready=True)
                    except Exception:
                        job["status"] = "stopped" if self.stop_event.is_set() else "error"
                        job["logs"].append("Đã dừng" if self.stop_event.is_set() else "Runner thất bại; thông tin nhạy cảm đã ẩn")
            await asyncio.gather(*(worker() for _ in range(self.concurrency)))

    def _event(self, ident: str, event: str) -> None:
        job = self.jobs[ident]
        if event in ("verified", "not_required"):
            job["phone_status"] = event
        elif event == "required" and job["phone_status"] != "verified":
            job["phone_status"] = "required"
        if len(job["logs"]) < 100:
            job["logs"].append({"verified": "Đã xác minh số điện thoại", "required": "Gặp yêu cầu thêm số điện thoại"}.get(event, "Đang xác thực OAuth"))

    def _execute(self, credentials: tuple, ident: str, loop: asyncio.AbstractEventLoop) -> dict:
        from codex_auth.runner import get_codex_auth
        from codex_auth.sms import create_sms_provider
        from db import get_engine
        from db.repositories import SettingsRepository

        owner = self
        def check_stop() -> None:
            if owner.stop_event.is_set():
                raise RuntimeError("Stopped")

        class LazySms:
            provider = None
            def request_phone(self):
                check_stop()
                loop.call_soon_threadsafe(owner._event, ident, "required")
                if self.provider is None:
                    repo = SettingsRepository(get_engine())
                    name, key = repo.get("codex.sms_provider"), repo.get("codex.sms_api_key")
                    if not name or not key:
                        raise RuntimeError("Chưa cấu hình SMS")
                    self.provider = create_sms_provider(name, key, repo.get("codex.sms_service_id"))
                return self.provider.request_phone()
            def __getattr__(self, name):
                # Cancellation/cleanup remains available after stop.
                if name not in ("cancel",):
                    check_stop()
                return getattr(self.provider, name)

        def log(message: str) -> None:
            check_stop()
            # Allowlist only semantic events; never forward runner text/URLs/OTP.
            # Never forward runner text, even when it contains secrets.
        email, password, secret = credentials
        check_stop()
        loop.call_soon_threadsafe(owner._event, ident, "oauth")
        return asyncio.run(get_codex_auth(email=email, password=password, secret=secret,
                                         sms_provider=LazySms(), fetch_api_key=False,
                                         phone_state=lambda state: loop.call_soon_threadsafe(owner._event, ident, state), log=log))


manager = CodexTab()
router = APIRouter(prefix="/api/codex/tab", dependencies=[Depends(require_token)])


@router.get("/jobs")
async def jobs():
    return manager.snapshot()


@router.post("/start")
async def start(request: Request):
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("Body phải là object")
        rows = parse_combos(payload.get("combos"))
        from db import get_engine
        from db.repositories import SettingsRepository
        repo = SettingsRepository(get_engine())
        concurrency = validate_concurrency(payload.get("concurrency", repo.get("codex.concurrency") or 1))
        if manager.task and not manager.task.done():
            raise HTTPException(409, "Batch đang chạy")
        repo.set("codex.concurrency", concurrency)
    except (ValueError, TypeError):
        raise HTTPException(400, "Input hoặc concurrency không hợp lệ") from None
    manager.start(rows, concurrency)
    return manager.snapshot()


@router.post("/stop")
async def stop():
    manager.stop()
    return manager.snapshot()


def _build_9router_export() -> dict[str, list[dict[str, Any]]]:
    accounts: list[dict[str, Any]] = []
    for job in manager.jobs.values():
        if job.get("status") != "success" or not job.get("download_ready"):
            continue
        result = job.get("result")
        tokens = result.get("tokens") if isinstance(result, dict) else None
        access_token = tokens.get("access_token") if isinstance(tokens, dict) else None
        if not isinstance(access_token, str) or not access_token:
            continue
        account: dict[str, Any] = {"accessToken": access_token}
        for source, target in (("refresh_token", "refreshToken"), ("id_token", "idToken")):
            value = tokens.get(source)
            if isinstance(value, str) and value:
                account[target] = value
        expires_in = tokens.get("expires_in")
        if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool) and math.isfinite(expires_in):
            account["expiresIn"] = expires_in
        email = result.get("email")
        if not isinstance(email, str) or not email:
            email = job.get("email")
        if isinstance(email, str) and email:
            account["email"] = email
        accounts.append(account)
    if not accounts:
        raise HTTPException(409, "Chưa có job thành công với access token hợp lệ để xuất 9Router")
    return {"accounts": accounts}


@router.get("/9router.json")
async def download_9router_json():
    return Response(json.dumps(_build_9router_export()), media_type="application/json", headers={
        "Content-Disposition": 'attachment; filename="codex-9router.json"', "Cache-Control": "no-store"})


@router.get("/auth.zip")
async def download_zip():
    successful = [job for job in manager.jobs.values() if job["status"] == "success" and job["download_ready"]]
    if not successful:
        raise HTTPException(404, "Chưa có kết quả thành công")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for job in successful:
            ident = job["id"]
            if not re.fullmatch(r"[a-f0-9]{32}", ident):
                raise HTTPException(500, "Job ID không hợp lệ")
            archive.writestr(f"{ident}/auth.json", json.dumps(job["result"]))
    return Response(buffer.getvalue(), media_type="application/zip", headers={
        "Content-Disposition": 'attachment; filename="codex-auth.zip"', "Cache-Control": "no-store"})


@router.post("/clear")
async def clear_completed():
    if (manager.task and not manager.task.done()) or any(job["status"] in ("queued", "running", "stopping") for job in manager.jobs.values()):
        raise HTTPException(409, "Chờ batch dừng hoàn toàn trước khi xóa")
    terminal = {ident for ident, job in manager.jobs.items() if job["status"] in ("success", "error", "stopped")}
    for ident in terminal:
        del manager.jobs[ident]
    return manager.snapshot()


@router.get("/jobs/{ident}/auth.json")
async def download(ident: str):
    job = manager.jobs.get(ident)
    if not job or not job["download_ready"]:
        raise HTTPException(404, "auth.json chưa sẵn sàng")
    return Response(json.dumps(job["result"]), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="auth.json"', "Cache-Control": "no-store"})
