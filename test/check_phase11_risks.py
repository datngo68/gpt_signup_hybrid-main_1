"""Phase 11 — verify 4 specific risks reported by user are addressed.

Risks (from user message):
  1. isTrusted=false for JS dispatchEvent → server reject so-token.
     FIX: use page.mouse.click() + page.keyboard.type() (Playwright CDP).
  2. fingerprint_preset=True requires Camoufox v149+ — older binary
     silently ignores or raises.
     FIX: remove fingerprint_preset; persona seeds via add_init_script.
  3. Cold-start ~10s per proxy key. 30 workers × 30 proxies = 300s
     warm-up if sequential.
     FIX: pool.acquire releases dict-lock BEFORE start(), so different
     keys launch in parallel (~10s max wall-clock).
  4. AsyncCamoufox + persistent_context=False — all contexts in same
     Browser process share UA/canvas/WebGL fingerprint. Risk: server
     detects 'fleet of bots' from identical fingerprints.
     FIX: per-context add_init_script seeds canvas/audio/font/WebGL
     with fresh random ints per signup.

Run: python3 test/check_phase11_risks.py
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"OK:   {msg}")


# ─── Risk 1: trusted events ──────────────────────────────────────────


def check_risk_1_trusted_events() -> None:
    print("\n── Risk 1: isTrusted=true via Playwright CDP ──")
    sc = (ROOT / "sentinel_sidecar.py").read_text()

    # No dispatchEvent in real code (only in comments and as a NEGATIVE
    # reference inside the docstring).
    src_no_strings = _strip_strings_and_comments(sc)
    if "dispatchEvent" in src_no_strings:
        fail("dispatchEvent still in sentinel_sidecar code")
    ok("no dispatchEvent in sentinel_sidecar code (only in docstring)")

    # Must use page.mouse.* + page.keyboard.* in _simulate_trusted_input
    for marker in ("page.mouse.move", "page.mouse.click", "page.keyboard.type"):
        if marker not in sc:
            fail(f"missing trusted-input call: {marker}")
        ok(f"trusted-input call present: {marker}")

    # K2 form interaction must also use trusted primitives — _ht is
    # imported from _human_input which uses Playwright keyboard.type.
    if "from _human_input import human_type" not in sc:
        fail("sentinel_sidecar: human_type not imported (no trusted typing in K2)")
    ok("K2 uses _human_input.human_type (per-char trusted keystrokes)")

    # browser_phase password_create branch — must use human_type, NOT
    # page.evaluate(dispatchEvent).
    bp = (ROOT / "browser_phase.py").read_text()
    bp_clean = _strip_strings_and_comments(bp)
    if "dispatchEvent" in bp_clean:
        fail("browser_phase still has dispatchEvent in code")
    ok("browser_phase has no dispatchEvent in code")
    if "human_type" not in bp:
        fail("browser_phase: human_type not used")
    ok("browser_phase uses human_type for password input")


# ─── Risk 2: fingerprint_preset removal ──────────────────────────────


def check_risk_2_fingerprint_preset() -> None:
    print("\n── Risk 2: fingerprint_preset NOT used in active code ──")
    for fname in ("browser_phase.py", "session_phase.py", "sentinel_sidecar.py"):
        src = (ROOT / fname).read_text()
        # Strip comments/strings before checking
        clean = _strip_strings_and_comments(src)
        # Look for `fingerprint_preset=True` (as a real kwarg, not in
        # string/comment).
        if re.search(r"\bfingerprint_preset\s*=\s*True\b", clean):
            fail(f"{fname}: fingerprint_preset=True still in active code")
        ok(f"{fname}: no active fingerprint_preset=True")


# ─── Risk 3: pool launches in parallel (start lock idempotent) ──────


def check_risk_3_parallel_warmup() -> None:
    print("\n── Risk 3: pool.acquire releases lock BEFORE start() ──")
    sc = (ROOT / "sentinel_sidecar.py").read_text()

    # Use AST to find the With(self._lock) node inside SentinelSidecarPool.acquire
    # and verify the br.start() call is NOT a descendant of that With block.
    tree = ast.parse(sc)
    acquire_fn: ast.AST | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "SentinelSidecarPool":
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name == "acquire":
                    acquire_fn = child
                    break
            break
    if acquire_fn is None:
        fail("could not locate SentinelSidecarPool.acquire via AST")

    # Find the With node whose context expr is self._lock
    lock_with: ast.With | None = None
    for stmt in acquire_fn.body:
        if isinstance(stmt, ast.With):
            for item in stmt.items:
                cm = item.context_expr
                if (
                    isinstance(cm, ast.Attribute)
                    and cm.attr == "_lock"
                    and isinstance(cm.value, ast.Name)
                    and cm.value.id == "self"
                ):
                    lock_with = stmt
                    break
            if lock_with is not None:
                break
    if lock_with is None:
        fail("acquire() has no `with self._lock:` block at top level")

    # Find ALL br.start() Call nodes in acquire_fn
    start_calls: list[ast.Call] = []
    for n in ast.walk(acquire_fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            if n.func.attr == "start" and isinstance(n.func.value, ast.Name) and n.func.value.id == "br":
                start_calls.append(n)
    if not start_calls:
        fail("acquire() never calls br.start()")

    # Gather all descendants of lock_with
    inside_lock = set(id(n) for n in ast.walk(lock_with))
    for call in start_calls:
        if id(call) in inside_lock:
            fail(
                f"br.start() at line {call.lineno} is INSIDE `with self._lock:` "
                "— pool serializes launches, defeating parallelism"
            )
    ok("pool.acquire: br.start() runs OUTSIDE `with self._lock:`")

    # Verify _SharedBrowser.start is idempotent via _start_lock
    src_no_comments = _strip_strings_and_comments(sc)
    if "_start_lock" not in src_no_comments:
        fail("_SharedBrowser missing _start_lock — concurrent start() races")
    ok("_SharedBrowser._start_lock present (idempotent start)")
    # And start() must guard with `with self._start_lock:`
    if "with self._start_lock:" not in src_no_comments:
        fail("_SharedBrowser.start() missing `with self._start_lock:` guard")
    ok("_SharedBrowser.start() uses `with self._start_lock:` guard")


# ─── Risk 4: per-context persona rotation ───────────────────────────


def check_risk_4_persona_rotation() -> None:
    print("\n── Risk 4: per-context persona rotation in shared Browser ──")
    sc = (ROOT / "sentinel_sidecar.py").read_text()

    # The functions must exist
    for func in ("_new_persona_seeds", "_build_persona_init_script"):
        if f"def {func}" not in sc:
            fail(f"sentinel_sidecar missing {func}()")
        ok(f"sentinel_sidecar has {func}()")

    # The seeds must include all 4 fingerprint vectors
    seeds_fn = _extract_function_source(sc, "_new_persona_seeds")
    for k in ("canvas", "audio", "fontSpacing", "webglVendor", "webglRenderer"):
        if f'"{k}"' not in seeds_fn:
            fail(f"_new_persona_seeds doesn't seed {k}")
        ok(f"_new_persona_seeds rotates {k}")

    # The init script must call the 5 Camoufox setters
    script_fn = _extract_function_source(sc, "_build_persona_init_script")
    for setter in (
        "setCanvasSeed",
        "setAudioFingerprintSeed",
        "setFontSpacingSeed",
        "setWebGLVendor",
        "setWebGLRenderer",
    ):
        if setter not in script_fn:
            fail(f"_build_persona_init_script doesn't call {setter}")
        ok(f"persona init script calls {setter}")

    # acquire_context must call ctx.add_init_script with persona script
    # BEFORE creating any page.
    acquire_src = _extract_function_source(sc, "acquire_context")
    if not acquire_src:
        # nested async function _create — fall back to fuzzy search
        acquire_src = sc
    # Look for the persona injection pattern
    if "_new_persona_seeds()" not in acquire_src or "_build_persona_init_script" not in acquire_src:
        fail("acquire_context doesn't inject persona seeds")
    ok("acquire_context injects fresh persona seeds per signup")

    # Verify the injection happens BEFORE the first page.goto chatgpt.com.
    # Heuristic: index of 'add_init_script(_build_persona_init_script' must
    # be BEFORE first 'page.goto(' OR 'new_page(' call.
    persona_pos = sc.find("_build_persona_init_script(seeds)")
    new_page_pos = sc.find("await ctx.new_page()")
    if persona_pos < 0 or new_page_pos < 0:
        fail("cannot locate persona vs new_page positions")
    if persona_pos > new_page_pos:
        fail(
            f"persona init_script (pos={persona_pos}) registered AFTER "
            f"new_page() (pos={new_page_pos}) — first page loads w/o rotated FP"
        )
    ok("persona init_script registered BEFORE first new_page()")


# ─── helpers ────────────────────────────────────────────────────────


def _strip_strings_and_comments(src: str) -> str:
    """Remove strings and comments from Python source so we can grep for
    code patterns without false matches in docstrings/comments."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    # Strip docstrings by walking and zeroing them out
    lines = src.splitlines()
    out_lines = list(lines)
    # Remove # comments
    for i, l in enumerate(out_lines):
        # Strip trailing comments but keep '#' inside strings.
        # Simple heuristic — split at first '#' outside string.
        s = []
        in_s = None
        for ch in l:
            if in_s:
                if ch == in_s:
                    in_s = None
                s.append(ch)
            elif ch in ("'", '"'):
                in_s = ch
                s.append(ch)
            elif ch == "#":
                break
            else:
                s.append(ch)
        out_lines[i] = "".join(s)
    cleaned = "\n".join(out_lines)
    # Drop triple-quoted strings (docstrings)
    cleaned = re.sub(r'"""[\s\S]*?"""', '""', cleaned)
    cleaned = re.sub(r"'''[\s\S]*?'''", "''", cleaned)
    # Drop single-line strings (best-effort)
    cleaned = re.sub(r'"[^"\n\\]*"', '""', cleaned)
    cleaned = re.sub(r"'[^'\n\\]*'", "''", cleaned)
    return cleaned


def _extract_function_source(src: str, name: str) -> str:
    """Extract the source lines of a function or method by name. Returns
    empty string if not found."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return ""
    lines = src.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            start = node.lineno - 1
            end = node.end_lineno
            return "\n".join(lines[start:end])
    return ""


def main() -> int:
    check_risk_1_trusted_events()
    check_risk_2_fingerprint_preset()
    check_risk_3_parallel_warmup()
    check_risk_4_persona_rotation()
    print("\nAll 4 risks addressed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
