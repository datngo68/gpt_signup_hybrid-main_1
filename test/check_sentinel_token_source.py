"""Verify P3 fix: page-native sentinel-token (SentinelBrowserOracle).

Checks:
  1. AST parse all 4 changed files OK.
  2. ``sentinel_browser.SentinelBrowserOracle`` API:
     - async ``get_token(*, device_id, flow, log_prefix)``
     - constructor takes (page, ctx, log)
  3. ``openai_sentinel_in_page.js`` has __runSentinelInPage + correct patches.
  4. ``request_phase._get_sentinel_token_async`` signature + body order:
     - browser_oracle kwarg present
     - prefer oracle: log marker contains "page-native"
     - fallback QuickJS via asyncio.to_thread
  5. ``session_phase`` uses _get_sentinel_token_async (no sync _get_sentinel_token
     calls in _drive_session_flow body).
  6. Smoke test SentinelBrowserOracle with mock page+ctx:
     - happy path: page.evaluate × 2 + ctx.request.post → assembled JSON token
     - page.evaluate raise → returns None
     - HTTP /sentinel/req status != 200 → returns None
     - empty request_p → returns None
"""
from __future__ import annotations

import ast
import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ─── 1. AST parse ─────────────────────────────────────────────────────


def _ast_parse(label: str, path: Path) -> ast.AST:
    src = path.read_text()
    return ast.parse(src, filename=str(path))


def step_ast() -> None:
    print("[1/6] AST parse changed files...")
    for name in (
        "sentinel_browser.py",
        "request_phase.py",
        "session_phase.py",
    ):
        p = ROOT / name
        if not p.exists():
            raise SystemExit(f"FAIL: missing {p}")
        _ast_parse(name, p)
        print(f"      ✓ {name}")
    js = ROOT / "openai_sentinel_in_page.js"
    if not js.exists():
        raise SystemExit("FAIL: missing openai_sentinel_in_page.js")
    print(f"      ✓ openai_sentinel_in_page.js exists ({js.stat().st_size} bytes)")


# ─── 2. sentinel_browser.SentinelBrowserOracle API ───────────────────


def step_oracle_api() -> None:
    print("[2/6] SentinelBrowserOracle API...")
    src = (ROOT / "sentinel_browser.py").read_text()
    tree = ast.parse(src)
    cls = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "SentinelBrowserOracle":
            cls = node
            break
    if cls is None:
        raise SystemExit("FAIL: SentinelBrowserOracle class missing")

    # __init__ params
    init = next(
        (m for m in cls.body if isinstance(m, ast.FunctionDef) and m.name == "__init__"),
        None,
    )
    if init is None:
        raise SystemExit("FAIL: __init__ missing")
    init_args = [a.arg for a in init.args.args]
    for required in ("self", "page", "ctx"):
        if required not in init_args:
            raise SystemExit(f"FAIL: __init__ missing arg {required!r}, got {init_args}")
    print(f"      ✓ __init__({', '.join(init_args)})")

    # get_token: async def
    gt = next(
        (m for m in cls.body if isinstance(m, ast.AsyncFunctionDef) and m.name == "get_token"),
        None,
    )
    if gt is None:
        raise SystemExit("FAIL: async def get_token missing")
    kwonly = [a.arg for a in gt.args.kwonlyargs]
    for required in ("device_id", "flow"):
        if required not in kwonly:
            raise SystemExit(f"FAIL: get_token missing kw {required!r}, got {kwonly}")
    print(f"      ✓ async get_token(*, {', '.join(kwonly)})")


# ─── 3. in-page script patches ───────────────────────────────────────


def step_in_page_script() -> None:
    print("[3/6] openai_sentinel_in_page.js content...")
    src = (ROOT / "openai_sentinel_in_page.js").read_text()
    required_markers = [
        "__runSentinelInPage",
        "SDK_GLOBAL_PATCH",
        "INSTANCE_PATCH",
        "EXPOSE_PATCH",
        "globalThis.__debugP",
        "globalThis.SentinelSDK",
        "getRequirementsToken",
        "getEnforcementToken",
        "__debug_bindProof",
        "__debug_n",
    ]
    for m in required_markers:
        if m not in src:
            raise SystemExit(f"FAIL: in-page script missing marker {m!r}")
    print(f"      ✓ all {len(required_markers)} markers present")
    # Must NOT mock window/document (would defeat real browser env)
    forbidden = [
        "globalThis.window =",
        "globalThis.document =",
        "globalThis.navigator =",
    ]
    for f in forbidden:
        if f in src:
            raise SystemExit(
                f"FAIL: in-page script MUST NOT override {f!r} "
                "(would mask real browser fingerprints)"
            )
    print(f"      ✓ no real-env overrides (canvas/WebGL/audio uses real browser)")


# ─── 4. request_phase._get_sentinel_token_async ───────────────────────


