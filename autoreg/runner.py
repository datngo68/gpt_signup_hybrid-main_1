"""AutoRegRunner — async runner tự động poll icloud_emails + signup ChatGPT.

Pattern mirrors HmeRunner:
- Module-level lazy singleton (khởi tạo trong icloud_routes.py)
- start/stop lifecycle với cancel_event
- LogCallback cho SSE bridging
- Stats tracking (monotonically non-decreasing)
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from db.repositories import ChatGptAccountRepository

_log = logging.getLogger(__name__)

# Async callback: (level, message, payload) -> Awaitable[None]
LogCallback = Callable[[str, str, dict[str, Any]], Awaitable[None]]


# Browser-close patterns thường xảy ra khi browser bị đóng giữa lúc đang
# navigate. Một mình các pattern này KHÔNG đủ để mark proxy dead (false
# positive: reaper kill do timeout, OOM, anti-bot reset…) — gating thêm bằng
# OAI domain trong _is_browser_closed_proxy_error.
_BROWSER_CLOSED_MARKERS: tuple[str, ...] = (
    "Target page, context or browser has been closed",
    "BrowserContext has been closed",
    "Transport closed",
    "Page closed",
)

# Domain cần navigate cho reg flow — nếu browser đóng giữa lúc đang navigate
# đến đây thì proxy CHỊU TRÁCH NHIỆM (CF/sentinel block IP, TLS handshake fail
# qua proxy, hoặc proxy tự reset). Anti-bot detection, resource pressure
# thường không kèm domain trong exception message.
_OAI_DOMAINS: tuple[str, ...] = (
    "openai.com",
    "chatgpt.com",
    "auth.openai.com",
    "sentinel.openai.com",
)

# Launch-timeout patterns — Camoufox launch step (set_device_id) treo > 90s
# THƯỜNG do proxy không kết nối nổi (Firefox cần bootstrap chrome:// qua proxy).
# Gating bằng cặp (kind + op): kind chỉ định loại exception, op khẳng định
# step launch cụ thể (không phải mint_token / export_cookies / mint_so —
# những op chạy SAU khi launch xong).
_LAUNCH_TIMEOUT_KIND_MARKERS: tuple[str, ...] = (
    "HybridBrowserPoolError",
    "camoufox op timeout",
)
_LAUNCH_TIMEOUT_OP_MARKERS: tuple[str, ...] = (
    "set_device_id",
    "/launch",
)


def _is_browser_closed_proxy_error(exc_or_msg) -> bool:
    """Detect proxy fail dấu hiệu 'browser bị đóng lúc navigate domain OAI'.

    Conservative: yêu cầu CẢ HAI điều kiện đồng thời để mark proxy dead —
      1. Exception message khớp pattern browser-closed
         (TargetClosedError, BrowserContext closed, Transport closed…).
      2. Message chứa domain OAI (trace navigate đến endpoint OAI).

    → Loại trừ false positive: browser crash do reaper timeout/OOM/anti-bot
    không kèm domain → không mark; còn navigate tới OAI fail thì proxy có
    rủi ro cao bị block → mark để pick proxy khác cho email kế tiếp.
    """
    if exc_or_msg is None:
        return False
    msg = str(exc_or_msg)
    if not msg:
        return False
    if not any(marker in msg for marker in _BROWSER_CLOSED_MARKERS):
        return False
    return any(domain in msg for domain in _OAI_DOMAINS)


def _is_launch_timeout_proxy_error(exc_or_msg) -> bool:
    """Detect proxy fail dấu hiệu 'Camoufox launch step timeout'.

    Camoufox launch trong reg_hybrid phải bootstrap Firefox + connect qua proxy
    (chrome://, fingerprint feeder). Nếu proxy chậm/treo → launch step
    ``set_device_id/launch`` quá 90s → ``HybridBrowserPoolError: camoufox op
    timeout (set_device_id/launch > 90s)``.

    Gating bằng CẶP (kind + op): yêu cầu **cả hai** substring có mặt để chỉ
    catch step launch:
      - kind: ``HybridBrowserPoolError`` hoặc ``camoufox op timeout``
        (exception loại).
      - op: ``set_device_id`` hoặc ``/launch`` (launch step cụ thể, không phải
        op mint_token / export_cookies chạy SAU khi launch OK).

    Trade-off (chấp nhận): chỉ 1 proxy chết → mark đúng + email kế tiếp đổi
    proxy. Máy yếu khiến tất cả launch chậm → toàn pool bị mark dead → autoreg
    fallback direct → user thấy ngay (vs kẹt 1 proxy bad không phát hiện).
    """
    if exc_or_msg is None:
        return False
    msg = str(exc_or_msg)
    if not msg:
        return False
    if not any(marker in msg for marker in _LAUNCH_TIMEOUT_KIND_MARKERS):
        return False
    return any(marker in msg for marker in _LAUNCH_TIMEOUT_OP_MARKERS)


def mark_proxy_dead_on_error(line: str | None, exc_or_msg, *, log=None) -> bool:
    """Mark proxy dead khi error match pattern proxy network / browser-closed /
    launch-timeout.

    Module-level helper (no ``self``) — dùng cho test/diag scripts mirror autoreg
    behavior (ví dụ ``test/run_hybrid_live_regdata.py`` chạy hybrid live test).
    AutoRegRunner._note_proxy_failure cũng route qua đây để giữ DRY.

    Idempotent + safe: line=None → False; line đã dead → False; pattern không
    match → False. Trả True chỉ khi vừa flip live→dead.

    Args:
        line: raw pool line (mark_dead key).
        exc_or_msg: Exception object hoặc error string.
        log: optional ``callable(str)`` để log "[proxy] ... marked dead".

    Returns:
        True nếu vừa mark dead; False nếu skip (an toàn để gọi vô điều kiện).
    """
    if not line:
        return False
    from web.manager import _is_proxy_network_error, _mask_proxy
    from web.proxy_pool import get_proxy_pool

    if not (
        _is_proxy_network_error(exc_or_msg)
        or _is_browser_closed_proxy_error(exc_or_msg)
        or _is_launch_timeout_proxy_error(exc_or_msg)
    ):
        return False
    if not get_proxy_pool().mark_dead(line):
        return False
    if log is not None:
        try:
            log(f"[proxy] {_mask_proxy(line)} marked dead (network/browser-closed/launch-timeout)")
        except Exception:  # noqa: BLE001 — logging fail không chặn flow
            pass
    return True


@dataclass
class AutoRegConfig:
    """Runtime config cho AutoRegRunner, build từ API request body."""

    # Default concurrency = 3 (Phase C optimization): browser pool cho hybrid
    # mode share Camoufox xuyên signup → 3 signup parallel chỉ tốn 1 browser
    # process (vs 1 signup serial). Tăng throughput x3 với cùng tài nguyên.
    # User có thể override qua API body hoặc giảm về 1 khi test/debug.
    concurrency: int = 3
    poll_interval: int = 30
    default_password: str = ""
    logs_url: str = ""
    api_key: str = ""
    # Shared config từ Settings store (reg.* namespace) — inject tại start time
    headless: bool = True
    job_timeout: float = 240.0
    auto_retry: bool = False
    auto_retry_max: int = 3
    auto_retry_delay: float = 30.0
    # Reg pipeline mode (Settings ``reg_mode.current``) — default ``browser`` để
    # backward compat. Hợp lệ: "browser" | "hybrid" (pure_request đã gỡ khỏi reg).
    reg_mode: str = "browser"
    # Gate proxy pool cho autoreg (Settings ``reg.use_proxy``). False → autoreg
    # bypass pool, mỗi email chạy DIRECT từ IP host. Mirror JobManager._use_proxy
    # cho UI manual reg — autoreg trước đây bị bypass setting này (bug có sẵn).
    use_proxy: bool = True


@dataclass
class AutoRegStats:
    """Cumulative stats, monotonically non-decreasing."""

    processed: int = 0
    success: int = 0
    errors: int = 0


class AutoRegRunner:
    """Async runner tự động poll icloud_emails + signup ChatGPT.

    Lifecycle:
        start(config) → spawns poll loop task
        stop() → sets cancel_event, non-blocking
        finally block reset _running=False
    """

    def __init__(
        self,
        *,
        log_callback: LogCallback,
        account_repo: "ChatGptAccountRepository",
    ) -> None:
        self._log_cb = log_callback
        self._account_repo = account_repo
        self._running: bool = False
        self._cancel_event: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._stats = AutoRegStats()
        self._current_cycle: int = 0
        self._config: AutoRegConfig | None = None

    # ── Read-only properties ─────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def stats(self) -> AutoRegStats:
        return self._stats

    @property
    def current_cycle(self) -> int:
        return self._current_cycle

    # ── Lifecycle ────────────────────────────────────────────────────

    async def start(self, config: AutoRegConfig) -> None:
        """Start poll loop. Raises RuntimeError if already running."""
        if self._running:
            raise RuntimeError("AutoRegRunner is already running")

        self._config = config
        self._cancel_event = asyncio.Event()
        self._running = True
        self._current_cycle = 0

        self._task = asyncio.create_task(self._poll_loop())

    def stop(self) -> None:
        """Signal graceful stop via cancel_event. Non-blocking.

        Also cancels the main task directly to force-break workers
        that are stuck in long-running operations (browser automation).
        """
        if self._cancel_event is not None:
            self._cancel_event.set()
        # Force-cancel the task if it's still alive after setting the event
        if self._task is not None and not self._task.done():
            self._task.cancel()

    # ── Internal: poll loop ──────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Main loop: poll → process batch → sleep → repeat.

        Optimization: skip sleep khi batch đầy (còn email chờ xử lý).
        Pipeline pattern: dùng asyncio.Queue + persistent workers thay vì
        batch-then-sleep — tối đa throughput, không lãng phí thời gian idle.
        """
        workers: list[asyncio.Task] = []
        try:
            await self._log("info", "AutoReg started", {
                "concurrency": self._config.concurrency,
                "poll_interval": self._config.poll_interval,
            })

            # Pipeline: queue + persistent worker tasks
            queue: asyncio.Queue[str] = asyncio.Queue()

            async def _worker(worker_id: int) -> None:
                """Persistent worker — pick email từ queue, process, lặp lại."""
                while not self._cancel_event.is_set():
                    try:
                        email = await asyncio.wait_for(
                            queue.get(), timeout=2.0
                        )
                    except asyncio.TimeoutError:
                        continue
                    try:
                        await self._process_email(email)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        _log.warning("Worker %d unhandled: %s", worker_id, exc)
                    finally:
                        queue.task_done()

            # Spawn persistent workers (concurrency cố định)
            for i in range(self._config.concurrency):
                workers.append(asyncio.create_task(_worker(i)))

            while not self._cancel_event.is_set():
                self._current_cycle += 1

                # Query emails với status='created'
                batch_limit = self._config.concurrency * 2
                try:
                    emails = await asyncio.to_thread(
                        self._account_repo.get_created_emails,
                        batch_limit,
                    )
                except Exception as exc:
                    await self._log("error", f"DB query failed: {exc}", {})
                    if await self._interruptible_sleep(self._config.poll_interval):
                        break
                    continue

                if not emails:
                    await self._log(
                        "info",
                        f"Cycle #{self._current_cycle}: no emails available, sleeping {self._config.poll_interval}s",
                        {"cycle": self._current_cycle},
                    )
                    if await self._interruptible_sleep(self._config.poll_interval):
                        break
                    continue

                await self._log(
                    "info",
                    f"Cycle #{self._current_cycle}: enqueue {len(emails)} email(s)",
                    {"cycle": self._current_cycle, "count": len(emails)},
                )

                # Đẩy vào queue — workers tự pick
                for email in emails:
                    if self._cancel_event.is_set():
                        break
                    await queue.put(email)

                # Đợi queue drain (tất cả email trong batch được xử lý)
                # Nhưng interruptible bởi cancel
                drain_task = asyncio.create_task(queue.join())
                cancel_task = asyncio.create_task(self._cancel_event.wait())
                done, pending = await asyncio.wait(
                    {drain_task, cancel_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                if cancel_task in done:
                    break

                # Optimization: nếu batch đầy → còn email → query ngay, không sleep
                if len(emails) >= batch_limit:
                    continue

                # Batch không đầy → hết email hoặc gần hết → sleep rồi poll tiếp
                if await self._interruptible_sleep(self._config.poll_interval):
                    break

            # Cleanup workers
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

        except asyncio.CancelledError:
            # Force-cancel workers when task itself is cancelled
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        except Exception as exc:
            await self._log("error", f"Poll loop crashed: {type(exc).__name__}: {exc}", {})
            _log.exception("AutoRegRunner poll loop crashed")
            # Cleanup workers on crash too
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        finally:
            self._running = False
            self._task = None
            try:
                await self._log("info", "AutoReg stopped", {
                    "stats": {
                        "processed": self._stats.processed,
                        "success": self._stats.success,
                        "errors": self._stats.errors,
                    },
                })
            except Exception:
                pass  # Don't let logging failure prevent cleanup

    # ── Internal: process single email ───────────────────────────────

    async def _process_email(self, email: str) -> None:
        """Single email signup: build request → run_signup → enable_2fa → persist.

        Respects reg config: proxy, headless, job_timeout, auto_retry.
        On final failure: update icloud_emails status to 'disabled' to prevent infinite retry.
        """
        from mfa_phase import MfaError, enable_2fa
        from models import SignupRequest, SignupResult
        from signup import (
            run_signup,
            await_with_extendable_deadline,
            make_otp_grace_checkpoint,
            SIGNUP_BASE_TIMEOUT_FLOOR,
            SIGNUP_POST_OTP_GRACE,
        )
        from web.mail_modes import get_spec

        self._stats.processed += 1
        max_attempts = (self._config.auto_retry_max + 1) if self._config.auto_retry else 1

        for attempt in range(1, max_attempts + 1):
            if self._cancel_event.is_set():
                return

            email_proxy: str | None = None
            email_proxy_line: str | None = None
            _proxy_leased: bool = False
            try:
                prefix = f"[{email}]" if max_attempts == 1 else f"[{email}][attempt {attempt}/{max_attempts}]"
                await self._log("info", f"{prefix} Starting signup", {"email": email, "attempt": attempt})

                # Build SignupRequest via worker spec — inject proxy + headless từ Settings
                # Proxy lease theo least-used + no-immediate-repeat (pool.acquire) →
                # N email song song với ≥N proxy live = mỗi email 1 IP riêng,
                # KHÔNG bị trùng do random.choice. Probe mode fallback
                # acquire_live_proxy (no lease). Phải release ở finally để
                # giảm lease count khi acc xong.
                #
                # Gate ``self._config.use_proxy`` (Settings ``reg.use_proxy``):
                # False → skip acquire, chạy direct (mirror JobManager._use_proxy
                # cho UI manual reg). Trước đây autoreg bypass setting này.
                if self._config.use_proxy:
                    from web.manager import _acquire_job_proxy_lease
                    email_proxy, email_proxy_line, _proxy_leased = await _acquire_job_proxy_lease()
                    # Log proxy đã chọn (mask credential) — observability cho user
                    # biết acc đi IP gì, phát hiện sớm pool stuck/lệch IP.
                    from web.proxy_format import mask_proxy as _mask
                    await self._log(
                        "info",
                        f"{prefix} [proxy] {_mask(email_proxy)}",
                        {"email": email, "proxy": _mask(email_proxy)},
                    )
                else:
                    await self._log(
                        "info",
                        f"{prefix} [proxy] disabled — chạy direct (no proxy)",
                        {"email": email},
                    )
                worker_config = self._resolve_worker_config(self._config)
                spec = get_spec("worker")
                parsed = spec.parse_line(email)
                request = spec.build_request(
                    parsed,
                    worker_config=worker_config,
                    password=self._config.default_password or None,
                    headless=self._config.headless,
                    proxy=email_proxy,
                    reg_mode=self._config.reg_mode,
                )

                # Run signup with job_timeout
                _bg_tasks: set[asyncio.Task] = set()

                def log_fn(msg: str) -> None:
                    coro = self._log("info", f"{prefix} {msg}", {"email": email})
                    try:
                        task = asyncio.create_task(coro)
                    except RuntimeError:
                        # Event loop closed — đóng coroutine để tránh RuntimeWarning
                        # "coroutine was never awaited".
                        coro.close()
                        return
                    _bg_tasks.add(task)
                    task.add_done_callback(_bg_tasks.discard)

                # Deadline gia hạn theo checkpoint OTP: base phủ trọn thời gian chờ
                # OTP; sau khi lấy được OTP, job luôn còn SIGNUP_POST_OTP_GRACE giây
                # để hoàn tất → không kill ngay sau khi đã có OTP.
                _loop = asyncio.get_event_loop()
                _base = max(float(self._config.job_timeout), SIGNUP_BASE_TIMEOUT_FLOOR)
                _deadline_holder = [_loop.time() + _base]
                _on_cp = make_otp_grace_checkpoint(
                    _deadline_holder, SIGNUP_POST_OTP_GRACE, _loop
                )
                result: SignupResult = await await_with_extendable_deadline(
                    run_signup(request, log=log_fn, on_checkpoint=_on_cp),
                    _deadline_holder, loop=_loop,
                )

                if not result.success:
                    error_msg = result.error or "signup failed"
                    # Loại proxy lỗi network khỏi pool (mark_dead key = raw line, F-J).
                    self._note_proxy_failure(email_proxy_line, error_msg, prefix=prefix)

                    # ── Skip retry cho error permanent (không phục hồi được) ─
                    # Các signal "email đã đăng ký" hoặc "account đã tồn tại"
                    # → retry vô ích, server vẫn trả cùng lý do. Mark email
                    # disabled ngay để autoreg pick email kế.
                    #   - invalid_auth_step: server từ chối /register vì auth state
                    #   - user_already_exists: /about-you trả error_code này
                    #   - AccountAlreadyExistsError: exception class browser_phase raise
                    error_lower = error_msg.lower()
                    permanent_signals = (
                        "invalid_auth_step",
                        "đã được đăng ký",
                        "user_already_exists",
                        "accountalreadyexistserror",
                    )
                    if any(sig in error_lower for sig in permanent_signals):
                        self._stats.errors += 1
                        await self._log("error", f"{prefix} Email đã đăng ký — skip retry, mark disabled: {error_msg}", {
                            "email": email, "error": error_msg,
                        })
                        await self._mark_email_failed(email)
                        return

                    # Retry?
                    if attempt < max_attempts:
                        delay = self._config.auto_retry_delay * attempt
                        await self._log("warn", f"{prefix} Failed: {error_msg} — retry in {delay:.0f}s", {
                            "email": email, "error": error_msg, "attempt": attempt,
                        })
                        if await self._interruptible_sleep(delay):
                            return  # cancelled
                        continue
                    # Final failure — mark email as disabled
                    self._stats.errors += 1
                    await self._log("error", f"{prefix} Signup failed (no more retries): {error_msg}", {
                        "email": email, "error": error_msg,
                    })
                    await self._mark_email_failed(email)
                    return

                # Signup succeeded — enable 2FA
                secret_2fa: str | None = None
                password = result.password or self._config.default_password

                if result.access_token:
                    await self._log("info", f"{prefix} Enabling 2FA...", {"email": email})
                    try:
                        mfa_result = result.two_factor
                        if mfa_result is not None:
                            log_fn("[2fa] dùng enroll inline (CF-clean) — skip enable_2fa")
                        else:
                            mfa_result = await enable_2fa(
                                access_token=result.access_token,
                                cookies=result.cookies,
                                proxy=email_proxy,
                                pending_enrollment=result.two_factor_partial,
                                log=log_fn,
                            )
                        secret_2fa = mfa_result.get("secret")
                        await self._log("info", f"{prefix} 2FA enabled", {
                            "email": email, "secret": secret_2fa,
                        })
                    except MfaError as exc:
                        await self._log("warn", f"{prefix} 2FA failed: {exc}", {
                            "email": email, "error": str(exc),
                        })
                else:
                    await self._log("warn", f"{prefix} No access_token, skipping 2FA", {
                        "email": email,
                    })

                # Persist success: INSERT chatgpt_accounts + UPDATE icloud_emails
                try:
                    await asyncio.to_thread(
                        self._account_repo.persist_success,
                        email,
                        password,
                        secret_2fa,
                    )
                except Exception as exc:
                    self._stats.errors += 1
                    await self._log("error", f"{prefix} DB persist failed: {exc}", {
                        "email": email, "error": str(exc),
                    })
                    return

                self._stats.success += 1
                await self._log("success", f"{email}|{password}|{secret_2fa or ''}", {
                    "email": email, "password": password, "secret_2fa": secret_2fa,
                })
                return  # done

            except asyncio.TimeoutError:
                error_msg = f"timeout {self._config.job_timeout:.0f}s exceeded"
                if attempt < max_attempts:
                    delay = self._config.auto_retry_delay * attempt
                    await self._log("warn", f"{prefix} {error_msg} — retry in {delay:.0f}s", {
                        "email": email, "error": error_msg, "attempt": attempt,
                    })
                    if await self._interruptible_sleep(delay):
                        return
                    continue
                self._stats.errors += 1
                await self._log("error", f"{prefix} {error_msg} (no more retries)", {
                    "email": email, "error": error_msg,
                })
                await self._mark_email_failed(email)
                return

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error_msg = f"{type(exc).__name__}: {exc}"
                # Loại proxy lỗi network khỏi pool (mark_dead key = raw line, F-J).
                self._note_proxy_failure(email_proxy_line, exc, prefix=prefix)

                # Skip retry cho error permanent — đồng nhất với nhánh
                # ``not result.success`` ở trên.
                error_lower = error_msg.lower()
                permanent_signals = (
                    "invalid_auth_step",
                    "đã được đăng ký",
                    "user_already_exists",
                    "accountalreadyexistserror",
                )
                if any(sig in error_lower for sig in permanent_signals):
                    self._stats.errors += 1
                    await self._log("error", f"{prefix} Email đã đăng ký — skip retry, mark disabled: {error_msg}", {
                        "email": email, "error": error_msg,
                    })
                    await self._mark_email_failed(email)
                    return

                if attempt < max_attempts:
                    delay = self._config.auto_retry_delay * attempt
                    await self._log("warn", f"{prefix} {error_msg} — retry in {delay:.0f}s", {
                        "email": email, "error": error_msg, "attempt": attempt,
                    })
                    if await self._interruptible_sleep(delay):
                        return
                    continue
                self._stats.errors += 1
                await self._log("error", f"{prefix} Unexpected error (no more retries): {error_msg}", {
                    "email": email, "error": error_msg,
                })
                await self._mark_email_failed(email)
                return
            finally:
                # Trả lease cho proxy bất kể outcome (success/fail/cancel) để
                # ``pool._leases`` không leak — least-used view chính xác cho
                # email kế tiếp. No-op khi probe mode (requires_release=False)
                # hoặc pool rỗng (line=None).
                from web.manager import _release_job_proxy_lease
                _release_job_proxy_lease(email_proxy_line, _proxy_leased)

    def _note_proxy_failure(self, line: str | None, exc_or_msg, *, prefix: str = "") -> None:
        """Mark proxy line chết khi gặp lỗi network — key = raw pool line (F-J).

        Delegate ``mark_proxy_dead_on_error`` module-level (DRY với
        ``test/run_hybrid_live_regdata.py`` + diag scripts). Idempotent.
        """
        from web.proxy_format import mask_proxy

        # Log fire-and-forget khi mark thành công — không chặn flow signup.
        def _emit_log(msg: str) -> None:
            # ``msg`` đã có prefix ``[proxy] ...`` từ helper. Chỉ cần prepend
            # job-prefix + "lỗi network — loại khỏi pool" tail như cũ để giữ
            # log signature tương thích (operators đang grep dòng này).
            full = f"{prefix} [proxy] {mask_proxy(line)} lỗi network — loại khỏi pool"
            try:
                asyncio.create_task(
                    self._log("warn", full, {"proxy": mask_proxy(line)})
                )
            except RuntimeError:
                pass

        mark_proxy_dead_on_error(line, exc_or_msg, log=_emit_log)

    async def _mark_email_failed(self, email: str) -> None:
        """Update icloud_emails status → 'disabled' để không bị pick lại.

        Dùng khi signup fail after max retries — email này không thể dùng nữa.
        """
        try:
            def _do_mark():
                with self._account_repo.engine.get_connection() as conn:
                    conn.execute(
                        "UPDATE icloud_emails SET status = 'disabled' "
                        "WHERE email = ? AND status = 'created'",
                        (email,),
                    )

            await asyncio.to_thread(_do_mark)
        except Exception as exc:
            _log.warning("Failed to mark email %s as disabled: %s", email, exc)

    # ── Internal: helpers ────────────────────────────────────────────

    async def _interruptible_sleep(self, seconds: float) -> bool:
        """Sleep interruptible by cancel_event.

        Returns True if interrupted (should break loop).
        """
        try:
            await asyncio.wait_for(self._cancel_event.wait(), timeout=seconds)
            return True  # cancelled
        except asyncio.TimeoutError:
            return False  # slept full duration

    @staticmethod
    def _resolve_worker_config(config: AutoRegConfig) -> dict[str, str]:
        """Resolve logs_url + api_key. Priority: UI input > env var > hardcoded default."""
        return {
            "logs_url": (
                config.logs_url
                or os.environ.get("HYBRID_WORKER_LOGS_URL", "")
                or "https://icloud-cf-mail.n5pskgzs9g.workers.dev/logs"
            ),
            "api_key": (
                config.api_key
                or os.environ.get("HYBRID_WORKER_API_KEY", "")
            ),
        }

    async def _log(self, level: str, message: str, payload: dict[str, Any]) -> None:
        """Dispatch log event qua callback. Best-effort, không raise."""
        try:
            await self._log_cb(level, message, payload)
        except Exception as exc:
            _log.warning("log_callback failed: %s", exc)
