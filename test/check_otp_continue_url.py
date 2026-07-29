"""Smoke + AST test cho fix P0: _submit_otp_via_api parse continue_url
và _drive_signup_flow follow goto(continue_url).

Verify:
  1. browser_phase.py parse-AST OK (no syntax error)
  2. `_submit_otp_via_api` signature trả `str | None`
  3. `_drive_signup_flow` có goto(continue_url) sau API fallback
  4. Smoke: gọi `_submit_otp_via_api` với mock context.request, expect:
       - 200 + body có continue_url → return URL
       - 200 + body JSON thiếu continue_url → return None
       - 200 + body không phải JSON → return None
       - 400 → raise BrowserPhaseError
       - ctx.request = None → raise BrowserPhaseError

Chạy: ``python3 test/check_otp_continue_url.py``
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

BROWSER_PHASE = ROOT / "browser_phase.py"


# ───────────────────────────── AST checks ─────────────────────────────


def _ast_check() -> None:
    print("[1/4] AST parse browser_phase.py...")
    src = BROWSER_PHASE.read_text()
    tree = ast.parse(src, filename=str(BROWSER_PHASE))
    print("      ✓ syntax OK")

    # Verify _submit_otp_via_api signature `-> str | None`
    fn_api = None
    fn_otp = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            if node.name == "_submit_otp_via_api":
                fn_api = node
            elif node.name == "_submit_otp":
                fn_otp = node
    if fn_api is None:
        raise SystemExit("FAIL: không tìm thấy _submit_otp_via_api")
    if fn_otp is None:
        raise SystemExit("FAIL: không tìm thấy _submit_otp")

    for fn in (fn_api, fn_otp):
        ret = fn.returns
        if ret is None:
            raise SystemExit(f"FAIL: {fn.name} không có return annotation")
        ret_repr = ast.unparse(ret)
        if "str" not in ret_repr or "None" not in ret_repr:
            raise SystemExit(
                f"FAIL: {fn.name} return annotation phải là `str | None`, got: {ret_repr}"
            )
        print(f"      ✓ {fn.name} return annotation = {ret_repr}")

    # Verify _submit_otp body có expect_response + Enter + human_type
    otp_src = ast.unparse(fn_otp)
    for marker in (
        "expect_response",
        "human_type",
        'press("Enter"',
        "/api/accounts/email-otp/validate",
    ):
        if marker not in otp_src:
            raise SystemExit(f"FAIL: _submit_otp thiếu pattern {marker!r}")
    print("      ✓ _submit_otp dùng expect_response + human_type + Enter")

    # Verify _drive_signup_flow has conditional goto(otp_continue_url) for
    # API path only (UI path lets page nav naturally; manual goto would
    # race with natural nav and cause NS_BINDING_ABORTED).
    src_normalized = re.sub(r"\s+", "", src)
    goto_count = src_normalized.count("page.goto(otp_continue_url")
    if goto_count < 2:
        raise SystemExit(
            f"FAIL: _drive_signup_flow chỉ có {goto_count} chỗ goto(otp_continue_url), "
            f"expected ≥ 2 (submit chính + retry pending code, conditional on otp_source=='api')"
        )
    print(f"      ✓ goto(otp_continue_url) xuất hiện {goto_count} lần (conditional on source=='api')")

    # Verify the otp_source pattern is used to gate goto.
    if "otp_source==\"api\"" not in src_normalized and "otp_source=='api'" not in src_normalized:
        raise SystemExit(
            "FAIL: caller phải gate goto bằng otp_source=='api' "
            "(UI path → page tự nav, không nên manual goto)"
        )
    print("      ✓ goto gated bằng otp_source == 'api' (UI path không race)")

    if "page.goto(continue_url" not in src_normalized:
        raise SystemExit("FAIL: nhánh API fallback chưa goto(continue_url)")
    if "followcontinue_url" not in src_normalized:
        raise SystemExit("FAIL: thiếu log line follow continue_url")
    print("      ✓ API fallback (P0) vẫn goto continue_url")


# ─────────────────────────── Smoke test mocks ───────────────────────────


class _MockResponse:
    def __init__(self, status: int, body: str):
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body


class _MockRequest:
    def __init__(self, status: int, body: str):
        self._status = status
        self._body = body
        self.last_url = None
        self.last_data = None

    async def post(self, url: str, *, data=None, headers=None) -> _MockResponse:
        self.last_url = url
        self.last_data = data
        return _MockResponse(self._status, self._body)


class _MockCtx:
    def __init__(self, status: int = 200, body: str = "{}"):
        self.request = _MockRequest(status, body)


class _NoRequestCtx:
    request = None


def _log(msg: str) -> None:
    print(f"      log: {msg}")


# ─────────────────────────── Smoke test cases ───────────────────────────


async def _smoke() -> None:
    # Import lazy để có cơ hội báo lỗi rõ nếu module có vấn đề.
    from browser_phase import _submit_otp_via_api, BrowserPhaseError

    print("[2/4] Case A: 200 + body có continue_url → return URL")
    ctx = _MockCtx(
        200,
        json.dumps({
            "continue_url": "https://auth.openai.com/about-you",
            "method": "GET",
            "page": {"type": "about_you"},
        }),
    )
    out = await _submit_otp_via_api(ctx, otp_code="123456", log=_log)
    assert out == "https://auth.openai.com/about-you", f"unexpected: {out!r}"
    assert ctx.request.last_url == "https://auth.openai.com/api/accounts/email-otp/validate"
    assert ctx.request.last_data == {"code": "123456"}
    print(f"      ✓ continue_url={out!r}")

    print("[3/4] Case B: 200 + body JSON thiếu continue_url → None")
    out = await _submit_otp_via_api(_MockCtx(200, '{"method":"GET"}'), otp_code="111111", log=_log)
    assert out is None, f"expected None, got {out!r}"
    print("      ✓ None")

    print("      Case C: 200 + body không phải JSON → None")
    out = await _submit_otp_via_api(_MockCtx(200, "<html>error</html>"), otp_code="111111", log=_log)
    assert out is None, f"expected None, got {out!r}"
    print("      ✓ None")

    print("      Case D: 200 + body rỗng → None")
    out = await _submit_otp_via_api(_MockCtx(200, ""), otp_code="111111", log=_log)
    assert out is None, f"expected None, got {out!r}"
    print("      ✓ None")

    print("      Case E: 200 + body là array (không phải object) → None")
    out = await _submit_otp_via_api(_MockCtx(200, "[]"), otp_code="111111", log=_log)
    assert out is None, f"expected None, got {out!r}"
    print("      ✓ None")

    print("      Case F: 200 + continue_url chỉ chứa whitespace → None")
    out = await _submit_otp_via_api(
        _MockCtx(200, json.dumps({"continue_url": "   "})), otp_code="111111", log=_log
    )
    assert out is None, f"expected None, got {out!r}"
    print("      ✓ None")

    print("[4/4] Error cases")
    print("      Case G: HTTP 400 → raise BrowserPhaseError")
    try:
        await _submit_otp_via_api(_MockCtx(400, '{"error":"bad"}'), otp_code="111111", log=_log)
    except BrowserPhaseError as exc:
        print(f"      ✓ raised: {exc}")
    else:
        raise SystemExit("FAIL: HTTP 400 không raise")

    print("      Case H: HTTP 500 → raise BrowserPhaseError")
    try:
        await _submit_otp_via_api(_MockCtx(500, "internal"), otp_code="111111", log=_log)
    except BrowserPhaseError as exc:
        print(f"      ✓ raised: {exc}")
    else:
        raise SystemExit("FAIL: HTTP 500 không raise")

    print("      Case I: ctx.request = None → raise BrowserPhaseError")
    try:
        await _submit_otp_via_api(_NoRequestCtx(), otp_code="111111", log=_log)
    except BrowserPhaseError as exc:
        print(f"      ✓ raised: {exc}")
    else:
        raise SystemExit("FAIL: ctx.request=None không raise")


def main() -> int:
    _ast_check()
    asyncio.run(_smoke())
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