def step_request_phase_async() -> None:
    print("[4/6] request_phase._get_sentinel_token_async...")
    src = (ROOT / "request_phase.py").read_text()
    tree = ast.parse(src)
    fn = next(
        (
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
            and n.name == "_get_sentinel_token_async"
        ),
        None,
    )
    if fn is None:
        raise SystemExit("FAIL: async _get_sentinel_token_async missing")

    kwonly = [a.arg for a in fn.args.kwonlyargs]
    if "browser_oracle" not in kwonly:
        raise SystemExit(f"FAIL: browser_oracle kwarg missing, got {kwonly}")
    print(f"      ✓ async def signature has browser_oracle keyword")

    body_src = ast.unparse(fn)
    for marker in (
        "browser_oracle.get_token",
        "page-native",
        "asyncio.to_thread",
        "_get_sentinel_token",  # fallback sync call
    ):
        if marker not in body_src:
            raise SystemExit(f"FAIL: async body missing marker {marker!r}")
    print(f"      ✓ body: oracle.get_token → fallback asyncio.to_thread")

    # Order: oracle check must precede asyncio.to_thread
    idx_oracle = body_src.find("browser_oracle.get_token")
    idx_fallback = body_src.find("asyncio.to_thread")
    if idx_oracle == -1 or idx_fallback == -1 or idx_oracle >= idx_fallback:
        raise SystemExit("FAIL: oracle path must be checked BEFORE asyncio.to_thread fallback")
    print(f"      ✓ priority order correct (oracle first)")


# ─── 5. session_phase wired ──────────────────────────────────────────


def step_session_phase_wired() -> None:
    print("[5/6] session_phase wired to oracle + async helper...")
    src = (ROOT / "session_phase.py").read_text()
    if "SentinelBrowserOracle" not in src:
        raise SystemExit("FAIL: session_phase missing SentinelBrowserOracle import")
    if "sentinel_oracle = _SentinelOracle" not in src:
        raise SystemExit("FAIL: session_phase doesn't construct oracle")
    if "_get_sentinel_token_async" not in src:
        raise SystemExit("FAIL: session_phase doesn't import async helper")

    # Count remaining SYNC _get_sentinel_token CALLS (not import) — must be 0
    # in body of _drive_session_flow. Allow the import line + the async call.
    # Simple heuristic: grep for "_get_sentinel_token(" without preceding "_async".
    call_pattern = re.compile(r"\b_get_sentinel_token\(")
    found_sync_calls = []
    for line_no, line in enumerate(src.splitlines(), 1):
        # Skip pure import lines
        stripped = line.strip()
        if stripped.startswith("from ") or stripped.startswith("import "):
            continue
        # Skip async calls
        if "_get_sentinel_token_async" in line:
            continue
        if call_pattern.search(line):
            found_sync_calls.append((line_no, line.strip()))
    if found_sync_calls:
        print("FAIL: sync _get_sentinel_token() still called in session_phase:")
        for ln, txt in found_sync_calls:
            print(f"  line {ln}: {txt}")
        raise SystemExit(1)
    print(f"      ✓ no sync _get_sentinel_token() calls in session_phase")

    # Both async call sites must pass browser_oracle=sentinel_oracle
    async_call_matches = re.findall(
        r"_get_sentinel_token_async\([^)]*?browser_oracle\s*=\s*sentinel_oracle",
        src,
        re.DOTALL,
    )
    if len(async_call_matches) < 2:
        raise SystemExit(
            f"FAIL: expected ≥2 async calls with browser_oracle=sentinel_oracle, "
            f"got {len(async_call_matches)}"
        )
    print(f"      ✓ {len(async_call_matches)} async calls forward sentinel_oracle")


# ─── 6. Smoke test SentinelBrowserOracle (mock page/ctx) ──────────────


class _MockResponse:
    def __init__(self, status=200, body=None, json_body=None):
        self.status = status
        self._body = body
        self._json_body = json_body

    async def text(self):
        if self._body is None:
            return json.dumps(self._json_body or {})
        return self._body

    async def json(self):
        if self._json_body is not None:
            return self._json_body
        return json.loads(self._body or "{}")


class _MockRequest:
    """Mocks ctx.request — supports .get() and .post()."""

    def __init__(self):
        self.sdk_response = _MockResponse(
            status=200,
            # Include ALL 3 sdk.js patch markers so _verify_sdk_patch_markers
            # (Phase 11) doesn't raise SdkPatchOutOfDateError on the fake body.
            body=(
                "var SentinelSDK=({}); "
                "var P=new _; "
                "return o?r?.[n(63)]?ce({so:o,c:r[n(63)]},t):o:null},t.token=ye,t}({});"
            ),
        )
        self.challenge_response = _MockResponse(
            status=200,
            json_body={"token": "SERVER-CHALLENGE-TOKEN", "turnstile": {"dx": "DX-VAL"}},
        )
        self.calls = []

    async def get(self, url, headers=None):
        self.calls.append(("GET", url))
        return self.sdk_response

    async def post(self, url, data=None, headers=None):
        self.calls.append(("POST", url, data))
        return self.challenge_response


