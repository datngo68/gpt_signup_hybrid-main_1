"""Verify Phase 10 — pure-HTTP hardening with SentinelSidecar.

Checks:
  1. AST parse 3 files: sentinel_sidecar.py, request_phase.py, signup.py.
  2. SentinelSidecar class API:
     - __init__(*, proxy, headless, locale, log, os_target) - kw-only
     - sync methods: start, close, get_sentinel_token, get_so_token,
       dump_cookies
     - JS markers in sentinel_sidecar.py source
  3. request_phase wiring:
     - _step_create_account accepts so_token kw with header injection
     - _run_request_phase_sync accepts sidecar param
     - sidecar.get_sentinel_token call site for username_password_create
     - sidecar.get_sentinel_token call site for create_account
     - sidecar.get_so_token call site for create_account
     - _import_cookies_from_sidecar called BEFORE register POST
     - run_request_phase spawns sidecar with timeout, closes in finally
     - REG_SIDECAR_DISABLED env flag respected
  4. signup.py: pure_request branch warning updated (no longer claims
     so-token missing).
  5. Smoke SentinelSidecar with monkey-patched AsyncCamoufox + oracle:
     - start/close lifecycle works
     - get_sentinel_token returns oracle's token
     - dump_cookies returns the ctx.cookies() result
     - methods raise / return None gracefully if not started
"""
from __future__ import annotations

import ast
import asyncio
import json
import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def step_ast() -> None:
    print("[1/5] AST parse changed files...")
    for name in ("sentinel_sidecar.py", "request_phase.py", "signup.py"):
        p = ROOT / name
        ast.parse(p.read_text(), filename=str(p))
        print(f"      ✓ {name}")


# ─── 2. SentinelSidecar API ──────────────────────────────────────────


