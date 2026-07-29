"""Browser pool cho hybrid mode — share Camoufox xuyên multi-signup.

Architecture:
    HybridBrowserPool (singleton, module-level)
      └── _CamoufoxRunner (per-proxy, share 1 Browser, lazy init)
            ├── _PlaywrightThread (dedicated executor, max_workers=1)
            │     • Mọi sync Playwright op chạy trên 1 thread cố định
            │     • Guarantee thread-affinity (sync API non-thread-safe)
            │     • Re-entrancy guard tránh deadlock khi nested submit
            ├── _SharedBrowser (Camoufox sync_api, lifetime quản lý qua refcount)
            └── HybridContextHandle (per-signup, isolated BrowserContext + Page)
                  • CamoufoxTokenGenerator interface
                  • Page load `sentinel.openai.com/.../frame.html` → sdk.js
                  • Bridge ``postMessage`` mint token/so

Tiết kiệm tốc độ:
    Cold launch Camoufox ~5-10s. Pool reuse browser → mỗi signup chỉ trả phí
    ``new_context() + page.goto(frame.html) + sdk.js load`` ~2-3s. Net ~5-7s/signup.

Thread-safety + asyncio compatibility:
    Playwright sync_api có 2 ràng buộc cứng:
      1. KHÔNG được gọi trong thread đang có asyncio event loop chạy
         (Playwright self-check sẽ raise NotImplementedError "Sync API inside
         the asyncio loop").
      2. Browser/Context/Page có thread-affinity — instance tạo ở thread T1 chỉ
         được dùng ở T1 (không phải just-serialized, mà CÙNG thread).

    `run_hybrid_signup` là async function chạy trong asyncio loop. Để thoả mãn
    cả 2 ràng buộc trên, pool route 100% Playwright op qua 1 dedicated thread
    (``_PlaywrightThread``) riêng biệt — thread này KHÔNG có event loop, và
    mọi browser/context/page đều sống xuyên thread đời pool runner.

Lifecycle:
    - Browser lazy launch ở ``_CamoufoxRunner.acquire_context`` đầu tiên.
    - Context.close() ngay sau signup (refcount release).
    - Browser KHÔNG tự close — chỉ shutdown qua ``shutdown()`` explicit hoặc
      ``atexit`` hook (đảm bảo cleanup khi process exit).

Cô lập (anti-cluster):
    Mỗi signup acquire BrowserContext riêng — cookies, storage, IndexedDB isolated.
    sdk.js trong page không share state giữa context. cf_clearance riêng per context.
"""
from __future__ import annotations

import atexit
import concurrent.futures
import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from ._proc_reaper import BrowserProcessReaper

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from chatgpt_camoufox.chatgpt_camoufox.camoufox_vm import SentinelToken


# ─────────────────────────────────────────────────────────────────────
# Constants — mirror chatgpt_camoufox.camoufox_vm
# ─────────────────────────────────────────────────────────────────────

_FRAME_URL = "https://sentinel.openai.com/backend-api/sentinel/frame.html"

# Cookies Camoufox earn mà curl_cffi cần replay (xem chatgpt_camoufox docs).
_SHARED_COOKIE_NAMES = frozenset({
    "cf_clearance", "__cf_bm", "__cflb", "_cfuvid", "oai-sc", "oai-did",
})

# Bridge JS — copy verbatim từ chatgpt_camoufox.camoufox_vm._BRIDGE_CALL để
# tránh phụ thuộc cứng vào private constant của package.
_BRIDGE_CALL = """
async ([kind, flow]) => {
  return await new Promise((resolve) => {
    const id = Math.random().toString(36).slice(2) + Date.now().toString(36);
    function handler(ev) {
      const d = ev.data;
      if (d && d.__sres === id) {
        window.removeEventListener('message', handler);
        resolve({ ok: d.ok, value: d.value, err: d.err });
      }
    }
    window.addEventListener('message', handler);
    window.postMessage({ __sreq: true, id, kind, flow }, '*');
    setTimeout(() => resolve({ ok: false, err: 'bridge timeout' }), 30000);
  });
}
"""

# NOTE: Observer feeder/burst (synthetic DOM events) đã được GỠ BỎ — golden
# ``CamoufoxTokenGenerator`` mint ``so`` trên page TĨNH headless KHÔNG feed event
# nào và vẫn ra token hợp lệ. Feeder cũ chỉ tạo signature máy móc lệch golden
# (drift nhánh B / soTokenFromSyntheticEvents). Pool path nay bám golden.

# Idle TTL — browser stay alive sau khi context cuối close. Sau TTL idle, browser
# tự shutdown. Đặt 5 min để cover gap giữa các signup trong cùng AutoReg cycle.
_BROWSER_IDLE_TTL_SECONDS = 300.0

