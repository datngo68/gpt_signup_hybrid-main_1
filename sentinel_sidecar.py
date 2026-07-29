"""SentinelSidecar — sync wrapper around a Camoufox page used by pure-HTTP
``request_phase`` flow to produce real sentinel-token + so-token + JS cookies.

Architecture (Phase 11 — trusted events + persona rotation + sdk verify)
-----------------------------------------------------------------------

Phase 10.1 introduced a pool that shares one Camoufox process between many
signups (saves ~70% RAM). Phase 11 hardens the per-signup path further:

  1. **Trusted DOM events** for Sentinel Session Observer.
     ``page.evaluate`` with ``dispatchEvent`` sets ``event.isTrusted=false``
     (W3C requirement). Sentinel Observer is known to weight events by
     ``isTrusted`` — anti-bot literature shows synthesised events are
     ignored or marked. Switching to ``page.mouse``/``page.keyboard``
     drives events through CDP/Marionette → ``isTrusted=true`` on the
     page side. so-token output reflects real interaction signal.

  2. **Per-context persona rotation**.
     Sharing one Camoufox process means all contexts share the parent
     process WebGL/canvas/audio backend. Without isolation, every signup
     emits identical fingerprints — the textbook "fleet of bots betray
     themselves" pattern (Twitter/X anti-spam research; gigazine sentinel
     paper). Camoufox exposes per-context patch functions
     (setCanvasSeed, setAudioFingerprintSeed, setFontSpacingSeed,
     setWebGLVendor, setWebGLRenderer) — we seed them with per-signup
     random ints via ``context.add_init_script``. Browser process stays
     shared (RAM saving intact) but each context's canvas/audio/font hash
     is unique.

  3. **sdk.js patch verification at fetch time** (in ``sentinel_browser``)
     fails the page-native path fast if OpenAI rotates the sdk bundle;
     caller falls back to QuickJS rather than silently emitting empty
     so-tokens. See ``_verify_sdk_patch_markers``.

Architecture pool layout (unchanged from Phase 10.1):

    SentinelSidecarPool (process-singleton, keyed by proxy)
      └── _SharedBrowser (one Camoufox Browser, ~150MB, daemon thread + loop)
            ├── BrowserContext A (per-signup, ~30-50MB)
            │     • per-context persona seeds (canvas/audio/font/WebGL)
            │     • own cookie jar
            │     • own sdk.js storage
            ├── BrowserContext B (different seeds)
            └── ...

Lifecycle
---------
    sc = SentinelSidecar(proxy="...", headless=True, log=log)
    sc.start()                       # acquires context + seeds + warm-up
    cookies = sc.dump_cookies()
    token  = sc.get_sentinel_token(device_id=...)
    so_tok = sc.get_so_token(device_id=...)
    sc.close()                       # releases context; browser stays alive

All public methods are SYNC, safe from any worker thread. Not safe inside
an existing asyncio event loop (use the async oracle directly there).
"""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import random
import string
import threading
from typing import Any, Callable, ClassVar, Optional


logger = logging.getLogger(__name__)


# A small candidate pool of plausible WebGL renderer strings to rotate per
# context. Each entry is a real-world (vendor, renderer) seen in the wild
# on macOS Firefox (matches `os_target="macos"` default). Keeping them
# Mac-coherent prevents UA-says-Mac-but-GPU-says-Intel-HD-iGPU mismatch.
_WEBGL_RENDERERS_MAC: tuple[tuple[str, str], ...] = (
    ("Mozilla",
     "Mozilla -- ANGLE (Apple, Apple M1, OpenGL 4.1)"),
    ("Mozilla",
     "Mozilla -- ANGLE (Apple, Apple M1 Pro, OpenGL 4.1)"),
    ("Mozilla",
     "Mozilla -- ANGLE (Apple, Apple M2, OpenGL 4.1)"),
    ("Mozilla",
     "Mozilla -- ANGLE (Apple, Apple M2 Max, OpenGL 4.1)"),
    ("Mozilla",
     "Mozilla -- ANGLE (Apple, Apple M3 Pro, OpenGL 4.1)"),
    ("Mozilla",
     "Mozilla -- ANGLE (Intel Inc., Intel(R) Iris(TM) Plus Graphics 655 OpenGL Engine, OpenGL 4.1)"),
)


def _build_persona_init_script(seeds: dict) -> str:
    """Compose a Camoufox per-context init script that seeds canvas /
    audio / font-spacing / WebGL fingerprints. The functions self-destruct
    after first call (Camoufox guarantee) so even leaked init scripts
    can't be re-invoked later from page JS.
    """
    payload = json.dumps(seeds, ensure_ascii=False)
    return (
        "(() => {\n"
        f"  const v = {payload};\n"
        "  if (typeof window.setCanvasSeed === 'function') window.setCanvasSeed(v.canvas);\n"
        "  if (typeof window.setAudioFingerprintSeed === 'function') window.setAudioFingerprintSeed(v.audio);\n"
        "  if (typeof window.setFontSpacingSeed === 'function') window.setFontSpacingSeed(v.fontSpacing);\n"
        "  if (typeof window.setWebGLVendor === 'function' && v.webglVendor) window.setWebGLVendor(v.webglVendor);\n"
        "  if (typeof window.setWebGLRenderer === 'function' && v.webglRenderer) window.setWebGLRenderer(v.webglRenderer);\n"
        "})();\n"
    )


def _new_persona_seeds() -> dict:
    """Generate a fresh per-context persona seed bundle. Each signup gets
    independent random seeds so canvas/audio/font hashes never collide
    across our fleet (defeats "bots betray themselves" detection).
    """
    vendor, renderer = random.choice(_WEBGL_RENDERERS_MAC)
    return {
        "canvas":       random.randint(10**8, 10**9 - 1),
        "audio":        random.randint(10**8, 10**9 - 1),
        "fontSpacing":  random.randint(10**8, 10**9 - 1),
        "webglVendor":  vendor,
        "webglRenderer": renderer,
    }


async def _simulate_trusted_input(page: Any, log: Callable[[str], None]) -> None:
    """Drive a few real keystrokes through Playwright primitives so the
    Sentinel Session Observer records events with ``isTrusted=true``.

    Why not ``page.evaluate(dispatchEvent)``: W3C requires that
    ``Event.isTrusted`` be ``false`` for any event constructed by page
    script. Sentinel Observer is documented (in OpenAI bot-detection
    reverse-engineering writeups) to weight events by ``isTrusted``, so
    synthesised events flow into a near-empty so-token bucket.

    ``page.mouse.click`` and ``page.keyboard.type`` go through Playwright's
    CDP/Marionette bridge, which dispatches events from the browser's UI
    thread — same path as a real human click — so ``isTrusted=true``.
    """
    try:
        loc = page.locator(
            'input[name="code"], input[type="password"], '
            'input[name="username"], input[type="text"], input'
        ).first
        await loc.wait_for(state="visible", timeout=10000)
    except Exception as exc:
        log(f"[sidecar] no input visible for trusted simulation: {exc}")
        return

    # Move mouse to input, click via real CDP path.
    try:
        box = await loc.bounding_box()
    except Exception:
        box = None
    if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
        target_x = box["x"] + 5 + random.random() * max(1, box["width"] - 10)
        target_y = box["y"] + box["height"] / 2
        # Approach: 2 hops of mouse movement, slight pause, click.
        try:
            await page.mouse.move(
                target_x - 30 + random.uniform(-10, 10),
                target_y - 10 + random.uniform(-5, 5),
                steps=5,
            )
            await asyncio.sleep(0.06 + random.random() * 0.12)
            await page.mouse.move(target_x, target_y, steps=8)
            await asyncio.sleep(0.04 + random.random() * 0.08)
            await page.mouse.click(
                target_x, target_y, delay=random.randint(40, 110),
            )
        except Exception as exc:
            log(f"[sidecar] mouse simulate failed (falling back to locator.click): {exc}")
            try:
                await loc.click(timeout=5000)
            except Exception:
                pass
    else:
        try:
            await loc.click(timeout=5000)
        except Exception as exc:
            log(f"[sidecar] locator.click failed: {exc}")
            return

    # Type random chars through CDP keyboard. Each char raises trusted
    # keydown/keypress/keyup. Gaussian-ish inter-key delay 60-180ms with
    # an occasional longer pause to mimic a real typer thinking.
    alphabet = string.ascii_lowercase + string.digits
    n_chars = 8 + random.randint(0, 5)
    try:
        for i in range(n_chars):
            await page.keyboard.type(
                random.choice(alphabet),
                delay=random.randint(60, 160),
            )
            # Occasional thinking pause (~12%).
            if random.random() < 0.12:
                await asyncio.sleep(0.25 + random.random() * 0.4)
    except Exception as exc:
        log(f"[sidecar] keyboard.type failed (continue): {exc}")

    # Clear the value so we don't accidentally submit. Use locator.fill("")
    # which is a trusted operation.
    try:
        await loc.fill("")
    except Exception:
        pass
    # Blur to mark "user finished editing"
    try:
        await page.keyboard.press("Tab")
    except Exception:
        pass

    log(f"[sidecar] trusted input simulated ({n_chars} chars, isTrusted=true)")