class _MockCtx:
    def __init__(self):
        self.request = _MockRequest()


class _MockPage:
    """Mocks page.evaluate. Returns dict per call based on payload.action."""

    def __init__(self, raise_on_action=None):
        self.calls = []
        self._raise_on_action = raise_on_action

    async def evaluate(self, script, args):
        self.calls.append((script, args))
        action = (args or {}).get("payload", {}).get("action")
        if self._raise_on_action == action:
            raise RuntimeError(f"page.evaluate forced fail on action={action}")
        if action == "requirements":
            return {"request_p": "REQUEST_P_FROM_PAGE"}
        if action == "solve":
            return {"final_p": "FINAL_P_FROM_PAGE", "t": "T_FROM_PAGE"}
        raise RuntimeError(f"unexpected action: {action}")


def _logs() -> tuple[list[str], object]:
    bucket: list[str] = []
    def _log(m):
        bucket.append(m)
    return bucket, _log


async def _smoke_happy() -> None:
    from sentinel_browser import SentinelBrowserOracle
    page = _MockPage()
    ctx = _MockCtx()
    logs, log = _logs()
    oracle = SentinelBrowserOracle(page=page, ctx=ctx, log=log)
    token = await oracle.get_token(device_id="DID-123", flow="login")
    assert token is not None, f"expected token, got None. logs={logs}"
    data = json.loads(token)
    assert data["p"] == "FINAL_P_FROM_PAGE", data
    assert data["t"] == "T_FROM_PAGE", data
    assert data["c"] == "SERVER-CHALLENGE-TOKEN", data
    assert data["id"] == "DID-123", data
    assert data["flow"] == "login", data
    # Verify call sequence: GET sdk.js, page.evaluate(requirements),
    # POST /sentinel/req, page.evaluate(solve)
    assert len(page.calls) == 2, f"expected 2 page.evaluate calls, got {len(page.calls)}"
    assert page.calls[0][1]["payload"]["action"] == "requirements"
    assert page.calls[1][1]["payload"]["action"] == "solve"
    assert page.calls[1][1]["payload"]["challenge"]["token"] == "SERVER-CHALLENGE-TOKEN"
    request_calls = [c[0] for c in ctx.request.calls]
    assert "GET" in request_calls, request_calls  # sdk.js
    assert "POST" in request_calls, request_calls  # /sentinel/req


async def _smoke_eval_fail() -> None:
    from sentinel_browser import SentinelBrowserOracle
    page = _MockPage(raise_on_action="requirements")
    ctx = _MockCtx()
    _logs_bucket, log = _logs()
    oracle = SentinelBrowserOracle(page=page, ctx=ctx, log=log)
    token = await oracle.get_token(device_id="X", flow="login")
    assert token is None, f"expected None when evaluate fails, got {token!r}"


async def _smoke_challenge_fail() -> None:
    from sentinel_browser import SentinelBrowserOracle
    page = _MockPage()
    ctx = _MockCtx()
    ctx.request.challenge_response = _MockResponse(status=500, body="server error")
    _logs_bucket, log = _logs()
    oracle = SentinelBrowserOracle(page=page, ctx=ctx, log=log)
    token = await oracle.get_token(device_id="X", flow="login")
    assert token is None, f"expected None when challenge HTTP 500, got {token!r}"


async def _smoke_empty_request_p() -> None:
    from sentinel_browser import SentinelBrowserOracle
    page = _MockPage()
    # Override evaluate to return empty request_p
    async def _eval_empty(script, args):
        action = args["payload"]["action"]
        if action == "requirements":
            return {"request_p": ""}
        return {"final_p": "x", "t": "y"}
    page.evaluate = _eval_empty
    ctx = _MockCtx()
    _logs_bucket, log = _logs()
    oracle = SentinelBrowserOracle(page=page, ctx=ctx, log=log)
    token = await oracle.get_token(device_id="X", flow="login")
    assert token is None, f"expected None when request_p empty, got {token!r}"


def step_smoke() -> None:
    print("[6/6] Smoke SentinelBrowserOracle mocks...")
    asyncio.run(_smoke_happy())
    print("      ✓ happy path: token assembled correctly")
    asyncio.run(_smoke_eval_fail())
    print("      ✓ page.evaluate fail → return None")
    asyncio.run(_smoke_challenge_fail())
    print("      ✓ /sentinel/req HTTP 500 → return None")
    asyncio.run(_smoke_empty_request_p())
    print("      ✓ empty request_p → return None")


def main() -> int:
    step_ast()
    step_oracle_api()
    step_in_page_script()
    step_request_phase_async()
    step_session_phase_wired()
    step_smoke()
    print("\nAll sentinel-token-source checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