# Cap thời gian chờ cho các op TEARDOWN (close context / shutdown browser). Op
# teardown có thể treo nếu browser/page wedged; cap để caller (cleanup async /
# atexit) KHÔNG block vô hạn. Op vẫn chạy nốt trên daemon worker — không leak block.
_TEARDOWN_TIMEOUT_SECONDS = 30.0

# ── Bound timeout cho op KHÔNG-teardown (fix bug "multi-signup launch-hang") ──
# Trước fix: launch/acquire/mint chạy ``_PlaywrightThread.run(timeout=None)`` →
# treo VÔ HẠN nếu Camoufox launch/sentinel mint wedged → outer watchdog 420s mới
# cắt. Fix: cap thời gian từng op để fail-fast, raise ``HybridBrowserPoolError``
# cho ``run_hybrid_signup`` classify + retry/dừng, KHÔNG nuốt lỗi.
#   - LAUNCH/ACQUIRE: cold-launch Camoufox + goto frame.html + bridge ready
#     thường ~2-10s; cho 90s buffer cho proxy chậm / CF challenge.
#   - MINT: page.evaluate sentinel sdk (token/so) thường ~1-3s; cho 45s buffer.
_LAUNCH_TIMEOUT_SECONDS = 90.0
_MINT_TIMEOUT_SECONDS = 45.0
# Grace giữa SIGTERM → SIGKILL khi reaper force-kill browser wedged.
_KILL_GRACE_SECONDS = 3.0


class HybridBrowserPoolError(RuntimeError):
    """Lỗi runtime pool (browser launch / context fail)."""


# ─────────────────────────────────────────────────────────────────────
# _PlaywrightThread — dedicated thread executor (thread-affinity + asyncio-safe)
# ─────────────────────────────────────────────────────────────────────


class _PlaywrightThread:
    """Single DAEMON worker thread + FIFO queue để route Playwright sync API.

    Lý do tồn tại:
      1. **Asyncio safety**: Playwright sync_api raise NotImplementedError khi
         được gọi từ thread có asyncio loop đang chạy. Caller của pool là
         ``run_hybrid_signup`` (async coroutine, chạy trong asyncio thread cha).
         Enqueue → chạy trên dedicated worker thread KHÔNG có event loop.

      2. **Thread-affinity**: Playwright Python sync_api dùng greenlet per
         (process, thread). Browser/Context/Page tạo ở thread T1 chỉ dùng được
         ở T1 — gọi từ T2 sẽ raise/treo. 1 worker thread cố định đảm bảo mọi op
         chạy ở CÙNG MỘT thread.

      3. **Re-entrancy guard**: Nếu method A chạy trong worker thread và gọi
         method B (cũng route qua queue) — sẽ tự deadlock (worker chờ chính nó).
         Guard qua so khớp identity ``threading.current_thread() is self._worker``
         để detect re-entry và gọi inline.

      4. **Không treo process exit**: worker là DAEMON + ``shutdown()``
         non-blocking + ``run(timeout=...)`` cho close path → 1 op Playwright
         treo (browser wedged) KHÔNG block caller vô hạn, cũng KHÔNG bị join ở
         interpreter shutdown (khác ThreadPoolExecutor non-daemon → hang).
    """

    def __init__(self, name: str = "camoufox-pool") -> None:
        # 1 worker thread DAEMON cố định + FIFO queue. Daemon = process exit
        # KHÔNG bị block khi 1 op Playwright treo (browser wedged) — đây là
        # khác biệt cốt lõi so với ThreadPoolExecutor (worker non-daemon, bị
        # join ở interpreter shutdown → hang). Mọi op vẫn chạy trên CÙNG 1
        # thread → giữ thread-affinity Playwright sync_api.
        self._queue: "queue.SimpleQueue[Any]" = queue.SimpleQueue()
        self._shutdown = False
        self._shutdown_lock = threading.Lock()
        self._worker = threading.Thread(
            target=self._loop, name=name, daemon=True,
        )
        self._worker.start()

    _SHUTDOWN = object()  # sentinel để worker thoát vòng lặp

    def _loop(self) -> None:
        """Vòng lặp worker — drain queue, chạy từng fn, set kết quả vào future."""
        while True:
            item = self._queue.get()
            if item is self._SHUTDOWN:
                return
            fn, args, kwargs, future = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn(*args, **kwargs))
            except BaseException as exc:  # noqa: BLE001 — propagate lên caller
                future.set_exception(exc)

    def _in_executor(self) -> bool:
        return threading.current_thread() is self._worker

    def run(
        self, fn: Callable[..., Any], *args: Any,
        timeout: float | None = None, **kwargs: Any,
    ) -> Any:
        """Chạy ``fn(*args, **kwargs)`` ở dedicated worker thread.

        Khi caller ĐÃ ở worker thread (re-entrant case), gọi inline để tránh
        deadlock (worker chờ chính nó). Khi caller ở thread khác (asyncio thread
        / pre-mint thread / atexit thread), enqueue + block-wait result.

        Args:
            timeout: None = chờ vô hạn (giữ behavior cũ cho op thường). Số giây
                = cap thời gian chờ — dùng cho close/shutdown để KHÔNG treo vô
                hạn nếu browser wedged. Hết timeout → raise
                ``concurrent.futures.TimeoutError`` (task vẫn chạy nốt trên
                daemon worker, không leak block).

        Raises: re-raise nguyên gốc exception từ ``fn``. Nếu đã shutdown và caller
        submit task mới từ thread ngoài, raise ``HybridBrowserPoolError``.
        """
        # Re-entrant: caller đang ở worker thread → gọi inline.
        if self._in_executor():
            return fn(*args, **kwargs)

        if self._shutdown:
            raise HybridBrowserPoolError(
                "_PlaywrightThread đã shutdown — không thể submit task mới"
            )

        future: concurrent.futures.Future = concurrent.futures.Future()
        self._queue.put((fn, args, kwargs, future))
        return future.result(timeout=timeout)

    def shutdown(self) -> None:
        """Báo worker thoát. Idempotent + non-blocking.

        Không join worker (daemon — tự chết khi process exit). Task đang chạy /
        đã enqueue trước sentinel vẫn drain xong theo FIFO. Sau shutdown, mọi
        ``run()`` từ thread ngoài raise; ``run()`` từ chính worker (inline) vẫn pass.
        """
        with self._shutdown_lock:
            if self._shutdown:
                return
            self._shutdown = True
        self._queue.put(self._SHUTDOWN)