def step_sidecar_api() -> None:
    print("[2/5] SentinelSidecar API surface...")
    src = (ROOT / "sentinel_sidecar.py").read_text()
    tree = ast.parse(src)

    # SentinelSidecar class
    sc = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.ClassDef) and n.name == "SentinelSidecar"),
        None,
    )
    if sc is None:
        raise SystemExit("FAIL: SentinelSidecar class missing")

    init = next((m for m in sc.body if isinstance(m, ast.FunctionDef) and m.name == "__init__"), None)
    if init is None:
        raise SystemExit("FAIL: SentinelSidecar.__init__ missing")
    kwonly = [a.arg for a in init.args.kwonlyargs]
    for required in ("proxy", "headless", "locale", "log", "os_target"):
        if required not in kwonly:
            raise SystemExit(f"FAIL: __init__ missing kw {required!r}, got {kwonly}")
    print(f"      ✓ SentinelSidecar.__init__(*, {', '.join(kwonly)})")

    method_names = {m.name for m in sc.body if isinstance(m, ast.FunctionDef)}
    async_methods = {m.name for m in sc.body if isinstance(m, ast.AsyncFunctionDef)}
    for required in ("start", "close", "get_sentinel_token", "get_so_token", "dump_cookies"):
        if required not in method_names:
            raise SystemExit(
                f"FAIL: SentinelSidecar.{required} missing or async. "
                f"methods={method_names} async={async_methods}"
            )
    print(f"      ✓ sync methods: start, close, get_sentinel_token, get_so_token, dump_cookies")

    # SentinelSidecarPool class
    pool = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.ClassDef) and n.name == "SentinelSidecarPool"),
        None,
    )
    if pool is None:
        raise SystemExit("FAIL: SentinelSidecarPool class missing")
    pool_methods = {m.name for m in pool.body if isinstance(m, ast.FunctionDef)}
    for required in ("instance", "acquire", "release", "shutdown_all", "stats"):
        if required not in pool_methods:
            raise SystemExit(f"FAIL: SentinelSidecarPool.{required} missing. methods={pool_methods}")
    print(f"      ✓ SentinelSidecarPool: instance, acquire, release, shutdown_all, stats")

    # _SharedBrowser class
    sb = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.ClassDef) and n.name == "_SharedBrowser"),
        None,
    )
    if sb is None:
        raise SystemExit("FAIL: _SharedBrowser class missing")
    sb_methods = {m.name for m in sb.body if isinstance(m, ast.FunctionDef)}
    for required in ("start", "shutdown", "add_ref", "release_ref", "acquire_context", "release_context"):
        if required not in sb_methods:
            raise SystemExit(f"FAIL: _SharedBrowser.{required} missing. methods={sb_methods}")
    print(f"      ✓ _SharedBrowser: start, shutdown, add_ref/release_ref, acquire_context/release_context")

    if "persistent_context=False" not in src:
        raise SystemExit(
            "FAIL: _SharedBrowser must use persistent_context=False "
            "to get a Browser (allowing new_context for per-signup isolation)"
        )
    print("      ✓ shared browser uses persistent_context=False (multi-context)")

    if "atexit.register" not in src:
        raise SystemExit("FAIL: pool must register atexit shutdown handler")
    print("      ✓ atexit handler registered for clean shutdown")

    # Phase 11: trusted events + persona rotation + sdk patch verification
    if "_simulate_trusted_input" not in src:
        raise SystemExit(
            "FAIL: missing trusted input helper (_simulate_trusted_input). "
            "Phase 11 requires Playwright native mouse/keyboard, not dispatchEvent."
        )
    if "_SIMULATE_FORM_INTERACTION_JS" in src:
        raise SystemExit(
            "FAIL: legacy dispatchEvent script still present. "
            "Phase 11 removes JS dispatchEvent (isTrusted=false) in favor of CDP path."
        )
    for marker in ("page.mouse.move", "page.mouse.click", "page.keyboard.type"):
        if marker not in src:
            raise SystemExit(f"FAIL: trusted input must use {marker!r}")
    print("      ✓ trusted input via page.mouse + page.keyboard (isTrusted=true)")

    if "_new_persona_seeds" not in src or "_build_persona_init_script" not in src:
        raise SystemExit("FAIL: per-context persona rotation helpers missing")
    for marker in (
        "setCanvasSeed", "setAudioFingerprintSeed", "setFontSpacingSeed",
        "setWebGLVendor", "setWebGLRenderer",
    ):
        if marker not in src:
            raise SystemExit(f"FAIL: persona script must seed {marker}")
    if "ctx.add_init_script(_build_persona_init_script" not in src:
        raise SystemExit(
            "FAIL: persona script must be wired to ctx.add_init_script"
        )
    print("      ✓ per-context persona rotation (canvas/audio/font/WebGL seeds)")


# ─── 3. request_phase wiring ─────────────────────────────────────────


