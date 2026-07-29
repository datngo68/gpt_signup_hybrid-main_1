"""Verify Phase 9 (anti-ban headless hardening):

  1. AST parse all 3 changed files OK.
  2. sentinel_browser.verify_fingerprint_health exists with async signature.
  3. browser_phase.py: AsyncCamoufox launch has ``fingerprint_preset=True``
     AND fingerprint health probe is called after goto chatgpt.com.
  4. session_phase.py: same as above + probe runs BEFORE
     SentinelBrowserOracle is constructed.
  5. Smoke verify_fingerprint_health with mocked page.evaluate:
     - healthy snapshot (real Firefox values) → healthy=True, issues=[]
     - empty WebGL → healthy=False, issues contains webgl_vendor_empty
     - canvas too short → contains canvas_too_short
     - audio missing → contains audio_context_missing
     - webdriver=True → contains navigator_webdriver_true
     - strict=True + degraded → raises RuntimeError
"""
from __future__ import annotations

import ast
import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def step_ast() -> None:
    print("[1/5] AST parse changed files...")
    for name in ("sentinel_browser.py", "browser_phase.py", "session_phase.py"):
        p = ROOT / name
        ast.parse(p.read_text(), filename=str(p))
        print(f"      ✓ {name}")


def step_helper_signature() -> None:
    print("[2/5] verify_fingerprint_health helper signature...")
    src = (ROOT / "sentinel_browser.py").read_text()
    tree = ast.parse(src)
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.AsyncFunctionDef)
         and n.name == "verify_fingerprint_health"),
        None,
    )
    if fn is None:
        raise SystemExit("FAIL: async verify_fingerprint_health missing")
    arg_names = [a.arg for a in fn.args.args]
    kwonly = [a.arg for a in fn.args.kwonlyargs]
    if "page" not in arg_names:
        raise SystemExit(f"FAIL: missing positional `page`, got {arg_names}")
    if "log" not in kwonly:
        raise SystemExit(f"FAIL: missing kw `log`, got {kwonly}")
    if "strict" not in kwonly:
        raise SystemExit(f"FAIL: missing kw `strict`, got {kwonly}")
    print(f"      ✓ async def verify_fingerprint_health(page, *, log, strict)")

    body = ast.unparse(fn)
    for marker in (
        "webgl_vendor",
        "webgl_renderer",
        "canvas_length",
        "audio_context",
        "hardware_concurrency",
        "navigator_webdriver_true",
        "issues",
        "healthy",
    ):
        if marker not in body:
            raise SystemExit(f"FAIL: helper body missing marker {marker!r}")
    print(f"      ✓ helper covers webgl/canvas/audio/navigator/webdriver")


def _has_active_fingerprint_preset(src: str) -> bool:
    """True iff ``fingerprint_preset=True`` appears as a real kwarg (not
    inside a string literal or comment). Strip comments per-line + skip
    triple-quoted docstrings via a simple state machine.
    """
    in_triple = False
    quote = None
    for line in src.splitlines():
        # toggle triple-quoted blocks
        if not in_triple:
            for q in ('"""', "'''"):
                if q in line:
                    cnt = line.count(q)
                    if cnt % 2 == 1:
                        in_triple = True
                        quote = q
                        break
            if in_triple:
                continue
        else:
            if quote in line:
                in_triple = False
                quote = None
            continue
        # strip line comments
        idx_hash = line.find("#")
        if idx_hash >= 0:
            line = line[:idx_hash]
        # also strip inline backtick docs (markdown-ish comments)
        if "``" in line:
            line = re.sub(r"``[^`]*``", "", line)
        if re.search(r"\bfingerprint_preset\s*=\s*True\b", line):
            return True
    return False


def step_browser_phase_wired() -> None:
    print("[3/5] browser_phase.py wired...")
    src = (ROOT / "browser_phase.py").read_text()
    if _has_active_fingerprint_preset(src):
        raise SystemExit(
            "FAIL: browser_phase still passes fingerprint_preset=True (active code). "
            "Phase 11.1 removed it; per-context add_init_script seeds replace it."
        )
    print("      ✓ no active fingerprint_preset=True kwarg (Phase 11.1 removed)")

    if "verify_fingerprint_health" not in src:
        raise SystemExit("FAIL: browser_phase doesn't import verify_fingerprint_health")
    call_count = src.count("await _vfp(page, log=log)")
    if call_count < 1:
        raise SystemExit(
            f"FAIL: browser_phase verify_fingerprint_health not called "
            f"(found {call_count} calls of _vfp pattern)"
        )
    print(f"      ✓ verify_fingerprint_health called {call_count}× after chatgpt.com goto")