# ─────────────────────────────────────────────────────────────────────
# _CamoufoxRunner — 1 Camoufox process, serialize call qua dedicated thread
# ─────────────────────────────────────────────────────────────────────


class _CamoufoxRunner:
    """Quản lý 1 Camoufox browser xuyên multi-signup cho cùng config.

    Lazy init: chỉ launch browser khi first ``acquire_context``. Mọi sync
    Playwright op route qua ``_PlaywrightThread`` (dedicated thread) → an toàn
    cho cả asyncio caller và thread-affinity.

    Thread-safety:
        - Public API (``acquire_context`` / ``release_context`` / ``shutdown``)
          gọi được từ bất kỳ thread nào (asyncio thread, pre-mint thread,
          atexit, ...). Tất cả route qua ``self._thread.run(...)``.
        - Internal methods (``_acquire_context_in_thread`` etc.) CHỈ chạy
          trong executor thread.
    """

    def __init__(
        self,
        *,
        proxy: str | None,
        headless: bool,
        insecure: bool,
        log: Callable[[str], None],
    ) -> None:
        self._proxy = proxy
        self._headless = headless
        self._insecure = insecure
        self._log = log
        # Dedicated thread cho mọi Playwright op của runner này.
        self._thread = _PlaywrightThread(
            name=f"camoufox-runner-{id(self):x}",
        )
        # Reaper force-kill browser wedged (chốt baseline TRƯỚC khi launch để
        # chỉ kill process browser do CHÍNH runner này spawn).
        self._reaper = BrowserProcessReaper(log)
        self._reaper.mark_baseline()
        # _state_lock bảo vệ counter + last_used khỏi đọc/ghi cross-thread.
        # KHÔNG bảo vệ Playwright ops (đã serialize ở _thread).
        self._state_lock = threading.Lock()
        self._browser_cm = None  # Camoufox context manager
        self._browser = None
        self._ref_count = 0
        self._last_used = time.monotonic()
        # Cache sdk.js + harness source (load 1 lần).
        self._page_script: str | None = None

    @property
    def thread(self) -> _PlaywrightThread:
        """Public access cho ``HybridContextHandle`` route call qua thread."""
        return self._thread

    # ── Internal methods — CHỈ gọi trong executor thread ──────────────

    def _load_page_script(self) -> str:
        """Đọc sdk.js + harness từ chatgpt_camoufox/assets, ghép thành 1 script."""
        if self._page_script is not None:
            return self._page_script
        from chatgpt_camoufox.chatgpt_camoufox.camoufox_vm import (
            SDK_PATH, HARNESS_PATH,
        )
        with open(SDK_PATH, "r", encoding="utf-8") as f:
            sdk_src = f.read()
        with open(HARNESS_PATH, "r", encoding="utf-8") as f:
            bridge_src = f.read()
        # Expose SentinelSDK on window (chatgpt_camoufox replace pattern).
        self._page_script = sdk_src.replace(
            "var SentinelSDK=", "window.SentinelSDK=", 1,
        ) + "\n;" + bridge_src
        return self._page_script

    def _ensure_browser_in_thread(self) -> Any:
        """Lazy launch Camoufox browser trong executor thread."""
        if self._browser is not None:
            return self._browser
        from camoufox.sync_api import Camoufox

        opts: dict = {"headless": self._headless}
        if self._proxy:
            opts["proxy"] = {"server": self._proxy}
        # Camoufox bám golden chatgpt_camoufox: Windows OS (xem
        # reg_hybrid/camoufox_factory _DEFAULT_PLATFORM).
        opts["os"] = "windows"

        self._log(
            f"[hybrid-pool] launching Camoufox "
            f"(proxy={'***' if self._proxy else 'direct'} "
            f"headless={self._headless} insecure={self._insecure})"
        )
        self._browser_cm = Camoufox(**opts)
        self._browser = self._browser_cm.__enter__()
        return self._browser

    def _acquire_context_in_thread(self) -> "HybridContextHandle":
        """Tạo BrowserContext + Page (frame.html) + inject sdk.js — IN THREAD."""
        browser = self._ensure_browser_in_thread()
        ctx_opts: dict = {}
        if self._insecure:
            ctx_opts["ignore_https_errors"] = True
        context = browser.new_context(**ctx_opts)
        page = context.new_page()
        page.goto(_FRAME_URL, wait_until="domcontentloaded", timeout=45000)
        page.add_script_tag(content=self._load_page_script())
        # Wait bridge ready (selector inject bởi camoufox_harness.js).
        try:
            page.wait_for_selector(
                "#__sentinel_bridge_ready",
                state="attached", timeout=15000,
            )
        except Exception as exc:
            err_text = None
            try:
                err_el = page.query_selector("#__sentinel_bridge_error")
                if err_el is not None:
                    err_text = err_el.text_content()
            except Exception:
                pass
            # Cleanup context dở trước khi raise — tránh leak.
            try:
                page.close()
            except Exception:
                pass
            try:
                context.close()
            except Exception:
                pass
            raise HybridBrowserPoolError(
                f"sentinel bridge init failed: {err_text or exc}"
            ) from exc
        # Setup page KHỚP golden ``CamoufoxTokenGenerator._ensure_page``: goto
        # frame.html → add sdk.js + harness → wait ``#__sentinel_bridge_ready``.
        # KHÔNG inject synthetic DOM events — golden mint ``so`` trên page TĨNH
        # headless và vẫn ra token hợp lệ, nên Observer feeder cũ chỉ tạo signature
        # máy móc lệch golden (drift nhánh B). Bỏ feeder để bám golden.
        return HybridContextHandle(runner=self, context=context, page=page)

    def _release_context_in_thread(self, handle: "HybridContextHandle") -> None:
        """Close context + page — IN THREAD."""
        try:
            handle.page.close()
        except Exception:
            pass
        try:
            handle.context.close()
        except Exception:
            pass

    def _shutdown_in_thread(self) -> None:
        """Đóng browser — IN THREAD."""
        cm = self._browser_cm
        self._browser_cm = None
        self._browser = None
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception as exc:
                self._log(f"[hybrid-pool] shutdown browser warn: {exc}")

    # ── Public API — gọi được từ bất kỳ thread ────────────────────────

    def acquire_context(self) -> "HybridContextHandle":
        """Tạo BrowserContext isolated + Page (frame.html) + inject sdk.js.

        Block đến khi page load + bridge ready. Raise ``HybridBrowserPoolError``
        nếu Camoufox launch fail hoặc bridge không init.

        Bound timeout ``_LAUNCH_TIMEOUT_SECONDS``: nếu cold-launch Camoufox treo
        (proxy chết / CF challenge wedged) → KHÔNG block vô hạn, force-kill
        browser orphan + raise để outer-loop classify.
        """
        try:
            handle = self._thread.run(
                self._acquire_context_in_thread,
                timeout=_LAUNCH_TIMEOUT_SECONDS,
            )
        except concurrent.futures.TimeoutError as exc:
            # Op treo trên daemon worker → kill browser vừa spawn (nếu có) để
            # không thành orphan, rồi fail-fast.
            self._reaper.kill_new(
                "acquire_context-timeout", grace_seconds=_KILL_GRACE_SECONDS,
            )
            self._log(
                f"[hybrid-pool] acquire_context TIMEOUT "
                f">{_LAUNCH_TIMEOUT_SECONDS:.0f}s — force-killed browser orphan"
            )
            raise HybridBrowserPoolError(
                f"camoufox op timeout (acquire_context > "
                f"{_LAUNCH_TIMEOUT_SECONDS:.0f}s)"
            ) from exc
        except Exception as exc:
            self._log(
                f"[hybrid-pool] acquire_context failed: "
                f"{type(exc).__name__}: {exc}"
            )
            raise
        with self._state_lock:
            self._ref_count += 1
            self._last_used = time.monotonic()
        return handle

    def release_context(self, handle: "HybridContextHandle") -> None:
        """Close context (browser stays alive cho signup kế tiếp)."""
        try:
            self._thread.run(
                self._release_context_in_thread, handle,
                timeout=_TEARDOWN_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 — release best-effort
            self._log(f"[hybrid-pool] release_context warn: {exc}")
        with self._state_lock:
            self._ref_count = max(0, self._ref_count - 1)
            self._last_used = time.monotonic()

    @property
    def ref_count(self) -> int:
        with self._state_lock:
            return self._ref_count

    @property
    def idle_seconds(self) -> float:
        with self._state_lock:
            if self._ref_count > 0:
                return 0.0
            return time.monotonic() - self._last_used

    def shutdown(self) -> None:
        """Đóng browser + dedicated thread. Idempotent + best-effort.

        Sau khi gọi ``_shutdown_in_thread`` (có thể treo nếu browser wedged →
        cap teardown timeout), SWEEP reaper force-kill mọi process browser còn
        sót do runner này spawn → tránh orphan tích lũy.
        """
        timed_out = False
        try:
            self._thread.run(
                self._shutdown_in_thread, timeout=_TEARDOWN_TIMEOUT_SECONDS,
            )
        except concurrent.futures.TimeoutError:
            timed_out = True
            self._log(
                f"[hybrid-pool] shutdown TIMEOUT >{_TEARDOWN_TIMEOUT_SECONDS:.0f}s "
                f"— sẽ force-kill browser"
            )
        except Exception as exc:  # noqa: BLE001
            self._log(f"[hybrid-pool] shutdown warn: {exc}")
        finally:
            # Sweep: kill browser còn sống (close treo HOẶC close "thành công"
            # nhưng Firefox không thực sự chết). Reaper chỉ chạm process delta
            # baseline của runner này.
            self._reaper.kill_new(
                "shutdown-sweep" if not timed_out else "shutdown-timeout",
                grace_seconds=_KILL_GRACE_SECONDS,
            )
            self._thread.shutdown()


# ─────────────────────────────────────────────────────────────────────
# HybridContextHandle — implement CamoufoxTokenGenerator interface
# ─────────────────────────────────────────────────────────────────────


@dataclass
class HybridContextHandle:
    """1 isolated BrowserContext + Page (frame.html) cho 1 signup.

    Implement đầy đủ interface ``CamoufoxTokenGenerator`` của chatgpt_camoufox:
        - ``set_device_id(did)``: seed oai-did cookie vào context.
        - ``export_cookies()``: dump shared cookies (cf_clearance + oai-sc).
        - ``mint_token(flow)``: page bridge → ``SentinelSDK.token(flow)``.
        - ``mint_so(flow)``: page bridge → ``SentinelSDK.sessionObserverToken(flow)``.
        - ``close()``: release context về pool (browser stays).

    Mọi method route qua ``runner.thread.run(...)`` → dedicated thread của runner
    → guarantee thread-affinity + asyncio-safe (caller có thể là async coroutine).
    """

    runner: _CamoufoxRunner
    context: Any
    page: Any
    _device_id: str | None = field(default=None)
    _closed: bool = field(default=False)

    # ── Internal — CHỈ gọi trong executor thread ──────────────────────

    def _set_device_id_in_thread(self, device_id: str) -> None:
        try:
            self.context.add_cookies([
                {"name": "oai-did", "value": device_id,
                 "domain": dom, "path": "/"}
                for dom in (".openai.com", ".chatgpt.com")
            ])
            self._device_id = device_id
        except Exception as exc:
            logger.warning("set_device_id failed: %s", exc)

    def _export_cookies_in_thread(self) -> list[dict]:
        try:
            cookies = self.context.cookies()
        except Exception as exc:
            logger.warning("export_cookies failed: %s", exc)
            return []
        return [c for c in cookies if c.get("name") in _SHARED_COOKIE_NAMES]

    def _mint_token_in_thread(self, flow: str) -> "SentinelToken":
        from chatgpt_camoufox.chatgpt_camoufox.camoufox_vm import (
            EnforcementError, SentinelToken,
        )

        try:
            result = self.page.evaluate(_BRIDGE_CALL, ["token", flow])
        except Exception as exc:
            raise EnforcementError(f"page.evaluate failed: {exc}") from exc
        if not result or not result.get("ok"):
            raise EnforcementError(
                (result or {}).get("err") or "token() failed"
            )
        raw = result.get("value")
        if not isinstance(raw, str) or not raw:
            raise EnforcementError("token() returned empty string")
        return SentinelToken.from_json(raw, flow)

    def _mint_so_in_thread(self, flow: str) -> str:
        from chatgpt_camoufox.chatgpt_camoufox.camoufox_vm import EnforcementError

        # Mint ``so`` KHỚP golden ``CamoufoxTokenGenerator.mint_so``: 1 lần bridge
        # call ``sessionObserverToken(flow)`` trên page tĩnh, KHÔNG synthetic
        # event, KHÔNG retry-burst. Empty → raise như golden để caller xử lý.
        try:
            result = self.page.evaluate(_BRIDGE_CALL, ["so", flow])
        except Exception as exc:
            raise EnforcementError(f"page.evaluate failed: {exc}") from exc
        if not result or not result.get("ok"):
            raise EnforcementError(
                (result or {}).get("err") or "sessionObserverToken() failed"
            )
        val = result.get("value")
        if isinstance(val, dict):
            return val.get("so", "")
        if isinstance(val, str) and val:
            try:
                return json.loads(val).get("so", val)
            except Exception:
                return val
        raise EnforcementError("sessionObserverToken() returned nothing")

    # ── CamoufoxTokenGenerator interface — route qua dedicated thread ─

    def set_device_id(self, device_id: str) -> None:
        """Seed ``oai-did`` cookie vào context để sdk.js dùng cùng id với curl."""
        if self._closed:
            return
        self.runner.thread.run(
            self._set_device_id_in_thread, device_id,
            timeout=_MINT_TIMEOUT_SECONDS,
        )

    def export_cookies(self) -> list[dict]:
        """Return cookies trong ``_SHARED_COOKIE_NAMES`` từ Camoufox context."""
        if self._closed:
            return []
        return self.runner.thread.run(
            self._export_cookies_in_thread, timeout=_MINT_TIMEOUT_SECONDS,
        )

    def mint_token(self, flow: str) -> "SentinelToken":
        """Page bridge → sdk.js ``token(flow)`` → SentinelToken dataclass."""
        from chatgpt_camoufox.chatgpt_camoufox.camoufox_vm import EnforcementError

        if self._closed:
            raise EnforcementError("context closed")
        try:
            return self.runner.thread.run(
                self._mint_token_in_thread, flow, timeout=_MINT_TIMEOUT_SECONDS,
            )
        except concurrent.futures.TimeoutError as exc:
            raise HybridBrowserPoolError(
                f"camoufox op timeout (mint_token > {_MINT_TIMEOUT_SECONDS:.0f}s)"
            ) from exc

    def mint_so(self, flow: str) -> str:
        """Page bridge → sdk.js ``sessionObserverToken(flow)`` → ``so`` string."""
        from chatgpt_camoufox.chatgpt_camoufox.camoufox_vm import EnforcementError

        if self._closed:
            raise EnforcementError("context closed")
        try:
            return self.runner.thread.run(
                self._mint_so_in_thread, flow, timeout=_MINT_TIMEOUT_SECONDS,
            )
        except concurrent.futures.TimeoutError as exc:
            raise HybridBrowserPoolError(
                f"camoufox op timeout (mint_so > {_MINT_TIMEOUT_SECONDS:.0f}s)"
            ) from exc

    def close(self) -> None:
        """Release context về pool. Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            self.runner.release_context(self)
        except Exception as exc:
            logger.warning("release_context failed: %s", exc)

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        self.close()


# ─────────────────────────────────────────────────────────────────────
# HybridBrowserPool — singleton, key by (proxy, headless, insecure)
# ─────────────────────────────────────────────────────────────────────


class HybridBrowserPool:
    """Module-level singleton — multi ``_CamoufoxRunner`` keyed by config."""

    def __init__(self) -> None:
        self._runners: dict[tuple, _CamoufoxRunner] = {}
        self._lock = threading.Lock()

    def _get_or_create_runner(
        self,
        *,
        proxy: str | None,
        headless: bool,
        insecure: bool,
        log: Callable[[str], None],
    ) -> _CamoufoxRunner:
        """Lazy-init runner cho ``(proxy, headless, insecure)``. Thread-safe."""
        key = (proxy or "", bool(headless), bool(insecure))
        with self._lock:
            runner = self._runners.get(key)
            if runner is None:
                runner = _CamoufoxRunner(
                    proxy=proxy, headless=headless, insecure=insecure, log=log,
                )
                self._runners[key] = runner
        return runner

    def acquire(
        self,
        *,
        proxy: str | None,
        headless: bool,
        insecure: bool,
        log: Callable[[str], None],
    ) -> HybridContextHandle:
        """Get or create runner cho ``(proxy, headless, insecure)``,
        acquire 1 context.

        Caller MUST gọi ``handle.close()`` để release context (refcount).
        """
        runner = self._get_or_create_runner(
            proxy=proxy, headless=headless, insecure=insecure, log=log,
        )
        return runner.acquire_context()

    def warm_up(
        self,
        *,
        proxy: str | None,
        headless: bool,
        insecure: bool,
        log: Callable[[str], None],
    ) -> None:
        """Eager-launch Camoufox browser cho config này (KHÔNG acquire context).

        Use case: autoreg / web manager / live test loop muốn loại bỏ cold launch
        ~10s khỏi first signup. Gọi ``warm_up`` ngay khi process khởi động →
        browser sẵn sàng khi ``acquire`` đầu tiên chạy.

        Idempotent: gọi nhiều lần với cùng config chỉ launch 1 lần (cache trong
        runner). Blocking call: chạy qua dedicated thread của runner.
        """
        runner = self._get_or_create_runner(
            proxy=proxy, headless=headless, insecure=insecure, log=log,
        )
        try:
            runner.thread.run(
                runner._ensure_browser_in_thread,
                timeout=_LAUNCH_TIMEOUT_SECONDS,
            )
            log(
                f"[hybrid-pool] warm_up OK "
                f"(proxy={'***' if proxy else 'direct'} "
                f"headless={headless} insecure={insecure})"
            )
        except concurrent.futures.TimeoutError as exc:
            runner._reaper.kill_new(
                "warm_up-timeout", grace_seconds=_KILL_GRACE_SECONDS,
            )
            log(
                f"[hybrid-pool] warm_up TIMEOUT >{_LAUNCH_TIMEOUT_SECONDS:.0f}s "
                f"— force-killed browser orphan"
            )
            raise HybridBrowserPoolError(
                f"camoufox op timeout (warm_up > {_LAUNCH_TIMEOUT_SECONDS:.0f}s)"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            log(f"[hybrid-pool] warm_up failed: {type(exc).__name__}: {exc}")
            raise

    def cleanup_idle(self, *, ttl_seconds: float = _BROWSER_IDLE_TTL_SECONDS) -> int:
        """Shutdown runners idle vượt TTL. Trả số runner đã đóng."""
        closed = 0
        with self._lock:
            stale = [
                (key, r) for key, r in self._runners.items()
                if r.idle_seconds > ttl_seconds
            ]
            for key, runner in stale:
                self._runners.pop(key, None)
                try:
                    runner.shutdown()
                    closed += 1
                except Exception as exc:
                    logger.warning("cleanup_idle shutdown failed: %s", exc)
        return closed

    def shutdown_all(self) -> None:
        """Đóng tất cả runner. Gọi khi process exit."""
        with self._lock:
            runners = list(self._runners.values())
            self._runners.clear()
        for runner in runners:
            try:
                runner.shutdown()
            except Exception as exc:
                logger.warning("shutdown_all failed: %s", exc)


# Module-level singleton.
_POOL: HybridBrowserPool | None = None
_POOL_LOCK = threading.Lock()


def get_pool() -> HybridBrowserPool:
    """Lấy pool singleton. Lazy init + register atexit shutdown."""
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                _POOL = HybridBrowserPool()
                # Đảm bảo cleanup khi process exit (process exit có thể skip
                # finally block của runner → leak browser process). atexit chạy
                # ngay cả khi exit qua SystemExit.
                atexit.register(_POOL.shutdown_all)
    return _POOL


# Knob env: tắt pool (debug/fallback) — dùng để verify pool overhead vs no-pool.
def pool_disabled() -> bool:
    """``HYBRID_POOL_DISABLED=1`` → mỗi signup launch Camoufox riêng (debug)."""
    return os.getenv("HYBRID_POOL_DISABLED", "0").lower() in ("1", "true", "yes")


def pool_enabled() -> bool:
    """Quyết định hybrid có dùng shared Camoufox pool hay không.

    Pool là **OPT-IN**. Default = no-pool: mỗi signup launch
    ``CamoufoxTokenGenerator`` golden riêng (khớp lifecycle golden — launch mới
    mỗi account, close trong finally), vừa tránh cluster fingerprint vừa hết
    hang do single-thread serialization của pool (1 op treo → mọi signup khác
    block vô hạn).

    Precedence (cao → thấp):
      1. Env ``HYBRID_POOL_DISABLED=1`` → luôn no-pool (override cứng cho
         debug/safety, bất chấp setting).
      2. Settings Store key ``reg.hybrid_pool_enabled`` (bool) — single source
         of truth. True → bật pool. False/None/vắng → no-pool.
      3. Default ``False`` (no-pool) khi DB chưa mở / lỗi đọc.

    KHÔNG dùng env làm default bật/tắt (chỉ override). KHÔNG file config riêng.
    """
    # Override cứng qua env: tắt pool bất chấp setting.
    if pool_disabled():
        return False
    # Settings Store (DB) — opt-in. Đọc qua thread-local read connection
    # (WAL, check_same_thread=False) nên an toàn khi gọi từ to_thread worker.
    try:
        from db import get_engine, get_settings_repo

        return get_settings_repo(get_engine()).get("reg.hybrid_pool_enabled") is True
    except Exception:  # noqa: BLE001 — DB chưa mở (live test/CLI) → no-pool
        return False


# ─────────────────────────────────────────────────────────────────────
# No-pool path adapter — thread-affinity cho CamoufoxTokenGenerator
# ─────────────────────────────────────────────────────────────────────


class _NoPoolThreadAffinityWrapper:
    """Wrap ``chatgpt_camoufox.CamoufoxTokenGenerator`` để route qua dedicated
    thread (giải quyết bug Sync API in asyncio loop + thread-affinity ở fallback
    no-pool path).

    No-pool path (``HYBRID_POOL_DISABLED=1``) build ``CamoufoxTokenGenerator``
    trực tiếp. Nó cũng dùng ``camoufox.sync_api`` → gặp y hệt bug pool path
    (asyncio guard + thread-affinity giữa main relay thread vs pre-mint thread).
    Adapter này giữ nguyên interface (set_device_id / export_cookies / mint_token
    / mint_so / close) nhưng mọi call route qua ``_PlaywrightThread`` riêng.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._thread = _PlaywrightThread(name=f"camoufox-nopool-{id(self):x}")
        # Reaper: chốt baseline NGAY (chưa launch) → mọi browser process spawn
        # sau đây bởi inner (CamoufoxTokenGenerator) thuộc về wrapper này và là
        # ứng viên force-kill khi launch/op treo hoặc close không kill được.
        self._reaper = BrowserProcessReaper(
            getattr(inner, "_log", None) or logger.warning
        )
        self._reaper.mark_baseline()

    def _run_bounded(self, fn: Callable[..., Any], *args: Any,
                     timeout: float, op: str, kill_on_timeout: bool) -> Any:
        """Route op qua dedicated thread với BOUND timeout (fail-fast).

        Hết timeout → op vẫn kẹt trên daemon worker (không cancel được) →
        ``kill_on_timeout`` để reaper force-kill browser orphan, rồi raise
        ``HybridBrowserPoolError`` cho ``run_hybrid_signup`` classify.
        """
        try:
            return self._thread.run(fn, *args, timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            if kill_on_timeout:
                self._reaper.kill_new(
                    f"timeout:{op}", grace_seconds=_KILL_GRACE_SECONDS,
                )
            logger.warning(
                "no-pool camoufox op TIMEOUT (%s > %.0fs) — kill=%s",
                op, timeout, kill_on_timeout,
            )
            raise HybridBrowserPoolError(
                f"camoufox op timeout ({op} > {timeout:.0f}s)"
            ) from exc

    def set_device_id(self, device_id: str) -> None:
        # set_device_id TRIGGER lazy launch (_ensure_page → Camoufox launch).
        # Đây là khâu cold-launch nghi treo → bound LAUNCH timeout + kill orphan.
        return self._run_bounded(
            self._inner.set_device_id, device_id,
            timeout=_LAUNCH_TIMEOUT_SECONDS, op="set_device_id/launch",
            kill_on_timeout=True,
        )

    def export_cookies(self) -> list[dict]:
        return self._run_bounded(
            self._inner.export_cookies,
            timeout=_MINT_TIMEOUT_SECONDS, op="export_cookies",
            kill_on_timeout=False,
        )

    def mint_token(self, flow: str):
        return self._run_bounded(
            self._inner.mint_token, flow,
            timeout=_MINT_TIMEOUT_SECONDS, op="mint_token",
            kill_on_timeout=True,
        )

    def mint_so(self, flow: str) -> str:
        return self._run_bounded(
            self._inner.mint_so, flow,
            timeout=_MINT_TIMEOUT_SECONDS, op="mint_so",
            kill_on_timeout=True,
        )

    def close(self) -> None:
        timed_out = False
        try:
            self._thread.run(
                self._inner.close, timeout=_TEARDOWN_TIMEOUT_SECONDS,
            )
        except concurrent.futures.TimeoutError:
            timed_out = True
            logger.warning(
                "no-pool close TIMEOUT >%.0fs — sẽ force-kill browser",
                _TEARDOWN_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 — close best-effort
            logger.warning("no-pool close failed: %s", exc)
        finally:
            # Sweep: kill Firefox/Camoufox còn sống dù close treo HAY close
            # "thành công" nhưng process không thực sự chết (orphan tích lũy là
            # gốc bug multi-signup launch-hang). Chỉ chạm delta baseline.
            self._reaper.kill_new(
                "no-pool-close-timeout" if timed_out else "no-pool-close-sweep",
                grace_seconds=_KILL_GRACE_SECONDS,
            )
            self._thread.shutdown()