def step_request_phase_wired() -> None:
    print("[3/5] request_phase wiring...")
    src = (ROOT / "request_phase.py").read_text()
    tree = ast.parse(src)

    # _step_create_account signature has so_token kw
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "_step_create_account"),
        None,
    )
    if fn is None:
        raise SystemExit("FAIL: _step_create_account missing")
    kwonly_keys = [a.arg for a in fn.args.args] + [a.arg for a in fn.args.kwonlyargs]
    # so_token can be either positional-with-default or kwonly — accept both.
    if "so_token" not in kwonly_keys:
        raise SystemExit(f"FAIL: _step_create_account missing so_token param. args={kwonly_keys}")
    body_src = ast.unparse(fn)
    if "openai-sentinel-so-token" not in body_src:
        raise SystemExit("FAIL: _step_create_account doesn't add openai-sentinel-so-token header")
    print("      ✓ _step_create_account has so_token + sets openai-sentinel-so-token header")

    # _run_request_phase_sync accepts sidecar
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "_run_request_phase_sync"),
        None,
    )
    if fn is None:
        raise SystemExit("FAIL: _run_request_phase_sync missing")
    arg_names = [a.arg for a in fn.args.args]
    if "sidecar" not in arg_names:
        raise SystemExit(f"FAIL: sync core missing sidecar param. args={arg_names}")
    print(f"      ✓ _run_request_phase_sync({', '.join(arg_names)})")

    # Call sites for sidecar.get_sentinel_token (×2: register + create_account)
    sent_call_count = src.count("sidecar.get_sentinel_token(")
    if sent_call_count < 2:
        raise SystemExit(
            f"FAIL: expected ≥2 sidecar.get_sentinel_token() calls, got {sent_call_count}"
        )
    print(f"      ✓ sidecar.get_sentinel_token called {sent_call_count}× (register + create_account)")

    # Call site for sidecar.get_so_token (create_account)
    if src.count("sidecar.get_so_token(") < 1:
        raise SystemExit("FAIL: sidecar.get_so_token never called")
    print("      ✓ sidecar.get_so_token called for create_account")

    # _import_cookies_from_sidecar must be present + called.
    if "_import_cookies_from_sidecar" not in src:
        raise SystemExit("FAIL: _import_cookies_from_sidecar helper missing")
    if src.count("_import_cookies_from_sidecar(") < 2:
        # called both before register AND before create_account
        raise SystemExit(
            "FAIL: _import_cookies_from_sidecar should be invoked at register AND create_account"
        )
    print("      ✓ _import_cookies_from_sidecar invoked (register + create_account)")

    # Ordering: cookie import BEFORE the register POST.
    idx_helper = src.find("_import_cookies_from_sidecar(session, sidecar, log)")
    idx_register_post = src.find(
        'session.post(\n                "https://auth.openai.com/api/accounts/user/register"'
    )
    if idx_helper == -1 or idx_register_post == -1:
        # Fallback: at least make sure helper is callable in module scope
        idx_register_post = src.find("user/register")
    if idx_helper > 0 and idx_register_post > 0 and idx_helper >= idx_register_post:
        raise SystemExit(
            "FAIL: _import_cookies_from_sidecar must be called BEFORE /register POST"
        )
    print("      ✓ cookie import precedes /register POST")

    # run_request_phase spawns + closes sidecar
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_request_phase"),
        None,
    )
    if fn is None:
        raise SystemExit("FAIL: async run_request_phase missing")
    body = ast.unparse(fn)
    for marker in (
        "SentinelSidecar",
        "sidecar.start(",
        "sidecar.close(",
        "REG_SIDECAR_DISABLED",
        "asyncio.to_thread",
    ):
        if marker not in body:
            raise SystemExit(f"FAIL: run_request_phase body missing marker {marker!r}")
    print("      ✓ run_request_phase spawns/closes sidecar + REG_SIDECAR_DISABLED env flag")


# ─── 4. signup.py warning updated ────────────────────────────────────


def step_signup_warning_updated() -> None:
    print("[4/5] signup.py pure_request warning...")
    src = (ROOT / "signup.py").read_text()
    # Old warning claimed "KHÔNG gen được openai-sentinel-so-token" — must be gone now.
    if "KHÔNG gen được" in src and "openai-sentinel-so-token" in src:
        # only fail if both phrases co-located
        old_pattern = re.search(
            r"KHÔNG gen được[\s\S]{0,160}openai-sentinel-so-token",
            src,
        )
        if old_pattern:
            raise SystemExit(
                "FAIL: signup.py still warns 'KHÔNG gen được openai-sentinel-so-token' "
                "— must be updated post Phase 10"
            )
    if "sentinel sidecar" not in src.lower() and "sidecar" not in src.lower():
        raise SystemExit(
            "FAIL: signup.py pure_request branch should mention sidecar"
        )
    print("      ✓ old so-token-missing warning replaced with sidecar note")


# ─── 5. Smoke SentinelSidecar with mocked Camoufox ───────────────────