# ─────────────────────────────────────────────────────────────────────
# _SharedBrowser — one Camoufox Browser, lives in a daemon thread
# ─────────────────────────────────────────────────────────────────────


class _SharedBrowser:
    """A single Camoufox Browser running in a private asyncio loop on a
    daemon thread. Hands out isolated ``BrowserContext`` instances on
    demand. Reference-counted so the pool can tear it down when idle.
    """

    def __init__(
        self,
        *,
        proxy: Optional[str],
        headless: bool,
        os_target: str,
        locale: str,
        log: Callable[[str], None],
    ) -> None:
        self._proxy = proxy
        self._headless = headless
        self._os_target = os_target
        self._locale = locale
        self._log = log

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._error: Optional[BaseException] = None

        self._cf_ctx_mgr: Any = None
        self._browser: Any = None

        self._ref_lock = threading.Lock()
        self._ref_count = 0

        # Idempotent start: pool.acquire releases its dict-lock BEFORE
        # calling start() so multiple threads can race here. Without
        # ``_start_lock`` two threads could both see ``_thread is None``
        # and spawn duplicate asyncio loops + Camoufox processes. Wasted
        # ~150MB RAM + the second loop's launch fails because Playwright
        # binds a port on the shared host.
        self._start_lock = threading.Lock()

    # ── Thread + loop bootstrap ─────────────────────────────────────

    def _thread_target(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._launch())
            self._ready.set()
            loop.run_forever()
        except BaseException as exc:
            self._error = exc
            self._ready.set()
        finally:
            try:
                loop.run_until_complete(self._shutdown())
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass

    async def _launch(self) -> None:
        from camoufox.async_api import AsyncCamoufox

        proxy_kwargs: dict = {}
        if self._proxy:
            from _browser_retry import parse_proxy_for_playwright as _parse_proxy
            proxy_kwargs["proxy"] = _parse_proxy(self._proxy)

        # persistent_context=False → AsyncCamoufox.__aenter__ returns a
        # Browser, not a BrowserContext. We need a Browser so we can call
        # ``new_context()`` per-signup for isolation.
        cf = AsyncCamoufox(
            headless=self._headless,
            persistent_context=False,
            os=[self._os_target],
            locale=[self._locale],
            geoip=bool(self._proxy),
            block_webrtc=True,
            humanize=True,
            # main_world_eval=True (Phase 11.2): cho phép ``page.evaluate``
            # chạy trong main world, KHÔNG qua Xray wrapper. Bắt buộc khi
            # sdk.js sentinel của OpenAI gọi crypto/TypedArray cross-context
            # — Firefox Xray refuse TypedArray serialize, dẫn đến lỗi
            # "Accessing TypedArray data over Xrays is slow, and forbidden".
            # Camoufox apply stealth patches để main world an toàn (mask
            # __playwright__ namespace).
            main_world_eval=True,
            # fingerprint_preset removed (Phase 11.1) — older Camoufox in this
            # venv forwards the kwarg to Playwright which rejects it. Per-context
            # persona seeds via ctx.add_init_script cover the anti-fleet need.
            **proxy_kwargs,
        )
        self._cf_ctx_mgr = cf
        self._browser = await cf.__aenter__()
        self._log(
            f"[sidecar-pool] shared browser launched "
            f"(proxy={'yes' if self._proxy else 'no'} headless={self._headless})"
        )

    async def _shutdown(self) -> None:
        cf = self._cf_ctx_mgr
        self._cf_ctx_mgr = None
        if cf is None:
            return
        try:
            await cf.__aexit__(None, None, None)
        except Exception as exc:
            self._log(f"[sidecar-pool] shutdown exception: {exc}")

    # ── Lifecycle ───────────────────────────────────────────────────

    def start(self, *, timeout: float = 60.0) -> None:
        # Idempotent: first caller spawns the thread; concurrent callers
        # wait on ``_ready`` (the launch finish signal) so they observe
        # the same outcome (success or error).
        with self._start_lock:
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._thread_target,
                    name="sentinel-sidecar-pool",
                    daemon=True,
                )
                self._thread.start()
        if not self._ready.wait(timeout=timeout):
            raise RuntimeError(
                f"_SharedBrowser startup timeout after {timeout}s"
            )
        if self._error is not None:
            raise RuntimeError(
                f"_SharedBrowser startup failed: {self._error!r}"
            ) from self._error

    def shutdown(self, *, timeout: float = 10.0) -> None:
        loop = self._loop
        thread = self._thread
        if loop is None or thread is None:
            return
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass
        thread.join(timeout=timeout)
        self._loop = None
        self._thread = None

    # ── Reference counting (pool uses this to drop browsers when idle) ─

    def add_ref(self) -> int:
        with self._ref_lock:
            self._ref_count += 1
            return self._ref_count

    def release_ref(self) -> int:
        with self._ref_lock:
            self._ref_count = max(0, self._ref_count - 1)
            return self._ref_count

    # ── Public API used by SentinelSidecar ──────────────────────────

    def run_in_loop(self, coro, *, timeout: float) -> Any:
        loop = self._loop
        if loop is None:
            raise RuntimeError("_SharedBrowser not started")
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=timeout)

    def acquire_context(
        self,
        *,
        log: Callable[[str], None],
        timeout: float = 60.0,
    ) -> tuple[Any, Any]:
        """Create a new isolated ``BrowserContext`` + load chatgpt.com +
        /email-verification + simulate trusted input. Returns
        ``(context, page)`` ready for sdk.js queries.

        Each context gets its own persona seeds (canvas/audio/font/WebGL)
        so concurrent signups in the same pool don't share fingerprints
        (anti-fleet detection).
        """
        async def _create() -> tuple[Any, Any]:
            ctx = await self._browser.new_context()

            # Per-context persona rotation — Camoufox patch functions get
            # fresh canvas/audio/font seeds + WebGL renderer per signup.
            # Inject BEFORE any page navigation so the very first request
            # already carries the rotated fingerprint.
            seeds = _new_persona_seeds()
            try:
                await ctx.add_init_script(_build_persona_init_script(seeds))
                log(
                    f"[sidecar] persona seeds rotated "
                    f"(canvas={seeds['canvas']} audio={seeds['audio']} "
                    f"webgl={seeds['webglRenderer'][:50]!r})"
                )
            except Exception as exc:
                log(f"[sidecar] persona add_init_script failed (continue): {exc}")

            # Phase 11.3 — install patched sdk.js into MAIN realm via
            # ``add_init_script``. Must happen BEFORE any ``new_page()`` so
            # the very first document load has ``__runSentinelInPage``
            # available in main realm — page.evaluate later only needs to
            # *call* it (cross-realm function invocation is safe; sdk.js
            # internals run in main realm where TypedArrays were created).
            try:
                from sentinel_browser import fetch_sdk_and_build_install_script
                install_js = await fetch_sdk_and_build_install_script(
                    ctx.request, log=log,
                )
                await ctx.add_init_script(install_js)
                log("[sidecar] sentinel sdk.js installed in main realm (add_init_script)")
            except Exception as exc:
                log(
                    f"[sidecar] sdk.js install_script failed (will retry "
                    f"via <script> tag at first oracle call): {exc}"
                )

            page = await ctx.new_page()

            await page.goto(
                "https://chatgpt.com/", wait_until="domcontentloaded",
            )
            log("[sidecar] chatgpt.com loaded")

            try:
                from sentinel_browser import verify_fingerprint_health as _vfp
                await _vfp(page, log=log)
            except Exception as exc:  # noqa: BLE001
                log(f"[sidecar] fingerprint probe exception: {exc}")

            try:
                await page.goto(
                    "https://auth.openai.com/email-verification",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                log("[sidecar] /email-verification loaded")
            except Exception as exc:
                log(f"[sidecar] /email-verification visit failed: {exc}")

            # Phase 11: trusted DOM events via Playwright primitives.
            # Replaces the page.evaluate(dispatchEvent) approach which
            # generates isTrusted=false events that Sentinel Observer
            # weights down (or ignores entirely).
            try:
                await _simulate_trusted_input(page, log)
            except Exception as exc:
                log(f"[sidecar] trusted input simulation failed: {exc}")

            return ctx, page

        return self.run_in_loop(_create(), timeout=timeout)

    def release_context(self, ctx: Any, *, timeout: float = 10.0) -> None:
        async def _close() -> None:
            try:
                await ctx.close()
            except Exception as exc:  # noqa: BLE001
                self._log(f"[sidecar] context.close exception: {exc}")
        try:
            self.run_in_loop(_close(), timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            self._log(f"[sidecar] release_context failed: {exc}")


# ─────────────────────────────────────────────────────────────────────
# SentinelSidecarPool — process singleton, keyed by (proxy, headless)
# ─────────────────────────────────────────────────────────────────────


class SentinelSidecarPool:
    """Process-wide pool of ``_SharedBrowser`` instances. Sidecars sharing
    the same (proxy, headless) tuple reuse the same Firefox parent process.
    """

    _instance: ClassVar[Optional["SentinelSidecarPool"]] = None
    _instance_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self) -> None:
        self._browsers: dict[str, _SharedBrowser] = {}
        self._lock = threading.Lock()
        # Idle browser TTL: if a browser ref-count drops to 0, we keep it
        # alive for this many seconds before tearing down, so the next
        # signup in the same batch can reuse it without paying startup cost.
        self._idle_ttl_seconds = 60.0
        self._idle_timers: dict[str, threading.Timer] = {}

    @classmethod
    def instance(cls) -> "SentinelSidecarPool":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
                atexit.register(cls._instance.shutdown_all)
            return cls._instance

    def _key(self, *, proxy: Optional[str], headless: bool, os_target: str) -> str:
        return f"{proxy or ''}|{'h' if headless else 'H'}|{os_target}"

    def acquire(
        self,
        *,
        proxy: Optional[str],
        headless: bool,
        os_target: str,
        locale: str,
        log: Callable[[str], None],
        timeout: float = 60.0,
    ) -> _SharedBrowser:
        key = self._key(proxy=proxy, headless=headless, os_target=os_target)
        with self._lock:
            # Cancel pending idle shutdown if any
            timer = self._idle_timers.pop(key, None)
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:
                    pass

            br = self._browsers.get(key)
            if br is None:
                br = _SharedBrowser(
                    proxy=proxy, headless=headless,
                    os_target=os_target, locale=locale, log=log,
                )
                self._browsers[key] = br

        # Start outside the lock — startup can take ~10s.
        try:
            br.start(timeout=timeout)
        except BaseException:
            # Failed startup — remove the broken entry so next call retries.
            with self._lock:
                if self._browsers.get(key) is br:
                    self._browsers.pop(key, None)
            raise
        br.add_ref()
        return br

    def release(
        self,
        *,
        proxy: Optional[str],
        headless: bool,
        os_target: str,
    ) -> None:
        key = self._key(proxy=proxy, headless=headless, os_target=os_target)
        with self._lock:
            br = self._browsers.get(key)
            if br is None:
                return
            remaining = br.release_ref()
            if remaining > 0:
                return
            # Idle — schedule teardown after TTL so subsequent signups can
            # reuse the browser without paying ~10s relaunch.
            existing_timer = self._idle_timers.pop(key, None)
            if existing_timer is not None:
                try:
                    existing_timer.cancel()
                except Exception:
                    pass

            def _on_idle_timeout(key=key) -> None:
                with self._lock:
                    br2 = self._browsers.get(key)
                    if br2 is None:
                        return
                    if br2._ref_count > 0:
                        # Someone reused it during the idle window — keep alive.
                        return
                    self._browsers.pop(key, None)
                    self._idle_timers.pop(key, None)
                try:
                    br2.shutdown()
                except Exception:
                    pass

            timer = threading.Timer(self._idle_ttl_seconds, _on_idle_timeout)
            timer.daemon = True
            self._idle_timers[key] = timer
            timer.start()

    def shutdown_all(self) -> None:
        with self._lock:
            browsers = list(self._browsers.values())
            self._browsers.clear()
            for t in self._idle_timers.values():
                try:
                    t.cancel()
                except Exception:
                    pass
            self._idle_timers.clear()
        for br in browsers:
            try:
                br.shutdown()
            except Exception:
                pass

    # ── Diagnostics ─────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        """Snapshot for monitoring/logging. Safe to call any time."""
        with self._lock:
            return {
                "browsers": len(self._browsers),
                "keys": list(self._browsers.keys()),
                "ref_counts": {
                    k: br._ref_count for k, br in self._browsers.items()
                },
                "idle_timers": list(self._idle_timers.keys()),
            }


# ─────────────────────────────────────────────────────────────────────
# SentinelSidecar — per-signup facade
# ─────────────────────────────────────────────────────────────────────


class SentinelSidecar:
    """Per-signup wrapper. Owns 1 isolated ``BrowserContext`` borrowed
    from the shared pool. Public methods are sync; safe from any worker
    thread. Not reentrant (one signup → one sidecar instance).
    """

    def __init__(
        self,
        *,
        proxy: Optional[str] = None,
        headless: bool = True,
        locale: Optional[str] = None,
        log: Optional[Callable[[str], None]] = None,
        os_target: str = "macos",
    ) -> None:
        self._proxy = proxy
        self._headless = headless
        self._locale = locale or "en-US"
        self._os_target = os_target
        self._log = log or (lambda m: logger.info(m))

        self._browser: Optional[_SharedBrowser] = None
        self._ctx: Any = None
        self._page: Any = None
        self._oracle: Any = None

    # ── Public accessors ───────────────────────────────────────────

    @property
    def proxy(self) -> Optional[str]:
        """Upstream proxy URL the sidecar's Camoufox was launched with
        (None = direct). Caller needs this to decide whether IP-bound
        cookies (``__cf_bm`` etc.) can be safely synced into its own
        jar — if its proxy differs, those cookies are bound to a
        different IP and syncing them defeats Cloudflare's bot check.
        """
        return self._proxy

    # ── Lifecycle ──────────────────────────────────────────────────

    def start(self, *, timeout: float = 60.0) -> None:
        """Acquire shared browser from pool + create isolated context.
        Blocks until the page is ready (chatgpt.com loaded + form
        simulation done). Raises on failure.
        """
        if self._browser is not None:
            return
        pool = SentinelSidecarPool.instance()
        br = pool.acquire(
            proxy=self._proxy,
            headless=self._headless,
            os_target=self._os_target,
            locale=self._locale,
            log=self._log,
            timeout=timeout,
        )
        try:
            ctx, page = br.acquire_context(log=self._log, timeout=timeout)
        except BaseException:
            pool.release(
                proxy=self._proxy, headless=self._headless,
                os_target=self._os_target,
            )
            raise
        self._browser = br
        self._ctx = ctx
        self._page = page
        # SentinelBrowserOracle keeps a reference to page+ctx for evaluate
        # + ctx.request.post. Construct fresh per signup so its internal
        # cache (sdk.js text, in-page script) is private.
        from sentinel_browser import SentinelBrowserOracle
        self._oracle = SentinelBrowserOracle(page=page, ctx=ctx, log=self._log)

    def close(self, *, timeout: float = 10.0) -> None:
        """Release the borrowed context back to the pool. Browser stays
        alive in the pool for the next signup (subject to idle TTL).
        """
        br = self._browser
        ctx = self._ctx
        self._browser = None
        self._ctx = None
        self._page = None
        self._oracle = None
        if br is None:
            return
        if ctx is not None:
            try:
                br.release_context(ctx, timeout=timeout)
            except Exception as exc:
                self._log(f"[sidecar] release_context exception: {exc}")
        try:
            SentinelSidecarPool.instance().release(
                proxy=self._proxy, headless=self._headless,
                os_target=self._os_target,
            )
        except Exception as exc:
            self._log(f"[sidecar] pool.release exception: {exc}")

    # ── Sync public API ────────────────────────────────────────────

    def get_sentinel_token(
        self,
        *,
        device_id: str,
        flow: str = "username_password_create",
        timeout: float = 30.0,
    ) -> Optional[str]:
        """Real sentinel-token via page-native sdk.js. None on failure."""
        if self._oracle is None or self._browser is None:
            return None
        return self._browser.run_in_loop(
            self._oracle.get_token(device_id=device_id, flow=flow),
            timeout=timeout,
        )

    # ── K2 — Real form-submit interception ──────────────────────────

    def intercept_register_token(
        self,
        *,
        email: str,
        device_id: str,
        logging_id: str,
        timeout: float = 90.0,
    ) -> Optional[dict]:
        """**K2 path** — real-form intercept for ``openai-sentinel-token``.

        Why: ``get_sentinel_token`` via ``page.evaluate(sdk)`` triggers
        sdk.js's internal fail path (``buildGenerateFailMessage``) which
        accesses TypedArray cross-realm and trips Firefox Xray. The only
        way to make sdk.js fire its CORRECT code path is via a real
        user-initiated form submission.

        Architecture:
            1. Sidecar navigates its own auth flow: chatgpt.com →
               authorize URL → /email-verification → click "Continue
               with password" → /create-account/password.
            2. Sidecar fills a DUMMY password.
            3. ``page.route`` matches POST ``/api/accounts/user/register``,
               captures all request headers (including
               ``openai-sentinel-token``), then ABORTS the request
               (server never sees this submission — sidecar's auth state
               stays clean for next call).
            4. Returns ``{sentinel_token, cookies}`` for the curl_cffi
               caller to use in its REAL ``/register`` request.

        ``sentinel-token`` content does NOT hash the request body, so
        re-using sidecar's token for caller's real password POST is safe
        (server validates token against session state, not body bytes).

        Returns ``None`` if the form flow fails or the route never fires.
        Caller falls back to QuickJS in that case.
        """
        if self._browser is None:
            return None

        async def _do_intercept() -> Optional[dict]:
            from _nextauth_bootstrap import bootstrap_authorize_url
            from _human_input import human_type as _ht

            page = self._page

            captured: dict = {}
            event = asyncio.Event()
            # Audit: track ANY /register POST that finishes (not aborted).
            # If non-empty after K2 returns, our route.abort() failed and
            # the sidecar's DUMMY password reached the server — server
            # would store the dummy and break login forever. We DROP the
            # captured token in that case so caller falls back to QuickJS
            # (caller's own /register POST will then create the account
            # with the REAL password).
            leaked_register_requests: list[str] = []
            # Diagnostic: catch ALL POSTs to spot the SPA's actual endpoint.
            all_post_urls: list[str] = []

            def _on_request_finished(request) -> None:
                try:
                    url = request.url
                    method = request.method
                except Exception:
                    return
                if method == "POST" and (
                    "/api/" in url
                    or "auth.openai.com" in url
                ):
                    all_post_urls.append(url)
                if (
                    "/api/accounts/user/register" in url
                    and method == "POST"
                ):
                    leaked_register_requests.append(url)

            async def _route(route, request):
                if (
                    "/api/accounts/user/register" in request.url
                    and request.method == "POST"
                ):
                    try:
                        hdrs = await request.all_headers()
                    except Exception:
                        hdrs = dict(request.headers)
                    captured["sentinel_token"] = hdrs.get("openai-sentinel-token")
                    captured["so_token"] = hdrs.get("openai-sentinel-so-token")
                    captured["device_id"] = hdrs.get("oai-device-id")
                    captured["body"] = request.post_data
                    event.set()
                    try:
                        await route.abort()
                        captured["_abort_ok"] = True
                    except Exception as exc:
                        captured["_abort_ok"] = False
                        captured["_abort_err"] = repr(exc)
                else:
                    try:
                        await route.continue_()
                    except Exception:
                        pass

            try:
                await page.route("**/api/accounts/user/register", _route)
            except Exception as exc:
                self._log(f"[sidecar.K2] route registration failed: {exc}")
                return None
            try:
                page.on("requestfinished", _on_request_finished)
            except Exception:
                pass

            # ── DEFENSE 0: JS-level fetch + XHR interceptor ────────
            # Camoufox's ``route.abort()`` is empirically leaky for
            # auth.openai.com (verified via K2c HTTP 400 "user already
            # exists"). Install a JS-realm fetch + XHR override BEFORE
            # navigation so the request never leaves the renderer.
            # Captured headers expose ``openai-sentinel-token`` for the
            # caller's real /register POST.
            try:
                ctx = self._ctx
                await ctx.add_init_script(
                    """
() => {
    if (window.__K2_INSTALLED) return;
    window.__K2_INSTALLED = true;
    window.__capturedK2Headers = null;
    window.__capturedK2Body = null;

    const isReg = (url) => {
        try {
            return String(url || '').indexOf('/api/accounts/user/register') >= 0;
        } catch (e) { return false; }
    };

    const origFetch = window.fetch;
    window.fetch = function(input, init) {
        try {
            const url = (typeof input === 'string') ? input : (input && input.url);
            const method = (init && init.method) || (input && input.method) || 'GET';
            if (isReg(url) && String(method).toUpperCase() === 'POST') {
                let hdrs = {};
                if (init && init.headers) {
                    const h = init.headers;
                    if (typeof h.forEach === 'function') {
                        h.forEach((v, k) => { hdrs[String(k).toLowerCase()] = String(v); });
                    } else {
                        for (const k of Object.keys(h)) {
                            hdrs[String(k).toLowerCase()] = String(h[k]);
                        }
                    }
                }
                window.__capturedK2Headers = hdrs;
                window.__capturedK2Body = (init && init.body) ? String(init.body) : null;
                return Promise.reject(new TypeError('K2 intercepted'));
            }
        } catch (e) {}
        return origFetch.apply(this, arguments);
    };

    const origOpen = XMLHttpRequest.prototype.open;
    const origSend = XMLHttpRequest.prototype.send;
    const origSetHeader = XMLHttpRequest.prototype.setRequestHeader;
    XMLHttpRequest.prototype.open = function(method, url) {
        this.__k2_method = String(method || '').toUpperCase();
        this.__k2_url = String(url || '');
        this.__k2_headers = {};
        return origOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
        if (this.__k2_headers) {
            this.__k2_headers[String(name).toLowerCase()] = String(value);
        }
        return origSetHeader.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function(body) {
        if (isReg(this.__k2_url) && this.__k2_method === 'POST') {
            window.__capturedK2Headers = this.__k2_headers;
            window.__capturedK2Body = body ? String(body) : null;
            try { this.abort(); } catch (e) {}
            try { this.dispatchEvent(new Event('error')); } catch (e) {}
            return;
        }
        return origSend.apply(this, arguments);
    };
}
""".strip()
                )
                self._log("[sidecar.K2] fetch+XHR interceptor installed")
            except Exception as exc:
                self._log(
                    f"[sidecar.K2] interceptor install failed: {exc} "
                    f"— relying on page.route only (LEAK RISK)"
                )

            try:
                # Drive same flow as browser_phase password_create branch.
                await page.goto(
                    "https://chatgpt.com/", wait_until="domcontentloaded",
                )
                authorize_url = await bootstrap_authorize_url(
                    page, email=email, device_id=device_id,
                    logging_id=logging_id,
                )
                self._log(
                    f"[sidecar.K2] authorize URL ready "
                    f"({authorize_url[:80]}...)"
                )
                await page.goto(
                    authorize_url, wait_until="domcontentloaded", timeout=30000,
                )
                self._log(f"[sidecar.K2] post-authorize url={page.url[:100]!r}")

                # Mirror browser_phase state machine: detect screen, click
                # password button, retry. Use ``_PWD_BTN_SELECTOR`` semantics.
                #
                # NOTE: the server sometimes redirects authorize →
                # /create-account/password DIRECTLY (skipping
                # /email-verification) when ``login_hint`` matches a fresh
                # signup flow. In that case, the "Continue with password"
                # button doesn't exist on the page — just skip the click.
                pwd_btn_selector = (
                    'button:has-text("password"), a:has-text("password"), '
                    '[role="button"]:has-text("password")'
                )
                clicked_pwd_btn = False
                already_on_pwd_create = (
                    "/create-account/password" in (page.url or "")
                    or "/log-in/password" in (page.url or "")
                )
                if already_on_pwd_create:
                    self._log(
                        "[sidecar.K2] skipping password-button click — "
                        "already on password page (server auto-redirect)"
                    )
                    clicked_pwd_btn = True
                else:
                    # Up to 8s waiting for the "Continue with password" button
                    # — SPA can take ~3-5s to fully render on slower runners.
                    for attempt in range(8):
                        # Server may auto-navigate during our wait — re-check
                        # URL each loop and bail out if already on pwd page.
                        if (
                            "/create-account/password" in (page.url or "")
                            or "/log-in/password" in (page.url or "")
                        ):
                            self._log(
                                f"[sidecar.K2] page auto-navigated to "
                                f"{page.url[:80]!r} during wait — skipping click"
                            )
                            clicked_pwd_btn = True
                            break
                        try:
                            pwd_btn = page.locator(pwd_btn_selector).first
                            if await pwd_btn.is_visible(timeout=1000):
                                try:
                                    btn_text = await pwd_btn.text_content(timeout=500)
                                except Exception:
                                    btn_text = ""
                                await pwd_btn.click(timeout=3000)
                                self._log(
                                    f"[sidecar.K2] clicked password button: "
                                    f"{(btn_text or '').strip()[:60]}"
                                )
                                clicked_pwd_btn = True
                                break
                        except Exception:
                            pass
                        await asyncio.sleep(0.5)
                if not clicked_pwd_btn:
                    self._log(
                        f"[sidecar.K2] 'Continue with password' button never "
                        f"visible after 8s — url={page.url[:120]!r}; aborting K2"
                    )
                    return None

                # If we clicked the button (vs. server auto-redirect), wait
                # for the SPA route to settle on the password page. The click
                # triggers a client-side navigation that takes a few seconds.
                if not already_on_pwd_create:
                    try:
                        await page.wait_for_url(
                            lambda u: (
                                "/create-account/password" in (u or "")
                                or "/log-in/password" in (u or "")
                            ),
                            timeout=15000,
                        )
                        self._log(
                            f"[sidecar.K2] SPA navigation settled: "
                            f"{page.url[:100]!r}"
                        )
                    except Exception as exc:
                        self._log(
                            f"[sidecar.K2] wait_for_url after click failed: "
                            f"{exc} (current url={page.url[:100]!r})"
                        )
                        return None

                # Wait for password input on /create-account/password
                pwd_input = None
                pwd_selector_used = None
                for idx, sel in enumerate((
                    'input[type="password"]',
                    'input[name="password"]',
                    'input[autocomplete*="password"]',
                )):
                    try:
                        loc = page.locator(sel).first
                        sel_timeout = 15000 if idx == 0 else 2000
                        if await loc.is_visible(timeout=sel_timeout):
                            pwd_input = loc
                            pwd_selector_used = sel
                            break
                    except Exception:
                        continue
                if pwd_input is None:
                    self._log(
                        f"[sidecar.K2] no password input on "
                        f"{page.url[:100]!r} after click"
                    )
                    return None
                self._log(
                    f"[sidecar.K2] password input detected: {pwd_selector_used}"
                )

                # Fill dummy password — different from caller's real one
                dummy = "K2_T0kenC@ptUre_2026"
                await _ht(
                    pwd_input, dummy, delay_min_ms=60, delay_max_ms=140,
                    log=self._log,
                )
                # Brief dwell — let validators react & sentinel observer
                # complete its activity check (button enable conditioned on
                # password strength + observer state).
                await asyncio.sleep(0.6)

                # ── DEFENSE 0b: re-install fetch/XHR override via
                # page.evaluate just before submit. The init_script
                # version runs at document_start but the SPA can capture
                # ``window.fetch`` into a module-scoped variable on
                # import and call THAT reference, bypassing later
                # overrides. Re-installing here uses
                # ``Object.defineProperty`` to make the override
                # non-writable, and also wraps any ALREADY-cached
                # references the SPA may hold (we can't see them, but
                # the form's onsubmit handler typically calls window.fetch
                # at runtime, not a cached ref).
                try:
                    await page.evaluate(
                        """
() => {
    const isReg = (url) => {
        try {
            return String(url || '').indexOf('/api/accounts/user/register') >= 0;
        } catch (e) { return false; }
    };
    // Snapshot existing fetch + XHR (might already be our override)
    const realFetch = window.__K2_realFetch || window.fetch;
    window.__K2_realFetch = realFetch;
    const newFetch = function(input, init) {
        try {
            const url = (typeof input === 'string') ? input : (input && input.url);
            const method = (init && init.method) || (input && input.method) || 'GET';
            if (isReg(url) && String(method).toUpperCase() === 'POST') {
                let hdrs = {};
                if (init && init.headers) {
                    const h = init.headers;
                    if (typeof h.forEach === 'function') {
                        h.forEach((v, k) => { hdrs[String(k).toLowerCase()] = String(v); });
                    } else {
                        for (const k of Object.keys(h)) {
                            hdrs[String(k).toLowerCase()] = String(h[k]);
                        }
                    }
                }
                window.__capturedK2Headers = hdrs;
                window.__capturedK2Body = (init && init.body) ? String(init.body) : null;
                return Promise.reject(new TypeError('K2 intercepted (forced)'));
            }
        } catch (e) {}
        return realFetch.apply(this, arguments);
    };
    // Non-writable override so SPA can't restore.
    try {
        Object.defineProperty(window, 'fetch', {
            value: newFetch,
            writable: false,
            configurable: true,
        });
    } catch (e) {
        window.fetch = newFetch;
    }
    // Also override XHR.send for forms that use XHR.
    const proto = XMLHttpRequest.prototype;
    if (!proto.__K2_xhrPatched) {
        const origOpen = proto.open;
        const origSend = proto.send;
        const origSetHeader = proto.setRequestHeader;
        proto.open = function(method, url) {
            this.__k2_method = String(method || '').toUpperCase();
            this.__k2_url = String(url || '');
            this.__k2_headers = {};
            return origOpen.apply(this, arguments);
        };
        proto.setRequestHeader = function(name, value) {
            if (this.__k2_headers) {
                this.__k2_headers[String(name).toLowerCase()] = String(value);
            }
            return origSetHeader.apply(this, arguments);
        };
        proto.send = function(body) {
            if (isReg(this.__k2_url) && this.__k2_method === 'POST') {
                window.__capturedK2Headers = this.__k2_headers;
                window.__capturedK2Body = body ? String(body) : null;
                try { this.abort(); } catch (e) {}
                try { this.dispatchEvent(new Event('error')); } catch (e) {}
                return;
            }
            return origSend.apply(this, arguments);
        };
        proto.__K2_xhrPatched = true;
    }
    return true;
}
""".strip()
                    )
                    self._log("[sidecar.K2] force-installed interceptor pre-submit")
                except Exception as exc:
                    self._log(
                        f"[sidecar.K2] force-install pre-submit failed: {exc}"
                    )

                # Submit (mirror browser_phase: wait for enabled, then click;
                # fallback to Enter key). The button can be DISABLED until
                # sdk.js observer has scored enough activity.
                clicked_submit = False
                for btn_sel in (
                    'button[type="submit"]',
                    'button:has-text("Continue")',
                    'button:has-text("Sign up")',
                ):
                    try:
                        btn = page.locator(btn_sel).first
                        if (
                            await btn.is_visible(timeout=1500)
                            and await btn.is_enabled(timeout=2500)
                        ):
                            await btn.click(timeout=3000)
                            clicked_submit = True
                            self._log(f"[sidecar.K2] clicked submit: {btn_sel}")
                            break
                    except Exception:
                        continue
                if not clicked_submit:
                    self._log(
                        "[sidecar.K2] no enabled submit button — fallback Enter key"
                    )
                    try:
                        await pwd_input.press("Enter")
                        clicked_submit = True
                    except Exception as exc:
                        self._log(f"[sidecar.K2] Enter fallback failed: {exc}")
                if not clicked_submit:
                    self._log("[sidecar.K2] could not submit form — aborting")
                    return None
                # Wait for capture. PRIMARY signal: JS-level
                # ``window.__capturedK2Headers`` populated by the
                # fetch/XHR interceptor (DEFENSE 0 — leak impossible
                # because the request never leaves the renderer).
                # SECONDARY: ``event.set()`` from the page.route handler
                # (kept as a backstop only).
                js_headers: dict | None = None
                deadline = max(15.0, timeout - 30)
                step_t = 0.0
                while step_t < deadline:
                    try:
                        js_headers = await page.evaluate(
                            "() => window.__capturedK2Headers || null"
                        )
                    except Exception:
                        js_headers = None
                    if js_headers:
                        break
                    if event.is_set():
                        break
                    await asyncio.sleep(0.3)
                    step_t += 0.3

                if js_headers and isinstance(js_headers, dict):
                    # JS interceptor caught the POST — bytes never left
                    # the renderer. Headers are authoritative.
                    captured["sentinel_token"] = js_headers.get(
                        "openai-sentinel-token"
                    )
                    captured["so_token"] = js_headers.get(
                        "openai-sentinel-so-token"
                    )
                    captured["device_id"] = js_headers.get("oai-device-id")
                    captured["_via"] = "js-fetch"
                    self._log(
                        "[sidecar.K2] tokens captured via JS interceptor"
                    )
                elif not event.is_set():
                    self._log(
                        f"[sidecar.K2] neither JS interceptor nor "
                        f"page.route fired in {deadline:.0f}s — "
                        f"url={page.url[:100]!r}"
                    )
                    return None
                else:
                    captured["_via"] = "page.route"
                    self._log(
                        "[sidecar.K2] tokens via page.route (LEAK RISK — "
                        "JS interceptor missed the request)"
                    )

                # ── DEFENSE 1: navigate away immediately ────────────
                # The SPA on /create-account/password may retry the form
                # submit on transient failures. Our route handler is
                # still registered (until ``finally`` below unrouts it),
                # but if a retry slips through any timing crack, we want
                # to make sure no second /register POST fires. Navigating
                # to about:blank destroys the form before any retry can
                # run.
                try:
                    await page.goto("about:blank", timeout=5000)
                except Exception:
                    pass

                # ── DEFENSE 2: verify abort actually succeeded ──────
                # If our route.abort() raised (vd unsupported by this
                # Camoufox build) AND the JS interceptor also missed
                # (we fell back to page.route), the request reached the
                # server with DUMMY password. DROP the captured token.
                if captured.get("_via") == "page.route" and captured.get("_abort_ok") is False:
                    self._log(
                        f"[sidecar.K2] route.abort() FAILED "
                        f"({captured.get('_abort_err')!r}) — DROPPING "
                        f"captured token to prevent dummy-password leak. "
                        f"Caller will fall back to QuickJS."
                    )
                    return None
            finally:
                try:
                    page.remove_listener(
                        "requestfinished", _on_request_finished,
                    )
                except Exception:
                    pass
                try:
                    await page.unroute("**/api/accounts/user/register", _route)
                except Exception:
                    pass

            if not captured.get("sentinel_token"):
                self._log("[sidecar.K2] sentinel-token not in captured headers")
                return None

            # ── DEFENSE 3: leaked-request audit ────────────────────
            # ``requestfinished`` fires for requests that completed the
            # full round-trip (response received). If any /register POST
            # finished, the server got our DUMMY password — we MUST NOT
            # use the token (account is now bound to dummy, login will
            # fail). Note: aborted requests fire ``requestfailed``, not
            # ``requestfinished`` — so a non-empty list here means leak.
            if leaked_register_requests:
                self._log(
                    f"[sidecar.K2] LEAK DETECTED: {len(leaked_register_requests)} "
                    f"/register POST(s) reached the server (route.abort() "
                    f"did not stop them). Account may be created with "
                    f"DUMMY password. DROPPING captured token; caller "
                    f"falls back to QuickJS so its real /register POST "
                    f"creates the account with the REAL password."
                )
                return None

            self._log(
                f"[sidecar.K2] captured sentinel-token "
                f"(len={len(captured['sentinel_token'])} "
                f"so_token={'yes' if captured.get('so_token') else 'no'} "
                f"abort_ok={captured.get('_abort_ok')})"
            )
            if all_post_urls:
                from urllib.parse import urlparse as _urlparse
                paths = []
                for u in all_post_urls:
                    try:
                        paths.append(_urlparse(u).path or u[:60])
                    except Exception:
                        paths.append(u[:60])
                self._log(
                    f"[sidecar.K2] POSTs observed during K2: "
                    f"{sorted(set(paths))!r}"
                )
            return {
                "sentinel_token": captured["sentinel_token"],
                "so_token": captured.get("so_token"),
                "device_id": captured.get("device_id") or device_id,
            }

        try:
            return self._browser.run_in_loop(_do_intercept(), timeout=timeout)
        except Exception as exc:
            self._log(f"[sidecar.K2] intercept_register_token failed: {exc}")
            return None

    # ── K2c — /create_account form-submit interception ─────────────

    def intercept_create_account_token(
        self,
        *,
        device_id: str,
        name: str,
        birthdate: str,
        caller_cookies: list[dict] | None = None,
        timeout: float = 90.0,
    ) -> Optional[dict]:
        """Drive the sidecar through a FAKE ``/about-you`` form submit and
        capture the ``openai-sentinel-token`` + ``openai-sentinel-so-token``
        headers from the outgoing ``POST /api/accounts/create_account``.
        The request is then aborted (route.abort) so the server never sees
        the dummy birthdate; the caller (curl_cffi) reuses both tokens for
        the REAL create_account POST with the user's actual name/age.

        Why this is needed
        ──────────────────
        ``SentinelSDK.token()`` invoked via ``page.evaluate`` hits the
        Firefox Xray membrane in ``buildGenerateFailMessage`` (TypedArray
        cross-realm). The same call from the page's own form-submit handler
        runs in-realm and produces a valid token bundle WITH ``so`` field
        populated (Observer activity from human-like typing + click is what
        makes ``so`` non-null). Phase 11.4's K2 fixed this for ``/register``;
        K2c extends the pattern to ``/create_account``.

        Cookie sync
        ───────────
        ``/about-you`` requires an authenticated session — the user must
        have completed ``/register`` and ``/email-otp/validate``. The
        sidecar's BrowserContext doesn't have that state (its own
        ``/register`` attempt was aborted by K2). Caller hands us its
        post-OTP cookies (``__Secure-next-auth.session-token``,
        ``login_session``, ``oai-asli``, ``oai-sc``, ``oai-did``, ...)
        which we inject via ``ctx.add_cookies`` before navigating.

        Parameters
        ──────────
        device_id     : caller's device_id (used by sdk.js when generating
                        the token; must match the ``oai-device-id`` header
                        the caller will send on the real POST).
        name          : DUMMY name to type (server never sees it — aborted).
        birthdate     : DUMMY birthdate "YYYY-MM-DD".
        caller_cookies: curl_cffi cookies as dicts with name/value/domain/path.
        timeout       : overall budget for the K2c flow.

        Returns
        ───────
        ``{sentinel_token, so_token, device_id, body}`` on success, else None.
        """
        if self._browser is None or self._ctx is None:
            return None

        async def _do_intercept() -> Optional[dict]:
            from _human_input import (
                human_type as _ht,
                resolve_birth_field_value as _resolve_birth,
            )
            import random as _r

            page = self._page
            ctx = self._ctx

            # ── Sync caller cookies into the sidecar context ─────────
            # Playwright ``add_cookies`` updates existing cookies that
            # match (name, domain, path); new ones are appended. So we
            # don't need to clear first — caller's auth state simply
            # overrides any leftover sidecar state (e.g. ``oai-asli``
            # from K2's aborted register flow). Cloudflare cookies
            # (``__cf_bm``, ``cf_clearance``) keep their sidecar values
            # because caller's are usually the same anyway (same IP).
            if caller_cookies:
                pw_cookies = []
                for c in caller_cookies:
                    cname = (c.get("name") or "").strip()
                    cval = c.get("value") or ""
                    cdom = (c.get("domain") or "").strip()
                    if not cname or not cval or not cdom:
                        continue
                    # Playwright requires the domain to start with "." for
                    # cross-subdomain cookies. curl_cffi jar usually stores
                    # them with leading "." already, but normalise just in
                    # case.
                    if not cdom.startswith("."):
                        # Bare-host cookies stay bare-host (no leading dot).
                        pass
                    pw_cookies.append({
                        "name": cname,
                        "value": cval,
                        "domain": cdom,
                        "path": c.get("path") or "/",
                        "secure": True,
                    })
                if pw_cookies:
                    try:
                        await ctx.add_cookies(pw_cookies)
                        self._log(
                            f"[sidecar.K2c] synced {len(pw_cookies)} caller "
                            f"cookies into sidecar context"
                        )
                    except Exception as exc:
                        self._log(
                            f"[sidecar.K2c] cookie sync FAILED: "
                            f"{type(exc).__name__}: {exc}"
                        )
                        # Without auth cookies, /about-you would redirect to
                        # /email-verification — K2c can't recover. Bail out.
                        return None

            # ── Register the route BEFORE navigation ─────────────────
            captured: dict = {}
            event = asyncio.Event()
            # Same leak defense as K2 (intercept_register_token): if
            # ``route.abort()`` silently fails OR the request slips past
            # the route handler entirely, the server processes the dummy
            # /create_account submission FIRST and the caller's real POST
            # then hits "user already exists" (HTTP 400). We MUST detect
            # this and drop the captured token so caller falls back to
            # QuickJS for so-token (caller's own POST creates the account).
            leaked_ca_requests: list[str] = []
            # Diagnostic: log ALL POST requests during K2c so we can see
            # if the SPA uses a non-/create_account endpoint to actually
            # create the account.
            all_post_urls: list[str] = []

            def _on_request_finished(request) -> None:
                try:
                    url = request.url
                    method = request.method
                except Exception:
                    return
                if method == "POST" and (
                    "/api/" in url
                    or "auth.openai.com" in url
                ):
                    all_post_urls.append(url)
                if (
                    "/api/accounts/create_account" in url
                    and method == "POST"
                ):
                    leaked_ca_requests.append(url)

            async def _route(route, request):
                url = request.url
                method = request.method
                if (
                    "/api/accounts/create_account" not in url
                    or method != "POST"
                ):
                    try:
                        await route.continue_()
                    except Exception:
                        pass
                    return
                # Got the POST — capture headers + abort.
                try:
                    hdrs = await request.all_headers()
                except Exception:
                    hdrs = dict(request.headers)
                captured["sentinel_token"] = hdrs.get("openai-sentinel-token")
                captured["so_token"] = hdrs.get("openai-sentinel-so-token")
                captured["device_id"] = hdrs.get("oai-device-id")
                captured["body"] = request.post_data
                event.set()
                try:
                    await route.abort()
                    captured["_abort_ok"] = True
                except Exception as exc:
                    captured["_abort_ok"] = False
                    captured["_abort_err"] = repr(exc)

            try:
                await page.route(
                    "**/api/accounts/create_account", _route,
                )
            except Exception as exc:
                self._log(f"[sidecar.K2c] route registration failed: {exc}")
                return None
            try:
                page.on("requestfinished", _on_request_finished)
            except Exception:
                pass

            # ── DEFENSE 0: JS-level fetch + XHR interceptor ────────
            # Camoufox's ``route.abort()`` is NOT 100% reliable for
            # auth.openai.com — empirically the request still reaches
            # the server (verified by HTTP 400 "user already exists"
            # on caller's subsequent POST). To make leak IMPOSSIBLE,
            # we install a fetch + XMLHttpRequest override in the
            # PAGE'S OWN realm via ``add_init_script`` BEFORE the page
            # loads. The override:
            #   1. Detects POST to /api/accounts/create_account.
            #   2. Captures all request headers into ``__capturedCAHeaders``.
            #   3. Aborts the request at the JS layer (Promise.reject /
            #      XHR abort) so the SPA's onsubmit handler sees a
            #      network error but no bytes ever leave the renderer.
            # This bypasses Camoufox's leaky Marionette-level routing.
            try:
                await ctx.add_init_script(
                    """
() => {
    if (window.__K2C_INSTALLED) return;
    window.__K2C_INSTALLED = true;
    window.__capturedCAHeaders = null;
    window.__capturedCABody = null;

    const isCA = (url) => {
        try {
            const s = String(url || '');
            return s.indexOf('/api/accounts/create_account') >= 0;
        } catch (e) { return false; }
    };

    // ── fetch override ──
    const origFetch = window.fetch;
    window.fetch = function(input, init) {
        try {
            const url = (typeof input === 'string') ? input : (input && input.url);
            const method = (init && init.method) || (input && input.method) || 'GET';
            if (isCA(url) && String(method).toUpperCase() === 'POST') {
                // Capture headers from init.headers (Headers object or plain dict)
                let hdrs = {};
                if (init && init.headers) {
                    const h = init.headers;
                    if (typeof h.forEach === 'function') {
                        h.forEach((v, k) => { hdrs[String(k).toLowerCase()] = String(v); });
                    } else {
                        for (const k of Object.keys(h)) {
                            hdrs[String(k).toLowerCase()] = String(h[k]);
                        }
                    }
                }
                window.__capturedCAHeaders = hdrs;
                window.__capturedCABody = (init && init.body) ? String(init.body) : null;
                // Reject the fetch — page sees a network error and the
                // request is NEVER sent to the server.
                return Promise.reject(new TypeError('K2c intercepted'));
            }
        } catch (e) { /* fall through */ }
        return origFetch.apply(this, arguments);
    };

    // ── XHR override (some SPAs use XHR) ──
    const origOpen = XMLHttpRequest.prototype.open;
    const origSend = XMLHttpRequest.prototype.send;
    const origSetHeader = XMLHttpRequest.prototype.setRequestHeader;
    XMLHttpRequest.prototype.open = function(method, url) {
        this.__k2c_method = String(method || '').toUpperCase();
        this.__k2c_url = String(url || '');
        this.__k2c_headers = {};
        return origOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
        if (this.__k2c_headers) {
            this.__k2c_headers[String(name).toLowerCase()] = String(value);
        }
        return origSetHeader.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function(body) {
        if (isCA(this.__k2c_url) && this.__k2c_method === 'POST') {
            window.__capturedCAHeaders = this.__k2c_headers;
            window.__capturedCABody = body ? String(body) : null;
            // Abort BEFORE actual send hits the network
            try { this.abort(); } catch (e) {}
            // Fire an error event so SPA's onerror handler runs
            try {
                this.dispatchEvent(new Event('error'));
            } catch (e) {}
            return; // do NOT call origSend
        }
        return origSend.apply(this, arguments);
    };
}
""".strip()
                )
                self._log("[sidecar.K2c] fetch+XHR interceptor installed")
            except Exception as exc:
                self._log(
                    f"[sidecar.K2c] interceptor install failed: {exc} "
                    f"— relying on page.route only (LEAK RISK)"
                )

            try:
                # ── Navigate to /about-you ───────────────────────────
                await page.goto(
                    "https://auth.openai.com/about-you",
                    wait_until="domcontentloaded",
                    timeout=25000,
                )
                self._log(
                    f"[sidecar.K2c] navigated, url={page.url[:100]!r}"
                )
                # Server can redirect to /email-verification if cookies
                # don't grant /about-you access. Fail fast — better to
                # fall back to QuickJS than send a useless K2c attempt.
                if "/about-you" not in (page.url or ""):
                    self._log(
                        f"[sidecar.K2c] expected /about-you, got "
                        f"{page.url[:100]!r} — cookies missing "
                        f"create_account permission"
                    )
                    return None

                # ── Fill name ────────────────────────────────────────
                name_input = None
                for sel in (
                    'input[name="name"]',
                    'input[autocomplete="name"]',
                    'input[id*="name" i]',
                ):
                    try:
                        loc = page.locator(sel).first
                        if await loc.is_visible(timeout=5000):
                            name_input = loc
                            self._log(f"[sidecar.K2c] name input: {sel}")
                            break
                    except Exception:
                        continue
                if name_input is None:
                    self._log("[sidecar.K2c] name input not found on /about-you")
                    return None
                await _ht(
                    name_input, name,
                    delay_min_ms=60, delay_max_ms=140, log=self._log,
                )
                await asyncio.sleep(_r.uniform(0.35, 0.7))  # tab dwell

                # ── Fill birthdate / age ─────────────────────────────
                # Try ``input[type="date"]`` first; fall back to age field.
                filled_age = False
                try:
                    date_loc = page.locator('input[type="date"]').first
                    if await date_loc.is_visible(timeout=1500):
                        await page.fill('input[type="date"]', birthdate)
                        filled_age = True
                        self._log(
                            f"[sidecar.K2c] filled date input: {birthdate}"
                        )
                except Exception:
                    pass
                if not filled_age:
                    try:
                        year_str, _m, _d = birthdate.split("-", 2)
                    except Exception:
                        year_str = "1999"
                    age_input = None
                    for sel in (
                        'input[name="age"]',
                        'input[type="number"]',
                        'input[inputmode="numeric"]',
                    ):
                        try:
                            loc = page.locator(sel).first
                            if await loc.is_visible(timeout=1500):
                                age_input = loc
                                break
                        except Exception:
                            continue
                    if age_input is not None:
                        # OpenAI A/B test: field number có thể là NĂM SINH
                        # (min 1896) hoặc TUỔI — đọc min/max để điền đúng.
                        value, kind = await _resolve_birth(
                            age_input, birthdate, log=self._log,
                        )
                        await _ht(
                            age_input, value,
                            delay_min_ms=80, delay_max_ms=160, log=self._log,
                        )
                        filled_age = True
                        self._log(f"[sidecar.K2c] filled {kind} input: {value}")
                    else:
                        # NO standalone age input found. The /about-you form
                        # may have changed (server A/B tests UI). Dump all
                        # input names so we know what fields actually exist.
                        try:
                            field_info = await page.evaluate("""
() => Array.from(document.querySelectorAll('input,select,textarea'))
    .filter(el => el.offsetParent !== null || el.type === 'hidden')
    .map(el => ({
        tag: el.tagName.toLowerCase(),
        type: el.type || '',
        name: el.name || '',
        id: el.id || '',
        placeholder: el.placeholder || '',
        required: el.required || false,
    }))
""")
                            self._log(
                                f"[sidecar.K2c] /about-you form fields: "
                                f"{field_info!r}"
                            )
                        except Exception:
                            pass
                        # If only ``name`` is required (newer UI), skip age
                        # fill entirely — form validates with just name.
                        any_age_field = False
                        try:
                            any_age_field = await page.evaluate("""
() => !!document.querySelector(
    'input[name="age"],input[name="birthdate"],input[name="dateOfBirth"],input[type="date"],input[type="number"],input[inputmode="numeric"]'
)
""")
                        except Exception:
                            pass
                        if not any_age_field:
                            self._log(
                                "[sidecar.K2c] no age/date input on form — "
                                "newer /about-you (name only); skipping age"
                            )
                            filled_age = True  # treat as success
                        else:
                            # Last resort: Tab from name → keyboard type.
                            # Only if some age-related field exists. Không có
                            # loc để đọc min/max → điền NĂM SINH (UI hiện tại).
                            try:
                                await page.keyboard.press("Tab")
                                await asyncio.sleep(0.3)
                                for ch in year_str:
                                    await page.keyboard.type(
                                        ch, delay=_r.randint(120, 200),
                                    )
                                filled_age = True
                                self._log(
                                    f"[sidecar.K2c] Tab + typed year: {year_str}"
                                )
                            except Exception as exc:
                                self._log(
                                    f"[sidecar.K2c] age fill failed: {exc}"
                                )
                if not filled_age:
                    self._log("[sidecar.K2c] could not fill age — aborting")
                    return None

                # Brief dwell so observer scores the keystrokes before submit
                await asyncio.sleep(_r.uniform(0.6, 1.0))

                # ── DEFENSE 0b: force-install fetch+XHR override via
                # page.evaluate right before submit. The init_script
                # version can be bypassed if the SPA caches the original
                # fetch reference at import time. ``Object.defineProperty``
                # with ``writable: false`` prevents the SPA from
                # restoring the original.
                try:
                    await page.evaluate(
                        """
() => {
    const isCA = (url) => {
        try {
            return String(url || '').indexOf('/api/accounts/create_account') >= 0;
        } catch (e) { return false; }
    };
    const realFetch = window.__K2C_realFetch || window.fetch;
    window.__K2C_realFetch = realFetch;
    const newFetch = function(input, init) {
        try {
            const url = (typeof input === 'string') ? input : (input && input.url);
            const method = (init && init.method) || (input && input.method) || 'GET';
            if (isCA(url) && String(method).toUpperCase() === 'POST') {
                let hdrs = {};
                if (init && init.headers) {
                    const h = init.headers;
                    if (typeof h.forEach === 'function') {
                        h.forEach((v, k) => { hdrs[String(k).toLowerCase()] = String(v); });
                    } else {
                        for (const k of Object.keys(h)) {
                            hdrs[String(k).toLowerCase()] = String(h[k]);
                        }
                    }
                }
                window.__capturedCAHeaders = hdrs;
                window.__capturedCABody = (init && init.body) ? String(init.body) : null;
                return Promise.reject(new TypeError('K2c intercepted (forced)'));
            }
        } catch (e) {}
        return realFetch.apply(this, arguments);
    };
    try {
        Object.defineProperty(window, 'fetch', {
            value: newFetch,
            writable: false,
            configurable: true,
        });
    } catch (e) {
        window.fetch = newFetch;
    }
    const proto = XMLHttpRequest.prototype;
    if (!proto.__K2C_xhrPatched) {
        const origOpen = proto.open;
        const origSend = proto.send;
        const origSetHeader = proto.setRequestHeader;
        proto.open = function(method, url) {
            this.__k2c_method = String(method || '').toUpperCase();
            this.__k2c_url = String(url || '');
            this.__k2c_headers = {};
            return origOpen.apply(this, arguments);
        };
        proto.setRequestHeader = function(name, value) {
            if (this.__k2c_headers) {
                this.__k2c_headers[String(name).toLowerCase()] = String(value);
            }
            return origSetHeader.apply(this, arguments);
        };
        proto.send = function(body) {
            if (isCA(this.__k2c_url) && this.__k2c_method === 'POST') {
                window.__capturedCAHeaders = this.__k2c_headers;
                window.__capturedCABody = body ? String(body) : null;
                try { this.abort(); } catch (e) {}
                try { this.dispatchEvent(new Event('error')); } catch (e) {}
                return;
            }
            return origSend.apply(this, arguments);
        };
        proto.__K2C_xhrPatched = true;
    }
    return true;
}
""".strip()
                    )
                    self._log("[sidecar.K2c] force-installed interceptor pre-submit")
                except Exception as exc:
                    self._log(
                        f"[sidecar.K2c] force-install pre-submit failed: {exc}"
                    )

                # ── Submit ───────────────────────────────────────────
                clicked = False
                for sel in (
                    'button[type="submit"]',
                    'button:has-text("Continue")',
                    'button:has-text("Sign up")',
                    'button:has-text("Agree")',
                ):
                    try:
                        btn = page.locator(sel).first
                        if (
                            await btn.is_visible(timeout=1500)
                            and await btn.is_enabled(timeout=2500)
                        ):
                            await btn.click(timeout=3000)
                            clicked = True
                            self._log(f"[sidecar.K2c] clicked submit: {sel}")
                            break
                    except Exception:
                        continue
                if not clicked:
                    self._log(
                        "[sidecar.K2c] no enabled submit button — Enter fallback"
                    )
                    try:
                        await page.keyboard.press("Enter")
                        clicked = True
                    except Exception as exc:
                        self._log(
                            f"[sidecar.K2c] Enter fallback failed: {exc}"
                        )
                if not clicked:
                    self._log("[sidecar.K2c] could not submit form — aborting")
                    return None

                # Wait for the routed POST. timeout - 25s leaves headroom
                # for the outer ``run_in_loop`` budget.
                # PRIMARY signal: ``window.__capturedCAHeaders`` populated
                # by the JS-level fetch/XHR interceptor (DEFENSE 0 — leak
                # impossible because the request never leaves the
                # renderer). SECONDARY: ``event.set()`` from the page.route
                # handler (kept as a backstop if some browser internal
                # path bypasses the JS interceptors).
                js_headers: dict | None = None
                deadline = max(15.0, timeout - 30)
                step_t = 0.0
                while step_t < deadline:
                    try:
                        js_headers = await page.evaluate(
                            "() => window.__capturedCAHeaders || null"
                        )
                    except Exception:
                        js_headers = None
                    if js_headers:
                        break
                    if event.is_set():
                        break  # page.route fired (less reliable but acceptable)
                    await asyncio.sleep(0.3)
                    step_t += 0.3

                if js_headers and isinstance(js_headers, dict):
                    # JS interceptor caught the POST — bytes never left
                    # the renderer. Headers are authoritative.
                    captured["sentinel_token"] = js_headers.get(
                        "openai-sentinel-token"
                    )
                    captured["so_token"] = js_headers.get(
                        "openai-sentinel-so-token"
                    )
                    captured["device_id"] = js_headers.get("oai-device-id")
                    captured["_via"] = "js-fetch"
                    self._log("[sidecar.K2c] tokens captured via JS interceptor")
                elif not event.is_set():
                    self._log(
                        f"[sidecar.K2c] neither JS interceptor nor "
                        f"page.route fired in {deadline:.0f}s — "
                        f"url={page.url[:100]!r}"
                    )
                    return None
                else:
                    captured["_via"] = "page.route"
                    self._log(
                        "[sidecar.K2c] tokens via page.route (LEAK RISK — "
                        "JS interceptor missed the request)"
                    )

                # ── DEFENSE 1: navigate away immediately ────────────
                # /about-you SPA may retry the form submit if route.abort
                # surfaces as a transient error. Navigate away to destroy
                # the form before our route handler is unrouted in the
                # ``finally`` block below.
                try:
                    await page.goto("about:blank", timeout=5000)
                except Exception:
                    pass

                # ── DEFENSE 2: verify abort actually succeeded ──────
                # If route.abort() raised internally (and JS interceptor
                # missed), the server already received the dummy
                # /create_account submit. DROP tokens.
                if captured.get("_via") == "page.route" and captured.get("_abort_ok") is False:
                    self._log(
                        f"[sidecar.K2c] route.abort() FAILED "
                        f"({captured.get('_abort_err')!r}) — DROPPING "
                        f"captured tokens."
                    )
                    return None
            finally:
                try:
                    page.remove_listener(
                        "requestfinished", _on_request_finished,
                    )
                except Exception:
                    pass
                try:
                    await page.unroute(
                        "**/api/accounts/create_account", _route,
                    )
                except Exception:
                    pass

            if not captured.get("sentinel_token"):
                self._log(
                    "[sidecar.K2c] /create_account POST captured but "
                    "openai-sentinel-token header missing — invalid"
                )
                return None

            # ── DEFENSE 3: leaked-request audit ────────────────────
            # If any /create_account POST completed its full round-trip,
            # server processed the dummy submission. Account is now bound
            # to sidecar's dummy name/birthdate — caller's real POST will
            # hit HTTP 400. DROP captured tokens.
            if leaked_ca_requests:
                self._log(
                    f"[sidecar.K2c] LEAK DETECTED: {len(leaked_ca_requests)} "
                    f"/create_account POST(s) reached the server. Account "
                    f"is bound to sidecar's dummy name/birthdate; caller "
                    f"will hit HTTP 400 'user already exists'. DROPPING "
                    f"captured tokens."
                )
                return None

            self._log(
                f"[sidecar.K2c] captured ca-tokens "
                f"(sentinel-len={len(captured['sentinel_token'])} "
                f"so_token={'yes' if captured.get('so_token') else 'no'} "
                f"abort_ok={captured.get('_abort_ok')})"
            )
            # Diagnostic: surface ALL POSTs seen during K2c so caller can
            # spot a sneaky endpoint that bypassed our interceptors.
            if all_post_urls:
                # Trim each URL to the path component so the log stays readable
                from urllib.parse import urlparse as _urlparse
                paths = []
                for u in all_post_urls:
                    try:
                        paths.append(_urlparse(u).path or u[:60])
                    except Exception:
                        paths.append(u[:60])
                self._log(
                    f"[sidecar.K2c] POSTs observed during K2c: "
                    f"{sorted(set(paths))!r}"
                )
            return {
                "sentinel_token": captured["sentinel_token"],
                "so_token": captured.get("so_token"),
                "device_id": captured.get("device_id") or device_id,
                "body": captured.get("body"),
            }

        try:
            return self._browser.run_in_loop(
                _do_intercept(), timeout=timeout,
            )
        except Exception as exc:
            self._log(
                f"[sidecar.K2c] intercept_create_account_token failed: {exc}"
            )
            return None

    def dump_cookies(self, *, timeout: float = 10.0) -> list[dict]:
        """All cookies from the per-signup ``BrowserContext`` in Playwright
        dict shape (name/value/domain/path/...).
        """
        if self._browser is None or self._ctx is None:
            return []
        async def _dump():
            return await self._ctx.cookies()
        try:
            cookies = self._browser.run_in_loop(_dump(), timeout=timeout)
            return list(cookies) if cookies else []
        except Exception as exc:
            self._log(f"[sidecar] dump_cookies failed: {exc}")
            return []

    def get_so_token(
        self,
        *,
        device_id: str,
        flow: str = "create_account",
        timeout: float = 30.0,
    ) -> Optional[str]:
        """Extract the so-token (Session Observer payload) for the given flow.

        The sentinel-token JSON returned by ``get_sentinel_token`` is the
        full assembled blob ``{p, t, c, id, flow}``. The Sentinel SDK
        bundles the Session Observer output under the ``so`` field when
        events have been recorded. We expose it separately because
        ``openai-sentinel-so-token`` HTTP header carries only ``{so, c}``
        (the so-token + challenge token), not the full sentinel-token.

        Returns None when sdk.js's ``token()`` returns no ``so`` field —
        Observer probably never fired enough events. Caller should skip
        the header rather than send empty (an empty header is itself a
        signal that the bot tried but failed).
        """
        if self._oracle is None or self._browser is None:
            return None

        async def _gen_so() -> Optional[str]:
            base = await self._oracle.get_token(
                device_id=device_id, flow=flow,
            )
            if not base:
                return None
            import json as _json
            try:
                base_data = _json.loads(base)
            except Exception:
                return None
            c_value = str(base_data.get("c") or "").strip()
            if not c_value:
                return None
            try:
                so_payload = await self._page.evaluate(
                    """async (cTok) => {
                        if (typeof globalThis.SentinelSDK?.token !== 'function') return null;
                        try {
                            const r = await globalThis.SentinelSDK.token({ token: cTok, c: cTok });
                            if (!r || typeof r !== 'object') return null;
                            const so = r.so;
                            if (so == null || so === '') return null;
                            return { so: String(so), c: cTok };
                        } catch (e) { return { __error: String(e) }; }
                    }""",
                    c_value,
                )
            except Exception as exc:
                self._log(f"[sidecar] so-token evaluate failed: {exc}")
                return None
            if not so_payload or so_payload.get("__error"):
                if so_payload and so_payload.get("__error"):
                    self._log(
                        f"[sidecar] so-token sdk error: {so_payload['__error']}"
                    )
                return None
            return _json.dumps(
                {"so": so_payload["so"], "c": so_payload["c"]},
                separators=(",", ":"),
            )

        try:
            return self._browser.run_in_loop(_gen_so(), timeout=timeout)
        except Exception as exc:
            self._log(f"[sidecar] get_so_token failed: {exc}")
            return None
