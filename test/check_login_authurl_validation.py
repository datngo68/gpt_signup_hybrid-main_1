#!/usr/bin/env python3
"""Smoke check cho fix login bug HTTP 409 invalid_state.

Mục đích:
    - Xác nhận `_step_auth_url` có validation `"auth.openai.com" not in url`
      → fail-fast trước khi cascade thành 409 invalid_state.
    - Xác nhận `_step_auth_url` raise full payload + cookies khi URL xấu.
    - Xác nhận `record` CLI chấp nhận `--proxy`.
    - Xác nhận `WebRecorderOptions.proxy` được validate scheme.
    - Xác nhận session_phase._do_bootstrap convert RequestPhaseError → SessionError.

Không gọi network thật. Chỉ parse AST + import + check chuỗi.

Chạy: python3 test/check_login_authurl_validation.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _ok(tc: str, label: str, detail: str = "") -> None:
    msg = f"[PASS] {tc} — {label}"
    if detail:
        msg += f" :: {detail}"
    print(msg, flush=True)


def _fail(tc: str, label: str, detail: str = "") -> None:
    msg = f"[FAIL] {tc} — {label}"
    if detail:
        msg += f" :: {detail}"
    print(msg, flush=True)


def t01_request_phase_syntax() -> bool:
    print("[1/10] parse request_phase.py", flush=True)
    src = (ROOT / "request_phase.py").read_text(encoding="utf-8")
    try:
        ast.parse(src)
    except SyntaxError as exc:
        _fail("TC-01", "request_phase.py syntax error", str(exc))
        return False
    _ok("TC-01", "request_phase.py parse OK")
    return True


def t02_step_auth_url_validates_domain() -> bool:
    print("[2/10] check _step_auth_url validate auth.openai.com", flush=True)
    src = (ROOT / "request_phase.py").read_text(encoding="utf-8")
    expectations = [
        ('"auth.openai.com" not in auth_url', "fail-fast guard"),
        ("signin/openai trả URL không phải auth.openai.com", "vietnamese error msg"),
        ("CSRF/origin/anti-bot", "diagnostic hint mention"),
        ("session.cookies.jar", "cookie dump for diagnosis"),
    ]
    failed = False
    for snippet, label in expectations:
        if snippet not in src:
            _fail("TC-02", f"missing snippet: {label}", repr(snippet)[:80])
            failed = True
        else:
            _ok("TC-02", label)
    return not failed


def t03_step_auth_url_logs_payload_on_no_url() -> bool:
    print("[3/10] check _step_auth_url log payload khi missing url", flush=True)
    src = (ROOT / "request_phase.py").read_text(encoding="utf-8")
    if "no URL in response — payload=" not in src:
        _fail("TC-03", "_step_auth_url không log payload khi miss `url`")
        return False
    _ok("TC-03", "payload preview included on miss")
    return True


def t04_cli_record_has_proxy_flag() -> bool:
    print("[4/10] check record CLI có --proxy option", flush=True)
    src = (ROOT / "cli.py").read_text(encoding="utf-8")
    if '"--proxy"' not in src:
        _fail("TC-04", "record CLI thiếu --proxy option")
        return False
    if "proxy=proxy" not in src:
        _fail("TC-04", "record CLI không pass proxy vào WebRecorderOptions")
        return False
    _ok("TC-04", "record --proxy wired vào WebRecorderOptions")
    return True


def t05_web_recorder_validates_proxy_scheme() -> bool:
    print("[5/10] check WebRecorder validate proxy scheme", flush=True)
    src = (ROOT / "web_recorder.py").read_text(encoding="utf-8")
    expectations = [
        ("--proxy phải bắt đầu", "validation error msg vietnamese"),
        ('"http://", "https://", "socks5://"', "scheme list contains http/https/socks5"),
    ]
    failed = False
    for snippet, label in expectations:
        if snippet not in src:
            _fail("TC-05", f"missing: {label}", repr(snippet)[:80])
            failed = True
        else:
            _ok("TC-05", label)
    return not failed


def t06_import_modules() -> bool:
    print("[6/10] import-time check (request_phase, cli, web_recorder)", flush=True)
    failed = False
    try:
        import request_phase  # noqa: F401
        _ok("TC-06", "import request_phase")
    except Exception as exc:  # noqa: BLE001
        _fail("TC-06", "import request_phase", repr(exc))
        failed = True
    try:
        import cli  # noqa: F401
        _ok("TC-06", "import cli")
    except Exception as exc:  # noqa: BLE001
        _fail("TC-06", "import cli", repr(exc))
        failed = True
    try:
        import web_recorder  # noqa: F401
        _ok("TC-06", "import web_recorder")
    except Exception as exc:  # noqa: BLE001
        _fail("TC-06", "import web_recorder", repr(exc))
        failed = True
    return not failed


def t07_session_phase_wraps_request_phase_error() -> bool:
    """session_phase._do_bootstrap convert RequestPhaseError → SessionError."""
    print("[7/10] check session_phase wrap RequestPhaseError → SessionError", flush=True)
    src = (ROOT / "session_phase.py").read_text(encoding="utf-8")
    expectations = [
        ("if isinstance(e, RequestPhaseError):", "in-loop wrap guard"),
        ("if isinstance(last_exc, RequestPhaseError):", "post-loop wrap guard"),
        ('raise SessionError(f"bootstrap failed: {e}") from e', "wrap raises SessionError chain"),
    ]
    failed = False
    for snippet, label in expectations:
        if snippet not in src:
            _fail("TC-07", f"missing: {label}", repr(snippet)[:80])
            failed = True
        else:
            _ok("TC-07", label)
    return not failed


def t08_diag_login_bootstrap_script() -> bool:
    """test/diag_login_bootstrap.py syntax + có CLI arg + import được."""
    print("[8/10] check diag_login_bootstrap.py syntax + structure", flush=True)
    diag_path = ROOT / "test" / "diag_login_bootstrap.py"
    if not diag_path.exists():
        _fail("TC-08", "diag_login_bootstrap.py không tồn tại")
        return False
    src = diag_path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        _fail("TC-08", "syntax error", str(exc))
        return False
    _ok("TC-08", "diag_login_bootstrap.py parse OK")

    expectations = [
        ('"--email"', "CLI --email"),
        ('"--proxy"', "CLI --proxy"),
        ('"--impersonate"', "CLI --impersonate"),
        ('"https://chatgpt.com/api/auth/csrf"', "STEP 1 hits NextAuth csrf"),
        ('"https://chatgpt.com/api/auth/signin/openai?"', "STEP 2 hits NextAuth signin/openai"),
        ("_redact_set_cookie", "Set-Cookie redaction"),
        ("_mask_value", "value masking"),
        ('"auth.openai.com" in auth_url', "OK conclusion classifier"),
        ('"csrf=true"', "CSRF rejected classifier"),
    ]
    failed = False
    for snippet, label in expectations:
        if snippet not in src:
            _fail("TC-08", f"missing: {label}", repr(snippet)[:80])
            failed = True
        else:
            _ok("TC-08", label)

    # Ensure script defines `main` + diag` functions (introspect AST).
    func_names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    for fn in ("diag", "main"):
        if fn not in func_names:
            _fail("TC-08", f"missing function: {fn}")
            failed = True
        else:
            _ok("TC-08", f"function {fn} defined")
    return not failed


def t09_prime_chatgpt_session_function_exists() -> bool:
    """request_phase định nghĩa _prime_chatgpt_session với behavior đúng."""
    print("[9/10] check _prime_chatgpt_session function in request_phase.py", flush=True)
    src = (ROOT / "request_phase.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    func_names = {
        n.name for n in tree.body if isinstance(n, ast.FunctionDef)
    }
    if "_prime_chatgpt_session" not in func_names:
        _fail("TC-09", "thiếu function _prime_chatgpt_session")
        return False
    _ok("TC-09", "_prime_chatgpt_session defined")

    expectations = [
        ('"https://chatgpt.com/auth/login"', "GET landing page URL"),
        ('c.name == "__cf_bm"', "idempotent skip check"),
        ('"text/html', "Accept html top-level navigation"),
        ('"Sec-Fetch-Mode": "navigate"', "navigate sec-fetch hint"),
        ('"Upgrade-Insecure-Requests": "1"', "browser-like HTML request"),
        ("prime chatgpt session failed", "fail-fast on HTTP error"),
    ]
    failed = False
    for snippet, label in expectations:
        if snippet not in src:
            _fail("TC-09", f"missing: {label}", repr(snippet)[:80])
            failed = True
        else:
            _ok("TC-09", label)
    return not failed


def t10_step_csrf_calls_prime() -> bool:
    """_step_csrf phải gọi _prime_chatgpt_session ngay đầu hàm."""
    print("[10/10] check _step_csrf calls _prime_chatgpt_session at top", flush=True)
    src = (ROOT / "request_phase.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    target = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_step_csrf":
            target = node
            break
    if target is None:
        _fail("TC-10", "không tìm thấy function _step_csrf")
        return False

    # Find first Expr/Call in function body that calls _prime_chatgpt_session.
    found = False
    for stmt in target.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            call = stmt.value
            if isinstance(call.func, ast.Name) and call.func.id == "_prime_chatgpt_session":
                found = True
                break
        # Skip docstrings (Expr Constant string).
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue
        # Stop scanning if hit non-Expr statement.
        if not isinstance(stmt, ast.Expr):
            break
    if not found:
        _fail("TC-10", "_step_csrf không gọi _prime_chatgpt_session ở đầu hàm (sau docstring)")
        return False
    _ok("TC-10", "_step_csrf gọi _prime_chatgpt_session đầu function body")
    return True


def main() -> int:
    cases = [
        t01_request_phase_syntax,
        t02_step_auth_url_validates_domain,
        t03_step_auth_url_logs_payload_on_no_url,
        t04_cli_record_has_proxy_flag,
        t05_web_recorder_validates_proxy_scheme,
        t06_import_modules,
        t07_session_phase_wraps_request_phase_error,
        t08_diag_login_bootstrap_script,
        t09_prime_chatgpt_session_function_exists,
        t10_step_csrf_calls_prime,
    ]
    failures = 0
    for fn in cases:
        try:
            ok = fn()
        except Exception as exc:  # noqa: BLE001
            _fail(fn.__name__, "raised unexpected", repr(exc))
            ok = False
        if not ok:
            failures += 1
    print(f"\n[summary] passed={len(cases) - failures}/{len(cases)}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