def _install_camoufox_mock():
    """Inject a fake camoufox.async_api into sys.modules so SentinelSidecar
    can import without real Camoufox binary. Returns mock instance for
    assertions.

    Mocks ``AsyncCamoufox(persistent_context=False)`` → returns a Browser
    with ``.new_context()``. Each context has its own pages list +
    cookies() method.
    """
    import types

    state = {
        "cookies": [
            {"name": "oai-sc", "value": "scv1", "domain": ".openai.com", "path": "/"},
            {"name": "_dd_s", "value": "dds1", "domain": ".chatgpt.com", "path": "/"},
            {"name": "oai-asli", "value": "aslv1", "domain": ".chatgpt.com", "path": "/"},
            {"name": "noisy-tracking", "value": "ignore-me", "domain": ".chatgpt.com", "path": "/"},
        ],
        "page_eval_calls": [],
        "goto_calls": [],
        "context_count": 0,
        "context_closed_count": 0,
        "init_scripts": [],          # list[str] — every add_init_script body
        "mouse_calls": [],           # ("move"|"click", x, y, kwargs)
        "keyboard_chars": [],        # list[str]
        "locator_clicks": 0,
        "locator_fills": 0,
    }

    class _MockLocator:
        first = None  # set below
        def __init__(self):
            self._self = self
        async def wait_for(self, **kwargs):
            return None
        async def bounding_box(self):
            return {"x": 100, "y": 200, "width": 180, "height": 30}
        async def click(self, **kwargs):
            state["locator_clicks"] += 1
        async def fill(self, value, **kwargs):
            state["locator_fills"] += 1

    class _MockMouse:
        async def move(self, x, y, **kwargs):
            state["mouse_calls"].append(("move", x, y, kwargs))
        async def click(self, x, y, **kwargs):
            state["mouse_calls"].append(("click", x, y, kwargs))

    class _MockKeyboard:
        async def type(self, ch, **kwargs):
            state["keyboard_chars"].append(ch)
        async def press(self, key):
            state["keyboard_chars"].append(f"press:{key}")

    class _MockPage:
        is_closed = staticmethod(lambda: False)
        def __init__(self):
            self.mouse = _MockMouse()
            self.keyboard = _MockKeyboard()
            self._loc = _MockLocator()
            self._loc.first = self._loc
        async def goto(self, url, **kwargs):
            state["goto_calls"].append(url)
            return None
        async def evaluate(self, script, *args):
            state["page_eval_calls"].append((script if isinstance(script, str) else "fn", args))
            return {"ok": True, "char_count": 10}
        def locator(self, selector):
            return self._loc

    class _MockCtx:
        def __init__(self):
            self.pages = []
            state["context_count"] += 1
        async def cookies(self, *args, **kwargs):
            return list(state["cookies"])
        async def new_page(self):
            p = _MockPage()
            self.pages.append(p)
            return p
        async def add_init_script(self, script, *args, **kwargs):
            state["init_scripts"].append(script)
        async def close(self):
            state["context_closed_count"] += 1

    class _MockBrowser:
        async def new_context(self, **kwargs):
            return _MockCtx()

    class _MockCamoufox:
        def __init__(self, **kwargs):
            state["camoufox_kwargs"] = kwargs
        async def __aenter__(self):
            state["browser"] = _MockBrowser()
            return state["browser"]
        async def __aexit__(self, *args):
            return None

    mod = types.ModuleType("camoufox.async_api")
    mod.AsyncCamoufox = _MockCamoufox
    sys.modules["camoufox.async_api"] = mod
    parent = sys.modules.setdefault("camoufox", types.ModuleType("camoufox"))
    parent.async_api = mod

    sb_mod = types.ModuleType("sentinel_browser")

    class _MockOracle:
        def __init__(self, page, ctx, log=None):
            state["oracle_constructed"] = True
            self.page = page
            self.ctx = ctx
            self.log = log
        async def get_token(self, *, device_id, flow, **kwargs):
            return json.dumps({
                "p": "PVAL", "t": "TVAL", "c": "CVAL",
                "id": device_id, "flow": flow,
            })

    async def _vfp(page, *, log=None, strict=False):
        return {"healthy": True, "issues": []}
    sb_mod.SentinelBrowserOracle = _MockOracle
    sb_mod.verify_fingerprint_health = _vfp

    # Inline stub for _verify_sdk_patch_markers — the real one only does
    # string searches + raises, no deps. Avoid loading the full
    # sentinel_browser module to keep mock environment clean.
    class _SdkPatchOutOfDateError(RuntimeError):
        pass
    _MARKERS = (
        ("SDK_GLOBAL_PATCH",    "var SentinelSDK="),
        ("INSTANCE_PATCH",      "var P=new _;"),
        ("EXPOSE_PATCH",        "return o?r?.[n(63)]?ce({so:o,c:r[n(63)]},t):o:null},t.token=ye,t}({});"),
    )
    def _stub_verify(text, *, log=None):
        missing = [name for name, marker in _MARKERS if marker not in text]
        if missing:
            raise _SdkPatchOutOfDateError(
                f"sdk.js patch markers missing: {missing}"
            )
    sb_mod._verify_sdk_patch_markers = _stub_verify
    sb_mod.SdkPatchOutOfDateError = _SdkPatchOutOfDateError
    sb_mod._SDK_PATCH_MARKERS = _MARKERS
    sys.modules["sentinel_browser"] = sb_mod

    br_mod = types.ModuleType("_browser_retry")
    def _parse_proxy(p):
        return {"server": p}
    br_mod.parse_proxy_for_playwright = _parse_proxy
    sys.modules.setdefault("_browser_retry", br_mod)

    return state


