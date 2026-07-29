"""Page-native sentinel-token generator (Camoufox/Playwright).

Replaces QuickJS-based token generation for paths that already have a live
Camoufox ``page`` — primarily ``session_phase`` (browser login → get_session).

Why
---
``sentinel_quickjs.py`` runs ``sdk.js`` inside a Node subprocess. Node has no
canvas, no WebGL, no AudioContext, no real navigator.plugins — every
fingerprint vector returns empty/undefined. The resulting ``so-token`` is a
"zero-fingerprint" signature that OpenAI's server-side anomaly detector flags
as bot. Symptom: accounts get deactivated 1-24h after creation/login
("deferred ban").

This module runs the same patched ``sdk.js`` inside Camoufox's real Firefox
page via ``page.evaluate``. Canvas, WebGL, AudioContext, plugins all return
authentic Firefox values (already spoofed-consistently by Camoufox).

Protocol (mirrors ``sentinel_quickjs``):

  1. ``page.evaluate(in_page_script, {action: "requirements", ...})``
     → ``{request_p}``    (sdk.js reads REAL navigator/canvas/WebGL inside page)
  2. ``ctx.request.post("/sentinel/req", {p: request_p, id, flow})``
     → ``{token, turnstile, ...}``   (HTTP via Camoufox context — same TLS,
                                      same cookies as page)
  3. ``page.evaluate(in_page_script, {action: "solve", challenge, ...})``
     → ``{final_p, t}``    (sdk.js solves PoW + computes t)
  4. Assemble ``{p: final_p, t, c: server_token, id, flow}`` → JSON string.

Public API
----------
    oracle = SentinelBrowserOracle(page, ctx, log=log)
    token = await oracle.get_token(device_id=did, flow="login")

Returns the full JSON token string (caller assigns to
``openai-sentinel-token`` header). Returns ``None`` on any internal failure
so caller can fall back to QuickJS (lower-quality token but won't crash).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional


logger = logging.getLogger(__name__)

SENTINEL_VERSION = "20260219f9f6"
SENTINEL_SDK_URL = (
    f"https://sentinel.openai.com/sentinel/{SENTINEL_VERSION}/sdk.js"
)
SENTINEL_REQ_URL = (
    "https://sentinel.openai.com/backend-api/sentinel/req"
)


# Patch markers that ``openai_sentinel_in_page.js`` rewrites in sdk.js to
# expose internal globals (SentinelSDK, __debugP, __debug_n, __debug_bindProof).
# These strings live in OpenAI's minified bundle — they break the moment
# OpenAI rotates ``SENTINEL_VERSION`` or recompiles sdk.js with different
# variable name mangling. When that happens we fail loudly so the operator
# can update the patch instead of silently emitting empty so-tokens.
_SDK_PATCH_MARKERS: tuple[tuple[str, str], ...] = (
    ("SDK_GLOBAL_PATCH",    "var SentinelSDK="),
    ("INSTANCE_PATCH",      "var P=new _;"),
    ("EXPOSE_PATCH",        "return o?r?.[n(63)]?ce({so:o,c:r[n(63)]},t):o:null},t.token=ye,t}({});"),
)


class SdkPatchOutOfDateError(RuntimeError):
    """sdk.js source no longer contains the expected patch markers.

    Raised before the in-page script runs so caller can fall back to QuickJS
    (still degraded but won't silently send empty so-tokens).
    """


def _verify_sdk_patch_markers(text: str, *, log: Callable[[str], None]) -> None:
    """Scan downloaded sdk.js for the 3 patch anchors. Raise
    ``SdkPatchOutOfDateError`` if any are missing — caller decides whether
    to abort or fall back. We log every miss with the marker label so
    operators can compare against the current sdk.js bundle.
    """
    missing: list[str] = []
    for label, marker in _SDK_PATCH_MARKERS:
        if marker not in text:
            missing.append(label)
            log(
                f"[sentinel-oracle] sdk.js missing patch marker {label!r} "
                f"(string: {marker[:60]!r}…). sdk.js may have rotated."
            )
    if missing:
        raise SdkPatchOutOfDateError(
            f"sdk.js patch markers missing: {missing}. Update markers in "
            f"sentinel_browser.py:_SDK_PATCH_MARKERS and "
            f"openai_sentinel_in_page.js."
        )
    log(f"[sentinel-oracle] sdk.js patch markers verified (v{SENTINEL_VERSION})")


def _in_page_script_path() -> Path:
    return Path(__file__).resolve().parent / "openai_sentinel_in_page.js"


def _json_string_literal(s: str) -> str:
    """JSON-encode + wrap in JS string literal so we can embed huge sdk.js
    source into a JS source template safely (no quote-escaping pitfalls).
    """
    return json.dumps(s)


def _build_install_script(in_page_script_text: str, sdk_source: str) -> str:
    """Compose the script that, when executed in a page's MAIN realm,
    installs ``globalThis.__runSentinelInPage`` + sdk.js source string.

    Designed to be passed both to ``BrowserContext.add_init_script`` (runs
    automatically on every new document, BEFORE any other scripts) and to
    a fallback ``<script>`` element when the main-realm install is needed
    after a navigation already happened.

    The build is idempotent — second invocation short-circuits via
    ``__sentinel_inpage_ready``.
    """
    return (
        "(function() {\n"
        "  if (globalThis.__sentinel_inpage_ready) return;\n"
        # Inline the helper script (declares async function __runSentinelInPage)
        + in_page_script_text + "\n"
        "  try {\n"
        "    globalThis.__runSentinelInPage = __runSentinelInPage;\n"
        "    globalThis.__sentinel_sdk_source = "
        + _json_string_literal(sdk_source) + ";\n"
        "    globalThis.__sentinel_inpage_ready = true;\n"
        "  } catch (e) {\n"
        "    globalThis.__sentinel_inpage_error = String(e);\n"
        "  }\n"
        "})();\n"
    )


async def fetch_sdk_and_build_install_script(
    request_ctx: Any, *, log: Callable[[str], None],
) -> str:
    """Pre-fetch sdk.js via ``ctx.request`` + build the init-time install
    script. Caller (``_SharedBrowser.acquire_context``) registers it as
    ``BrowserContext.add_init_script`` BEFORE creating any page, so the
    script lands in MAIN realm at document_start of every navigation.

    Phase 11.3 — this is the only path that reliably keeps sdk.js running
    in main realm. Late ``page.evaluate(install_via_script_tag)`` works on
    permissive pages but fails on chatgpt.com / auth.openai.com due to
    inline-script CSP (causes ``__sentinel_inpage_ready`` to never flip).
    """
    if request_ctx is None:
        raise RuntimeError("context.request unavailable")
    resp = await request_ctx.get(
        SENTINEL_SDK_URL,
        headers={
            "accept": "*/*",
            "sec-fetch-dest": "script",
            "sec-fetch-mode": "no-cors",
            "sec-fetch-site": "same-site",
        },
    )
    status = getattr(resp, "status", 0)
    if status != 200:
        raise RuntimeError(f"sdk.js fetch HTTP {status}")
    sdk_text = await resp.text()
    if not sdk_text:
        raise RuntimeError("sdk.js fetch returned empty body")
    _verify_sdk_patch_markers(sdk_text, log=log)
    in_page_path = _in_page_script_path()
    if not in_page_path.exists():
        raise RuntimeError(f"in-page script not found: {in_page_path}")
    return _build_install_script(in_page_path.read_text(encoding="utf-8"), sdk_text)


class SentinelBrowserOracle:
    """Stateful per-page oracle. Construct once per session, reuse across
    multiple ``get_token`` calls (sdk.js loads once via in-page cache).

    Thread-safe? No. Designed for serial async use inside a single
    ``_drive_session_flow`` invocation.
    """

    def __init__(
        self,
        page: Any,
        ctx: Any,
        *,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._page = page
        self._ctx = ctx
        self._log = log or (lambda m: logger.info(m))
        self._sdk_text: Optional[str] = None
        self._script_text: Optional[str] = None

    # ── Internal helpers ───────────────────────────────────────────────

    async def _fetch_sdk_text(self) -> str:
        """Download sdk.js via ``ctx.request`` (preserves cookies + TLS).
        Cached after first call.
        """
        if self._sdk_text is not None:
            return self._sdk_text
        request_ctx = getattr(self._ctx, "request", None)
        if request_ctx is None:
            raise RuntimeError(
                "context.request unavailable (Camoufox/Playwright too old?)"
            )
        resp = await request_ctx.get(
            SENTINEL_SDK_URL,
            headers={
                "accept": "*/*",
                "sec-fetch-dest": "script",
                "sec-fetch-mode": "no-cors",
                "sec-fetch-site": "same-site",
            },
        )
        status = getattr(resp, "status", 0)
        if status != 200:
            raise RuntimeError(f"sdk.js fetch HTTP {status}")
        text = await resp.text()
        if not text:
            raise RuntimeError("sdk.js fetch returned empty body")
        _verify_sdk_patch_markers(text, log=self._log)
        self._sdk_text = text
        return text

    def _load_in_page_script(self) -> str:
        if self._script_text is not None:
            return self._script_text
        path = _in_page_script_path()
        if not path.exists():
            raise RuntimeError(f"in-page script not found: {path}")
        self._script_text = path.read_text(encoding="utf-8")
        return self._script_text

    async def _ensure_sdk_installed_in_page(self) -> None:
        """Install sdk.js + helpers into the page's MAIN realm.

        Phase 11.3 — strategy:

        1. ``BrowserContext.add_init_script(install_script)`` is registered
           BEFORE navigation by ``_SharedBrowser.acquire_context``. That
           script (built via :func:`build_sdk_install_script`) installs
           ``globalThis.__runSentinelInPage`` and a baked sdk.js source
           string into the page's MAIN realm right after the document is
           created.

        2. Here we only verify the install completed (the page may have
           been created with a fresh navigation, or the init script may
           have errored). If the global isn't ready we try to install via
           a ``<script>`` element fallback (works on CSP-permissive
           pages).

        The point is to keep sdk.js execution inside the page's native
        realm so the Firefox Xray boundary is never crossed when sdk.js
        reads its own TypedArrays.
        """
        ready = False
        try:
            ready = bool(await self._page.evaluate(
                "() => !!globalThis.__sentinel_inpage_ready"
            ))
        except Exception:
            ready = False
        if ready:
            return

        # Fallback: try injecting via <script> element (works on pages
        # without strict inline CSP).
        sdk_text = await self._fetch_sdk_text()
        script_text = self._load_in_page_script()
        install_script = _build_install_script(script_text, sdk_text)
        try:
            await self._page.evaluate(
                "async (src) => {\n"
                "  if (globalThis.__sentinel_inpage_ready) return;\n"
                "  const s = document.createElement('script');\n"
                "  s.textContent = src;\n"
                "  document.head.appendChild(s);\n"
                "  const deadline = Date.now() + 5000;\n"
                "  while (!globalThis.__sentinel_inpage_ready && Date.now() < deadline) {\n"
                "    await new Promise(r => setTimeout(r, 25));\n"
                "  }\n"
                "  if (!globalThis.__sentinel_inpage_ready) {\n"
                "    throw new Error('sentinel in-page bootstrap timeout (CSP likely)');\n"
                "  }\n"
                "}",
                install_script,
            )
        except Exception as exc:
            raise RuntimeError(
                f"sentinel in-page install failed: {exc}. "
                f"Likely cause: init_script wasn't registered before navigation, "
                f"and current page has inline-script CSP."
            ) from exc

    async def _run_action(self, action: str, payload: dict) -> dict:
        """Invoke ``__runSentinelInPage`` installed in MAIN realm.

        ``page.evaluate`` runs the wrapper function in the isolated realm,
        but the wrapper merely *references* ``globalThis.__runSentinelInPage``
        which lives in main realm. JavaScript invokes the function with
        its own ``this`` and lexical scope, so sdk.js code (including its
        TypedArray accesses) executes in main realm — no Xray boundary
        crossed. We then ``JSON.parse(JSON.stringify(result))`` to cross
        the boundary safely as plain string/number/object data.
        """
        await self._ensure_sdk_installed_in_page()
        result = await self._page.evaluate(
            "async (payload) => {\n"
            "  if (typeof globalThis.__runSentinelInPage !== 'function') {\n"
            "    throw new Error('__runSentinelInPage missing — main-realm install failed');\n"
            "  }\n"
            "  const r = await globalThis.__runSentinelInPage({\n"
            "    sdkSource: globalThis.__sentinel_sdk_source,\n"
            "    payload,\n"
            "  });\n"
            "  // Defensive: only return JSON-cloneable data across the\n"
            "  // isolated↔main realm boundary. Any TypedArrays leaking\n"
            "  // out would re-trigger the Xray issue.\n"
            "  return JSON.parse(JSON.stringify(r));\n"
            "}",
            {"action": action, **payload},
        )
        return result if isinstance(result, dict) else {}

    async def _fetch_challenge(
        self, *, device_id: str, flow: str, request_p: str,
    ) -> dict:
        request_ctx = getattr(self._ctx, "request", None)
        if request_ctx is None:
            raise RuntimeError("context.request unavailable")
        body = {"p": request_p, "id": device_id, "flow": flow}
        headers = {
            "origin": "https://sentinel.openai.com",
            "referer": (
                f"https://sentinel.openai.com/backend-api/sentinel/"
                f"frame.html?sv={SENTINEL_VERSION}"
            ),
            "content-type": "text/plain;charset=UTF-8",
            "accept": "*/*",
        }
        resp = await request_ctx.post(
            SENTINEL_REQ_URL,
            data=json.dumps(body, separators=(",", ":")),
            headers=headers,
        )
        status = getattr(resp, "status", 0)
        if status != 200:
            raise RuntimeError(f"/sentinel/req HTTP {status}")
        try:
            return await resp.json()
        except Exception:
            text = await resp.text()
            raise RuntimeError(f"/sentinel/req non-JSON: {text[:200]!r}")

    # ── Public API ─────────────────────────────────────────────────────

    async def get_token(
        self,
        *,
        device_id: str,
        flow: str = "login",
        log_prefix: str = "[sentinel-browser]",
    ) -> Optional[str]:
        """Generate a sentinel-token using page-native sdk.js.

        Returns the full assembled JSON token string ready to be sent as the
        ``openai-sentinel-token`` HTTP header. Returns ``None`` on failure;
        caller may fall back to QuickJS but should log it as a degraded path.
        """
        try:
            # 1. Requirements (real fingerprints from Camoufox page)
            req_result = await self._run_action(
                "requirements", {"device_id": device_id},
            )
            request_p = str(req_result.get("request_p") or "").strip()
            if not request_p:
                self._log(f"{log_prefix} requirements returned empty request_p")
                return None

            # 2. HTTP /sentinel/req via Camoufox context
            challenge = await self._fetch_challenge(
                device_id=device_id, flow=flow, request_p=request_p,
            )
            c_token = str(challenge.get("token") or "").strip()
            if not c_token:
                self._log(f"{log_prefix} challenge token missing")
                return None

            # 3. Solve (real PoW solver inside page)
            solve_result = await self._run_action(
                "solve",
                {
                    "device_id": device_id,
                    "request_p": request_p,
                    "challenge": challenge,
                },
            )
            final_p = str(solve_result.get("final_p") or "").strip()
            t_raw = solve_result.get("t")
            t_value = "" if t_raw is None else str(t_raw).strip()
            if not final_p or not t_value:
                self._log(
                    f"{log_prefix} solve incomplete "
                    f"(final_p={'OK' if final_p else 'MISSING'}, "
                    f"t={'OK' if t_value else 'MISSING'})"
                )
                return None

            token = json.dumps(
                {
                    "p": final_p, "t": t_value, "c": c_token,
                    "id": device_id, "flow": flow,
                },
                separators=(",", ":"),
                ensure_ascii=False,
            )
            self._log(
                f"{log_prefix} OK (p={len(final_p)} t={len(t_value)} "
                f"c={len(c_token)} flow={flow})"
            )
            return token
        except Exception as exc:  # noqa: BLE001 — best-effort, caller fallbacks
            self._log(f"{log_prefix} error: {type(exc).__name__}: {exc}")
            return None



# ─────────────────────────────────────────────────────────────────────
# Fingerprint health check
# ─────────────────────────────────────────────────────────────────────
#
# Verifies that a freshly-launched Camoufox page produces real
# canvas/WebGL/audio fingerprints. In rare cases (broken Camoufox binary,
# missing GeoIP DB, Linux without virtual display, etc.) headless mode can
# degrade: WebGL falls back to software with empty vendor, canvas returns
# fixed pattern, AudioContext is suppressed. sdk.js then emits a
# zero-fingerprint so-token → server flags account.
#
# Best practice: call this RIGHT AFTER ``page.goto(chatgpt.com)`` (first
# navigation) and log the snapshot. If ``healthy`` is False the operator
# can switch to ``headless=False`` or fix the environment before burning
# OTP credits.


_FINGERPRINT_PROBE_JS = r"""() => {
  const result = {};
  // ── WebGL ─────────────────────────────────────────────
  try {
    const c = document.createElement('canvas');
    c.width = 200; c.height = 50;
    const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
    if (gl) {
      const ext = gl.getExtension('WEBGL_debug_renderer_info');
      if (ext) {
        result.webgl_vendor = String(gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) || '');
        result.webgl_renderer = String(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) || '');
      } else {
        result.webgl_vendor = '';
        result.webgl_renderer = '';
        result.webgl_no_debug_ext = true;
      }
    } else {
      result.webgl_context_null = true;
    }
  } catch (e) { result.webgl_error = String(e); }

  // ── Canvas 2D ─────────────────────────────────────────
  try {
    const c1 = document.createElement('canvas');
    c1.width = 80; c1.height = 40;
    const ctx = c1.getContext('2d');
    if (ctx) {
      ctx.fillStyle = 'rgba(100,150,200,0.7)';
      ctx.fillRect(0, 0, 80, 40);
      ctx.fillStyle = 'rgba(220,80,40,0.5)';
      ctx.fillRect(10, 5, 50, 20);
      ctx.font = '13px sans-serif';
      ctx.fillStyle = '#fff';
      ctx.fillText('cfp', 5, 20);
      result.canvas_data_url = c1.toDataURL();
      result.canvas_length = result.canvas_data_url.length;
    } else {
      result.canvas_2d_null = true;
    }
  } catch (e) { result.canvas_error = String(e); }

  // ── AudioContext ──────────────────────────────────────
  try {
    const Ctor = window.AudioContext || window.webkitAudioContext;
    if (Ctor) {
      const ac = new Ctor();
      result.audio_context = true;
      result.audio_sample_rate = Number(ac.sampleRate || 0);
      try { ac.close(); } catch (e) {}
    } else {
      result.audio_context = false;
    }
  } catch (e) { result.audio_error = String(e); result.audio_context = false; }

  // ── Navigator ─────────────────────────────────────────
  try {
    result.plugins_count = (navigator.plugins && navigator.plugins.length) || 0;
    result.languages = Array.from(navigator.languages || []).slice(0, 4);
    result.user_agent = String(navigator.userAgent || '');
    result.platform = String(navigator.platform || '');
    result.hardware_concurrency = Number(navigator.hardwareConcurrency || 0);
    result.device_memory = Number(navigator.deviceMemory || 0);
    result.webdriver = !!navigator.webdriver;
  } catch (e) { result.navigator_error = String(e); }

  return result;
}"""


async def verify_fingerprint_health(
    page: Any,
    *,
    log: Optional[Callable[[str], None]] = None,
    strict: bool = False,
) -> dict:
    """Probe Camoufox page for real fingerprint vectors.

    Args:
        page: live Camoufox Page (must already have document available).
        log: optional log callback. None → silent.
        strict: True → raise RuntimeError when any critical signal missing.
            False (default) → log warning, return snapshot with
            ``healthy=False`` so caller may continue with reduced safety.

    Returns:
        Snapshot dict including:
          - ``webgl_vendor`` / ``webgl_renderer`` (str; empty → degraded)
          - ``canvas_data_url`` / ``canvas_length`` (toDataURL output)
          - ``audio_context`` (bool) / ``audio_sample_rate``
          - ``plugins_count`` / ``hardware_concurrency`` / ``device_memory``
          - ``user_agent`` / ``platform`` / ``languages``
          - ``webdriver`` (must be False)
          - ``issues`` (list[str]) / ``healthy`` (bool)
    """
    _log = log or (lambda m: logger.info(m))
    try:
        snapshot = await page.evaluate(_FINGERPRINT_PROBE_JS)
    except Exception as exc:
        _log(f"[fingerprint] probe failed: {type(exc).__name__}: {exc}")
        return {"healthy": False, "issues": [f"probe_exception:{type(exc).__name__}"]}

    issues: list[str] = []
    # WebGL must report non-empty vendor + renderer (Camoufox spoofs these
    # to real preset values; empty = patch missing or headless degraded).
    if not snapshot.get("webgl_vendor"):
        issues.append("webgl_vendor_empty")
    if not snapshot.get("webgl_renderer"):
        issues.append("webgl_renderer_empty")
    # Canvas data URL should be long (PNG base64 of rendered text+shapes).
    # ~600+ chars expected for 80×40 canvas. < 300 = blank/all-zero pixels.
    canvas_len = int(snapshot.get("canvas_length") or 0)
    if canvas_len == 0:
        issues.append("canvas_empty")
    elif canvas_len < 300:
        issues.append(f"canvas_too_short:{canvas_len}")
    # AudioContext must be available (sdk.js reads sample rate).
    if not snapshot.get("audio_context"):
        issues.append("audio_context_missing")
    elif int(snapshot.get("audio_sample_rate") or 0) <= 0:
        issues.append("audio_sample_rate_zero")
    # navigator.hardwareConcurrency = 0 is impossible on real hardware.
    if int(snapshot.get("hardware_concurrency") or 0) <= 0:
        issues.append("hardware_concurrency_zero")
    # navigator.webdriver must be False (Camoufox should mask it).
    if snapshot.get("webdriver") is True:
        issues.append("navigator_webdriver_true")

    snapshot["issues"] = issues
    snapshot["healthy"] = len(issues) == 0

    # Compact one-line summary so operators can grep logs
    vendor = (snapshot.get("webgl_vendor") or "")[:40]
    renderer = (snapshot.get("webgl_renderer") or "")[:50]
    audio = "on" if snapshot.get("audio_context") else "OFF"
    sr = int(snapshot.get("audio_sample_rate") or 0)
    plugins = snapshot.get("plugins_count", 0)
    hc = snapshot.get("hardware_concurrency", 0)
    wd = "BOT" if snapshot.get("webdriver") else "off"
    marker = "OK" if snapshot["healthy"] else f"DEGRADED[{len(issues)}]"
    _log(
        f"[fingerprint] {marker} webgl='{vendor}'/'{renderer}' "
        f"canvas={canvas_len}B audio={audio}@{sr}Hz plugins={plugins} "
        f"hc={hc} webdriver={wd}"
    )
    if issues:
        _log(f"[fingerprint] WARNING issues={issues}")
        if strict:
            raise RuntimeError(
                f"fingerprint health check failed (strict mode): {issues}"
            )
    return snapshot
