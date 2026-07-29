"""Cloudflare Quick Tunnel manager.

Quản lý lifecycle của ``cloudflared tunnel --url http://<host>:<port>``
subprocess để expose UI ra Internet qua URL ``*.trycloudflare.com``.

Single source of truth cho config là Settings Store (`tunnel.cloudflare.enabled`).
Manager hydrate qua ``apply_settings()`` lúc startup; ``set_local_endpoint()``
được gọi từ CLI trước khi uvicorn start để biết bind đâu.

Cross-platform binary handling:
    - Tự động phát hiện OS/arch và tải đúng asset từ GitHub releases vào
      ``runtime/bin/cloudflared(.exe)`` nếu chưa có.
    - macOS releases là ``.tgz`` cần extract; Windows là ``.exe`` trực tiếp;
      Linux là binary trần.

URL parsing: regex match ``https://<sub>.trycloudflare.com`` từ stderr (đó là
nơi cloudflared in URL khi flag ``--url`` được dùng).
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import shutil
import stat
import sys
import tarfile
import time
from pathlib import Path
from typing import Any, Literal

_log = logging.getLogger(__name__)

# Regex bắt URL quick tunnel — cloudflared luôn dùng pattern này.
_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

# Buffer kích thước log nhỏ để debug khi fail; tránh leak RAM nếu cloudflared in spam.
_LOG_BUFFER_LINES = 80

# Timeout cho graceful shutdown trước khi SIGKILL (giây).
_SHUTDOWN_GRACE_SEC = 5.0

# Timeout chờ URL xuất hiện sau khi spawn (giây). Quick tunnel thường < 5s.
_URL_DETECT_TIMEOUT = 30.0

# Latest release URL pattern. GitHub redirect /latest/download/<asset>
# về tag mới nhất tự động.
_RELEASE_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download"

TunnelStatus = Literal["stopped", "starting", "running", "failed"]

# Env keys truyền local endpoint từ CLI → app. CLI import module này dưới tên
# `web.cloudflare_tunnel`, còn uvicorn/app dùng `gpt_signup_hybrid.web.cloudflare_tunnel`
# → HAI module + HAI singleton khác nhau. set_local_endpoint() ở phía CLI không
# tới được singleton phía app, nên phải truyền qua os.environ (env kế thừa cả
# subprocess khi --reload). Không có cái này → app giữ default 127.0.0.1:8083 →
# cloudflared forward sai port → 502.
_ENV_TUNNEL_HOST = "GSH_TUNNEL_LOCAL_HOST"
_ENV_TUNNEL_PORT = "GSH_TUNNEL_LOCAL_PORT"


class CloudflareTunnelError(Exception):
    """Tunnel lifecycle / binary download lỗi."""


def _detect_asset() -> tuple[str, bool, str]:
    """Trả (asset_name, is_targz, exe_suffix) cho OS+arch hiện tại.

    Raises ``CloudflareTunnelError`` nếu platform không support.
    """
    sys_name = platform.system().lower()
    machine = platform.machine().lower()

    # Normalize arch
    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    elif machine in ("i386", "i686", "x86"):
        arch = "386"
    elif machine.startswith("armv7") or machine.startswith("armhf"):
        arch = "arm"
    else:
        raise CloudflareTunnelError(f"Kiến trúc không hỗ trợ: {machine!r}")

    if sys_name == "linux":
        return f"cloudflared-linux-{arch}", False, ""
    if sys_name == "darwin":
        # macOS release đóng gói tar.gz chứa binary `cloudflared`.
        if arch not in ("amd64", "arm64"):
            raise CloudflareTunnelError(f"macOS chỉ hỗ trợ amd64/arm64, got {arch}")
        return f"cloudflared-darwin-{arch}.tgz", True, ""
    if sys_name == "windows":
        if arch not in ("amd64", "386"):
            raise CloudflareTunnelError(f"Windows chỉ hỗ trợ amd64/386, got {arch}")
        return f"cloudflared-windows-{arch}.exe", False, ".exe"
    raise CloudflareTunnelError(f"Hệ điều hành không hỗ trợ: {sys_name!r}")


def _runtime_bin_dir() -> Path:
    """Trả thư mục cài binary tunnel: ``<runtime_dir>/bin``."""
    from config import load_settings
    bin_dir = load_settings().runtime_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    return bin_dir


def _resolve_binary() -> Path | None:
    """Tìm binary đã cài trong runtime/bin (không tải mới).

    Trả None nếu chưa có. Caller dùng ``ensure_binary()`` để download.
    """
    suffix = ".exe" if platform.system().lower() == "windows" else ""
    local = _runtime_bin_dir() / f"cloudflared{suffix}"
    if local.exists() and local.is_file():
        return local
    # Fallback: dùng binary trên PATH (user đã cài tay) — vẫn coi là OK.
    on_path = shutil.which("cloudflared")
    if on_path:
        return Path(on_path)
    return None


async def _download_binary(target: Path, log: list[str]) -> None:
    """Download binary cloudflared từ GitHub releases vào ``target``.

    Dùng ``httpx`` async với follow_redirects (GitHub /latest/download → CDN).
    macOS asset là ``.tgz`` → extract ``cloudflared`` ra ``target``.

    Raises ``CloudflareTunnelError`` khi fail.
    """
    asset, is_targz, _ = _detect_asset()
    url = f"{_RELEASE_URL}/{asset}"
    log.append(f"[tunnel] download cloudflared từ {url}")
    _log.info("cloudflared download: %s -> %s", url, target)

    import httpx

    # Tải vào file tạm cùng thư mục (atomic rename ở cuối).
    tmp = target.with_suffix(target.suffix + ".part")
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise CloudflareTunnelError(
                        f"GitHub trả {resp.status_code} khi tải {asset}"
                    )
                with tmp.open("wb") as fp:
                    async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                        fp.write(chunk)
    except httpx.HTTPError as exc:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise CloudflareTunnelError(f"Lỗi mạng khi tải cloudflared: {exc}") from exc

    if is_targz:
        # Extract `cloudflared` từ tarball, ghi vào target.
        try:
            with tarfile.open(tmp, "r:gz") as tar:
                member = None
                for m in tar.getmembers():
                    name = Path(m.name).name
                    if name == "cloudflared" and m.isfile():
                        member = m
                        break
                if member is None:
                    raise CloudflareTunnelError(
                        f"Tarball {asset} không chứa binary 'cloudflared'"
                    )
                src = tar.extractfile(member)
                if src is None:
                    raise CloudflareTunnelError(
                        f"Không đọc được binary từ tarball {asset}"
                    )
                with target.open("wb") as fp:
                    shutil.copyfileobj(src, fp)
        finally:
            tmp.unlink(missing_ok=True)
    else:
        # Move atomic (cùng filesystem).
        tmp.replace(target)

    # Set executable bit trên Unix.
    if platform.system().lower() != "windows":
        st = target.stat()
        target.chmod(st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    log.append(f"[tunnel] đã cài cloudflared → {target}")


async def _ensure_binary(log: list[str]) -> Path:
    """Tìm binary; nếu thiếu thì download. Trả về Path tới binary.

    Idempotent: gọi nhiều lần an toàn. Caller giữ ``log`` để hiển thị progress.
    """
    existing = _resolve_binary()
    if existing is not None:
        return existing
    suffix = ".exe" if platform.system().lower() == "windows" else ""
    target = _runtime_bin_dir() / f"cloudflared{suffix}"
    await _download_binary(target, log)
    return target


class CloudflareTunnelManager:
    """Quản lý 1 quick tunnel duy nhất. Singleton qua ``get_cloudflare_tunnel()``."""

    def __init__(self) -> None:
        self._enabled: bool = False
        self._local_host: str = "127.0.0.1"
        self._local_port: int = 8083
        self._proc: asyncio.subprocess.Process | None = None
        self._tunnel_url: str | None = None
        self._status: TunnelStatus = "stopped"
        self._error: str | None = None
        self._started_at: float | None = None
        self._log_buffer: list[str] = []
        self._reader_task: asyncio.Task[None] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._url_event: asyncio.Event = asyncio.Event()
        self._lock: asyncio.Lock = asyncio.Lock()
        self._stop_requested: bool = False

    # ── Config ──────────────────────────────────────────────────────────
    def apply_settings(self, settings: dict[str, Any]) -> None:
        """Hydrate từ settings dict lúc startup. Chỉ set state, không auto-start.

        Caller (server on_startup) sau khi gọi apply_settings() nếu thấy
        ``enabled=True`` thì schedule task ``start_async()``.
        """
        if "tunnel.cloudflare.enabled" in settings:
            self._enabled = bool(settings["tunnel.cloudflare.enabled"])

    def set_local_endpoint(self, host: str, port: int) -> None:
        """Set host:port mà tunnel sẽ trỏ tới. Gọi từ CLI trước khi uvicorn start.

        Tunnel luôn trỏ tới loopback (127.0.0.1) ngay cả khi uvicorn bind LAN —
        vì cloudflared chạy cùng máy với uvicorn, không cần đi qua LAN.
        """
        if not isinstance(port, int) or not (1 <= port <= 65535):
            raise ValueError(f"port không hợp lệ: {port!r}")
        # Nếu bind là 0.0.0.0 hay LAN IP → vẫn đẩy về loopback để tunnel an toàn.
        loopback = host in {"127.0.0.1", "localhost", "::1"}
        self._local_host = "127.0.0.1" if not loopback else host
        self._local_port = port
        # Truyền qua env để vượt ranh giới module-identity (xem _ENV_TUNNEL_*).
        os.environ[_ENV_TUNNEL_HOST] = self._local_host
        os.environ[_ENV_TUNNEL_PORT] = str(self._local_port)

    def _load_endpoint_from_env(self) -> None:
        """Đồng bộ endpoint từ env (CLI set qua set_local_endpoint trên singleton
        module khác). Gọi trước khi spawn cloudflared để forward đúng port."""
        host = os.environ.get(_ENV_TUNNEL_HOST)
        port = os.environ.get(_ENV_TUNNEL_PORT)
        if host:
            self._local_host = host
        if port:
            try:
                self._local_port = int(port)
            except ValueError:
                _log.warning("tunnel: env %s không hợp lệ: %r", _ENV_TUNNEL_PORT, port)

    # ── Status getters ──────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def status(self) -> TunnelStatus:
        return self._status

    @property
    def url(self) -> str | None:
        return self._tunnel_url

    def to_status_dict(self) -> dict[str, Any]:
        """Snapshot state cho API response."""
        return {
            "enabled": self._enabled,
            "status": self._status,
            "url": self._tunnel_url,
            "error": self._error,
            "local_host": self._local_host,
            "local_port": self._local_port,
            "started_at": self._started_at,
            "uptime_sec": (
                int(time.time() - self._started_at)
                if self._started_at and self._status == "running"
                else None
            ),
            "log_tail": list(self._log_buffer[-20:]),
        }

    # ── Lifecycle ───────────────────────────────────────────────────────
    async def start_async(self) -> None:
        """Spawn cloudflared subprocess và đợi URL.

        Idempotent: nếu đã running → no-op. Nếu đang starting → no-op (lock).
        Set ``_status`` + ``_error`` đúng theo kết quả.
        """
        async with self._lock:
            if self._status == "running":
                return
            if self._proc is not None and self._proc.returncode is None:
                # Đang starting nhưng chưa có URL — để task hiện tại tự xử.
                return

            self._status = "starting"
            self._error = None
            self._tunnel_url = None
            self._url_event.clear()
            self._started_at = None
            self._log_buffer.clear()
            self._stop_requested = False

            # Sync endpoint từ env — CLI set ở singleton module khác (xem _ENV_TUNNEL_*).
            self._load_endpoint_from_env()

            try:
                binary = await _ensure_binary(self._log_buffer)
            except CloudflareTunnelError as exc:
                self._status = "failed"
                self._error = str(exc)
                self._log_buffer.append(f"[tunnel] cài binary lỗi: {exc}")
                _log.warning("cloudflare tunnel: cài binary lỗi: %s", exc)
                return

            # Spawn subprocess.
            target_url = f"http://{self._local_host}:{self._local_port}"
            cmd = [
                str(binary),
                "tunnel",
                "--no-autoupdate",
                "--url",
                target_url,
            ]
            self._log_buffer.append(f"[tunnel] spawn: {' '.join(cmd)}")
            try:
                # Windows: detach từ console group để Ctrl+C không lan vào parent.
                kwargs: dict[str, Any] = {
                    "stdout": asyncio.subprocess.PIPE,
                    "stderr": asyncio.subprocess.PIPE,
                }
                if sys.platform == "win32":
                    # CREATE_NEW_PROCESS_GROUP = 0x00000200
                    kwargs["creationflags"] = 0x00000200
                self._proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)
            except (OSError, FileNotFoundError) as exc:
                self._status = "failed"
                self._error = f"Không spawn được cloudflared: {exc}"
                self._log_buffer.append(f"[tunnel] spawn lỗi: {exc}")
                _log.warning("cloudflare tunnel: spawn lỗi: %s", exc)
                return

            # Schedule reader + monitor tasks.
            self._reader_task = asyncio.create_task(
                self._read_streams(), name="cloudflared-reader"
            )
            self._monitor_task = asyncio.create_task(
                self._monitor_proc(), name="cloudflared-monitor"
            )

        # Đợi URL ngoài lock để không chặn stop_async() đồng thời.
        try:
            await asyncio.wait_for(self._url_event.wait(), timeout=_URL_DETECT_TIMEOUT)
        except asyncio.TimeoutError:
            # Quá thời gian mà chưa có URL → coi như fail, kill proc.
            async with self._lock:
                if self._status != "running":
                    self._status = "failed"
                    self._error = (
                        f"Không nhận được URL từ cloudflared sau "
                        f"{int(_URL_DETECT_TIMEOUT)}s"
                    )
                    self._log_buffer.append(f"[tunnel] {self._error}")
            await self._kill_proc()

    async def stop_async(self) -> None:
        """Stop tunnel: gracefully terminate subprocess + cleanup tasks."""
        async with self._lock:
            self._stop_requested = True
            if self._proc is None or self._proc.returncode is not None:
                self._status = "stopped"
                self._tunnel_url = None
                self._started_at = None
                return
        await self._kill_proc()
        async with self._lock:
            self._status = "stopped"
            self._tunnel_url = None
            self._started_at = None
            self._error = None
            self._log_buffer.append("[tunnel] đã stop")

    async def restart_async(self) -> None:
        """Stop rồi start lại (URL mới được cấp)."""
        await self.stop_async()
        await self.start_async()

    # ── Internal: stream readers + monitor ──────────────────────────────
    async def _read_streams(self) -> None:
        """Đọc stdout + stderr của cloudflared, parse URL, giữ log buffer.

        cloudflared in URL ra **stderr** (logger Zerolog default). Đọc cả hai
        để không bị block khi 1 stream full pipe.
        """
        proc = self._proc
        if proc is None:
            return

        async def pump(stream: asyncio.StreamReader | None, label: str) -> None:
            if stream is None:
                return
            while True:
                try:
                    raw = await stream.readline()
                except (asyncio.CancelledError, ValueError):
                    raise
                except Exception as exc:  # noqa: BLE001
                    self._log_buffer.append(f"[tunnel] read {label} lỗi: {exc}")
                    return
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                # Giữ buffer giới hạn.
                self._log_buffer.append(f"[{label}] {line}")
                if len(self._log_buffer) > _LOG_BUFFER_LINES:
                    del self._log_buffer[: len(self._log_buffer) - _LOG_BUFFER_LINES]

                if self._tunnel_url is None:
                    m = _URL_RE.search(line)
                    if m:
                        self._tunnel_url = m.group(0)
                        self._status = "running"
                        self._started_at = time.time()
                        self._url_event.set()
                        _log.info("cloudflare tunnel ready: %s", self._tunnel_url)

        try:
            await asyncio.gather(
                pump(proc.stdout, "out"),
                pump(proc.stderr, "err"),
            )
        except asyncio.CancelledError:
            pass

    async def _monitor_proc(self) -> None:
        """Đợi subprocess kết thúc → mark failed/stopped phù hợp."""
        proc = self._proc
        if proc is None:
            return
        try:
            rc = await proc.wait()
        except asyncio.CancelledError:
            return
        # User chủ động stop → status đã/sẽ được stop_async() set 'stopped'.
        # Bỏ qua, không log dưới dạng crash.
        if self._stop_requested or self._status == "stopped":
            self._url_event.set()
            return
        # Đến đây nghĩa là cloudflared tự crash hoặc fail từ đầu.
        self._url_event.set()  # un-block start_async() đang đợi URL.
        if self._tunnel_url is None:
            self._status = "failed"
            self._error = (
                f"cloudflared kết thúc với exit code {rc} trước khi cấp URL"
            )
        else:
            self._status = "failed"
            self._error = f"cloudflared crash với exit code {rc}"
        self._tunnel_url = None
        self._log_buffer.append(f"[tunnel] {self._error}")
        _log.warning("cloudflare tunnel: %s", self._error)

    async def _kill_proc(self) -> None:
        """Terminate subprocess + cancel tasks. Idempotent."""
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=_SHUTDOWN_GRACE_SEC)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await proc.wait()
                except Exception:  # noqa: BLE001
                    pass
        for task_attr in ("_reader_task", "_monitor_task"):
            task: asyncio.Task[None] | None = getattr(self, task_attr)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            setattr(self, task_attr, None)
        self._proc = None


# ── Singleton ───────────────────────────────────────────────────────────
_tunnel: CloudflareTunnelManager | None = None


def get_cloudflare_tunnel() -> CloudflareTunnelManager:
    global _tunnel  # noqa: PLW0603
    if _tunnel is None:
        _tunnel = CloudflareTunnelManager()
    return _tunnel