def step_session_phase_wired() -> None:
    print("[4/5] session_phase.py wired...")
    src = (ROOT / "session_phase.py").read_text()
    if _has_active_fingerprint_preset(src):
        raise SystemExit(
            "FAIL: session_phase still passes fingerprint_preset=True (active code)"
        )
    print("      ✓ no active fingerprint_preset=True kwarg (Phase 11.1 removed)")

    if "verify_fingerprint_health" not in src:
        raise SystemExit("FAIL: session_phase doesn't import verify_fingerprint_health")
    if "_verify_fp(page, log=log)" not in src:
        raise SystemExit("FAIL: session_phase doesn't call verify probe with alias _verify_fp")
    idx_probe = src.find("await _verify_fp(page, log=log)")
    idx_oracle = src.find("sentinel_oracle = _SentinelOracle")
    if idx_probe == -1 or idx_oracle == -1:
        raise SystemExit("FAIL: session_phase probe/oracle markers missing")
    if idx_probe >= idx_oracle:
        raise SystemExit(
            "FAIL: verify_fingerprint_health must run BEFORE SentinelBrowserOracle"
        )
    print(f"      ✓ probe runs before oracle construction (idx probe={idx_probe} < oracle={idx_oracle})")


# ─── Smoke verify_fingerprint_health with mock page ──────────────────


def _healthy_snapshot() -> dict:
    return {
        "webgl_vendor": "Mozilla",
        "webgl_renderer": "Mozilla -- ANGLE (Apple, Apple M1 Pro, OpenGL 4.1)",
        "canvas_data_url": "data:image/png;base64," + "A" * 800,
        "canvas_length": 850,
        "audio_context": True,
        "audio_sample_rate": 44100,
        "plugins_count": 5,
        "languages": ["en-US", "en"],
        "user_agent": "Mozilla/5.0 ...",
        "platform": "MacIntel",
        "hardware_concurrency": 10,
        "device_memory": 8,
        "webdriver": False,
    }


class _MockPage:
    def __init__(self, snapshot):
        self._snapshot = snapshot
        self.calls = 0

    async def evaluate(self, script):
        self.calls += 1
        return self._snapshot


async def _smoke():
    from sentinel_browser import verify_fingerprint_health

    logs: list[str] = []
    def _log(m): logs.append(m)

    # Healthy
    snap = await verify_fingerprint_health(
        _MockPage(_healthy_snapshot()), log=_log,
    )
    assert snap["healthy"] is True, f"expected healthy, got issues={snap.get('issues')}"
    assert snap["issues"] == [], snap["issues"]
    assert any("OK webgl=" in m for m in logs), logs

    # Empty WebGL
    bad = _healthy_snapshot()
    bad["webgl_vendor"] = ""
    bad["webgl_renderer"] = ""
    logs.clear()
    snap = await verify_fingerprint_health(_MockPage(bad), log=_log)
    assert snap["healthy"] is False
    assert "webgl_vendor_empty" in snap["issues"]
    assert "webgl_renderer_empty" in snap["issues"]

    # Canvas too short
    bad = _healthy_snapshot()
    bad["canvas_length"] = 100
    snap = await verify_fingerprint_health(_MockPage(bad), log=_log)
    assert "canvas_too_short:100" in snap["issues"], snap["issues"]

    # Audio missing
    bad = _healthy_snapshot()
    bad["audio_context"] = False
    snap = await verify_fingerprint_health(_MockPage(bad), log=_log)
    assert "audio_context_missing" in snap["issues"]

    # hardware_concurrency=0
    bad = _healthy_snapshot()
    bad["hardware_concurrency"] = 0
    snap = await verify_fingerprint_health(_MockPage(bad), log=_log)
    assert "hardware_concurrency_zero" in snap["issues"]

    # webdriver=True
    bad = _healthy_snapshot()
    bad["webdriver"] = True
    snap = await verify_fingerprint_health(_MockPage(bad), log=_log)
    assert "navigator_webdriver_true" in snap["issues"]

    # strict mode raises on degraded
    bad = _healthy_snapshot()
    bad["webgl_vendor"] = ""
    raised = False
    try:
        await verify_fingerprint_health(_MockPage(bad), log=_log, strict=True)
    except RuntimeError:
        raised = True
    assert raised, "expected RuntimeError in strict mode"

    # probe exception path → returns {healthy: False, issues: ["probe_exception:..."]}
    class _RaisingPage:
        async def evaluate(self, script):
            raise RuntimeError("simulated")
    snap = await verify_fingerprint_health(_RaisingPage(), log=_log)
    assert snap.get("healthy") is False
    assert any(i.startswith("probe_exception") for i in snap.get("issues", []))


def step_smoke():
    print("[5/5] Smoke verify_fingerprint_health...")
    asyncio.run(_smoke())
    print("      ✓ healthy snapshot → healthy=True")
    print("      ✓ empty WebGL → webgl_*_empty")
    print("      ✓ canvas_length<300 → canvas_too_short")
    print("      ✓ audio_context=False → audio_context_missing")
    print("      ✓ hardware_concurrency=0 → hardware_concurrency_zero")
    print("      ✓ webdriver=True → navigator_webdriver_true")
    print("      ✓ strict=True + degraded → RuntimeError")
    print("      ✓ probe exception → healthy=False with probe_exception")


def main() -> int:
    step_ast()
    step_helper_signature()
    step_browser_phase_wired()
    step_session_phase_wired()
    step_smoke()
    print("\nAll Phase 9 (headless hardening) checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