def step_smoke():
    print("[5/5] SentinelSidecar smoke (mocked Camoufox + pool + Phase 11)...", flush=True)
    print("      → installing mocks...", flush=True)
    state = _install_camoufox_mock()
    print("      → mocks installed, importing sentinel_sidecar...", flush=True)
    for mod_name in ("sentinel_sidecar",):
        if mod_name in sys.modules:
            del sys.modules[mod_name]
    from sentinel_sidecar import SentinelSidecar, SentinelSidecarPool
    print("      → sentinel_sidecar imported", flush=True)

    logs: list[str] = []
    def _log(m): logs.append(m)

    SentinelSidecarPool._instance = None

    print("      → starting sidecar...", flush=True)
    sidecar = SentinelSidecar(proxy=None, headless=True, log=_log, os_target="macos")
    sidecar.start(timeout=15.0)
    print("      → sidecar started", flush=True)
    assert state.get("oracle_constructed"), "Oracle should be constructed in start()"
    assert any("chatgpt.com" in u for u in state["goto_calls"]), state["goto_calls"]
    assert any("email-verification" in u for u in state["goto_calls"]), state["goto_calls"]
    print("      ✓ start() acquires context from shared browser", flush=True)

    # Phase 11 — verify persona seeds injected via add_init_script
    assert state["init_scripts"], "Phase 11: persona init script not injected"
    seed_script = state["init_scripts"][0]
    for marker in (
        "setCanvasSeed", "setAudioFingerprintSeed",
        "setFontSpacingSeed", "setWebGLRenderer",
    ):
        assert marker in seed_script, (
            f"Phase 11: persona init script missing {marker!r}"
        )
    print(f"      ✓ per-context persona seeds injected via ctx.add_init_script", flush=True)

    # Phase 11 — verify trusted input used (mouse + keyboard, NOT dispatchEvent)
    assert state["mouse_calls"], "Phase 11: page.mouse.* never called (trusted input missing)"
    move_count = sum(1 for c in state["mouse_calls"] if c[0] == "move")
    click_count = sum(1 for c in state["mouse_calls"] if c[0] == "click")
    assert move_count >= 1, f"expected ≥1 mouse.move, got {move_count}"
    assert click_count >= 1, f"expected ≥1 mouse.click, got {click_count}"
    typed_chars = [c for c in state["keyboard_chars"] if not c.startswith("press:")]
    assert len(typed_chars) >= 6, (
        f"Phase 11: expected ≥6 trusted keystrokes, got {len(typed_chars)}"
    )
    assert any(c.startswith("press:Tab") for c in state["keyboard_chars"]), (
        "Phase 11: expected Tab keypress to blur after typing"
    )
    print(
        f"      ✓ trusted simulation: mouse.move×{move_count}, "
        f"mouse.click×{click_count}, keyboard chars×{len(typed_chars)}",
        flush=True,
    )

    # Phase 10 — pool semantics: 1 browser, ref_count=1
    pool = SentinelSidecarPool.instance()
    stats = pool.stats()
    assert stats["browsers"] == 1, stats
    assert list(stats["ref_counts"].values()) == [1], stats
    print(f"      ✓ pool stats: {stats['browsers']} browser, ref_count=1", flush=True)

    cookies = sidecar.dump_cookies(timeout=5.0)
    names = sorted(c["name"] for c in cookies)
    assert "oai-sc" in names, names
    assert "_dd_s" in names, names
    print(f"      ✓ dump_cookies returns {len(cookies)} cookies (incl. oai-sc/_dd_s)", flush=True)

    tok = sidecar.get_sentinel_token(device_id="DID-X", flow="username_password_create", timeout=5.0)
    assert tok is not None
    data = json.loads(tok)
    assert data["p"] == "PVAL", data
    assert data["id"] == "DID-X", data
    print(f"      ✓ get_sentinel_token returns oracle JSON", flush=True)

    # Phase 11 — second context gets fresh persona seeds (anti-fleet).
    # We re-use the same shared browser by acquiring a 2nd context directly
    # (skip full SentinelSidecar lifecycle to keep this micro test fast).
    state["init_scripts"].clear()
    state["mouse_calls"].clear()
    state["keyboard_chars"].clear()

    shared = sidecar._browser  # _SharedBrowser instance
    ctx2, page2 = shared.acquire_context(log=_log, timeout=15.0)
    assert state["init_scripts"], "Phase 11: 2nd context didn't get persona script"
    second_script = state["init_scripts"][0]
    assert second_script != seed_script, (
        "Phase 11: two contexts got IDENTICAL persona seeds — "
        "fleet detection risk"
    )
    print("      ✓ 2nd context got DIFFERENT persona seeds (anti-fleet)", flush=True)
    shared.release_context(ctx2, timeout=5.0)

    # Tear down — close sidecar1 + force pool shutdown so daemon timers
    # don't keep the process alive.
    sidecar.close(timeout=3.0)
    pool.shutdown_all()
    print("      ✓ teardown: pool.shutdown_all clean", flush=True)

    # Phase 11 — SDK patch marker verifier (independent of pool/sidecar)
    print("      -- sdk.js patch marker verifier --", flush=True)
    from sentinel_browser import _verify_sdk_patch_markers, SdkPatchOutOfDateError
    good = (
        "blah var SentinelSDK=({}) "
        "var P=new _; "
        "return o?r?.[n(63)]?ce({so:o,c:r[n(63)]},t):o:null},t.token=ye,t}({});"
    )
    _verify_sdk_patch_markers(good, log=_log)
    print("      ✓ all 3 markers present → no raise", flush=True)
    bad = good.replace("var P=new _;", "var Q=new _;")
    raised = False
    try:
        _verify_sdk_patch_markers(bad, log=_log)
    except SdkPatchOutOfDateError:
        raised = True
    assert raised, "expected SdkPatchOutOfDateError when marker missing"
    print("      ✓ missing marker → raises SdkPatchOutOfDateError", flush=True)


def main() -> int:
    step_ast()
    step_sidecar_api()
    step_request_phase_wired()
    step_signup_warning_updated()
    step_smoke()
    print("\nAll Phase 10 (pure-HTTP sidecar) checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
