"""Verify fix login retry pattern + DIRECT login cho SessionJobManager.

Tests:
  - TC-01: Syntax parse OK 2 file đã sửa
  - TC-02: SessionJobManager._run_job KHÔNG còn await _begin_job_proxy + KHÔNG
           pass proxy=job_proxy vào get_session_pure_request → đảm bảo DIRECT
  - TC-03: SessionJobManager._run_job chứa NON_RETRYABLE_PATTERNS + LOGIN_MAX_ATTEMPTS
  - TC-04: _prime_chatgpt_session có for-loop retry với "prime 403, retrying" log
  - TC-05: Symbol UPI run_upi_qr_probe vẫn còn nguyên login retry pattern (regression
           guard — không vô tình động vào)
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILED = 0


def _fail(tc: str, msg: str) -> None:
    global FAILED
    FAILED += 1
    print(f"[FAIL] {tc} :: {msg}", flush=True)


def _ok(tc: str, msg: str) -> None:
    print(f"[PASS] {tc} :: {msg}", flush=True)


def tc01_syntax() -> None:
    for rel in ("request_phase.py", "web/manager.py"):
        path = ROOT / rel
        try:
            ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            _fail("TC-01", f"{rel} parse fail: {exc}")
            return
    _ok("TC-01", "request_phase.py + web/manager.py parse OK")


def _find_func(tree: ast.AST, name: str, *, classname: str | None = None) -> ast.AST | None:
    """Tìm function/method theo tên (+ optional class scope)."""
    if classname:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == classname:
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == name:
                        return sub
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def tc02_session_run_job_direct() -> None:
    """SessionJobManager._run_job: KHÔNG await _begin_job_proxy, KHÔNG truyền
    proxy=job_proxy → login DIRECT."""
    src = (ROOT / "web/manager.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = _find_func(tree, "_run_job", classname="SessionJobManager")
    if fn is None:
        _fail("TC-02", "không tìm thấy SessionJobManager._run_job")
        return
    body_dump = ast.dump(fn)
    if "_begin_job_proxy" in body_dump:
        _fail("TC-02", "SessionJobManager._run_job vẫn gọi _begin_job_proxy (cần bỏ)")
        return
    # Verify get_session_pure_request được gọi với proxy=None (DIRECT)
    if "proxy=None" not in ast.unparse(fn):
        _fail("TC-02", "không thấy proxy=None trong _run_job (cần force DIRECT)")
        return
    if "job_proxy" in body_dump:
        _fail("TC-02", "vẫn còn ref tới job_proxy")
        return
    _ok("TC-02", "SessionJobManager._run_job login DIRECT (no _begin_job_proxy, proxy=None)")


def tc03_session_login_retry_pattern() -> None:
    src = (ROOT / "web/manager.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = _find_func(tree, "_run_job", classname="SessionJobManager")
    if fn is None:
        _fail("TC-03", "không tìm thấy SessionJobManager._run_job")
        return
    text = ast.unparse(fn)
    required_tokens = (
        "LOGIN_MAX_ATTEMPTS",
        "LOGIN_RETRY_DELAY",
        "NON_RETRYABLE_PATTERNS",
        "_is_login_error_retryable",
        "_login_with_retry",
        "password verify failed",
        "mfa verify failed",
        "no secret provided",
        "otp polling returned empty",
    )
    missing = [t for t in required_tokens if t not in text]
    if missing:
        _fail("TC-03", f"thiếu token retry pattern: {missing}")
        return
    _ok("TC-03", "SessionJobManager._run_job có đủ login retry pattern (3 attempts + non-retryable)")


def tc04_prime_session_retry() -> None:
    src = (ROOT / "request_phase.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = _find_func(tree, "_prime_chatgpt_session")
    if fn is None:
        _fail("TC-04", "không tìm thấy _prime_chatgpt_session")
        return
    text = ast.unparse(fn)
    if "for attempt in range(3)" not in text:
        _fail("TC-04", "thiếu for-loop retry 3 attempts")
        return
    if "prime 403, retrying" not in text:
        _fail("TC-04", "thiếu log retry 403 ('prime 403, retrying ...')")
        return
    if ".sleep(" not in text:
        _fail("TC-04", "thiếu time.sleep giữa retry")
        return
    _ok("TC-04", "_prime_chatgpt_session có retry 3x cho 403 + backoff sleep")


def tc04b_cf_403_triggers_rotation() -> None:
    """Fix triệt để: HTTP 403 từ chatgpt.com (prime/csrf) → trigger impersonate
    rotation chain (Chrome 145 → 142 → 136), không chỉ TLS error."""
    src = (ROOT / "request_phase.py").read_text(encoding="utf-8")
    if "_is_cloudflare_block_error" not in src:
        _fail("TC-04b", "thiếu _is_cloudflare_block_error helper")
        return
    if "_is_rotatable_error" not in src:
        _fail("TC-04b", "thiếu _is_rotatable_error helper")
        return
    # Verify: _bootstrap_with_tls_rotation dùng _is_rotatable_error (không phải
    # _is_tls_error đơn lẻ) để bao trùm CF 403.
    tree = ast.parse(src)
    fn = _find_func(tree, "_bootstrap_with_tls_rotation")
    if fn is None:
        _fail("TC-04b", "không tìm thấy _bootstrap_with_tls_rotation")
        return
    body = ast.unparse(fn)
    if "_is_rotatable_error" not in body:
        _fail("TC-04b", "_bootstrap_with_tls_rotation chưa dùng _is_rotatable_error")
        return
    # Verify: _is_cloudflare_block_error match đúng marker prime/csrf 403.
    cf_fn = _find_func(tree, "_is_cloudflare_block_error")
    if cf_fn is None:
        _fail("TC-04b", "không tìm thấy _is_cloudflare_block_error")
        return
    cf_text = ast.unparse(cf_fn)
    if "http 403" not in cf_text or "prime chatgpt session" not in cf_text or "csrf fetch" not in cf_text:
        _fail("TC-04b", "_is_cloudflare_block_error thiếu marker prime/csrf/403")
        return

    # Behavioral: simulate 2 exception → kiểm tra _is_cloudflare_block_error
    # phân loại đúng (chỉ trigger cho chatgpt 403, KHÔNG trigger cho 403 khác).
    sys.path.insert(0, str(ROOT))
    import importlib
    rp = importlib.import_module("request_phase")

    cf_403_prime = rp.RequestPhaseError("prime chatgpt session failed: HTTP 403")
    cf_403_csrf = rp.RequestPhaseError("CSRF fetch failed: HTTP 403")
    other_403 = rp.RequestPhaseError("Stripe approve failed: HTTP 403")
    other_500 = rp.RequestPhaseError("HTTP 500 internal")
    tls_err = Exception("curl: (35) tls connect error")

    cases = [
        ("prime 403", cf_403_prime, True),
        ("csrf 403", cf_403_csrf, True),
        ("stripe 403", other_403, False),
        ("500 error", other_500, False),
    ]
    for label, exc, expect in cases:
        got = rp._is_cloudflare_block_error(exc)
        if got != expect:
            _fail("TC-04b", f"{label}: expect _is_cloudflare_block_error={expect} got={got}")
            return

    # _is_rotatable_error = TLS OR CF 403
    if not rp._is_rotatable_error(tls_err):
        _fail("TC-04b", "_is_rotatable_error miss TLS error")
        return
    if not rp._is_rotatable_error(cf_403_prime):
        _fail("TC-04b", "_is_rotatable_error miss CF 403")
        return
    if rp._is_rotatable_error(other_403):
        _fail("TC-04b", "_is_rotatable_error sai trigger cho 403 không phải CF chatgpt")
        return

    _ok("TC-04b", "CF 403 (prime/csrf) trigger fingerprint rotation; 403 khác KHÔNG trigger")


def tc04c_session_phase_uses_rotatable() -> None:
    """session_phase._do_bootstrap dùng _is_rotatable_error thay vì _is_tls_error."""
    src = (ROOT / "session_phase.py").read_text(encoding="utf-8")
    if "_is_rotatable_error" not in src:
        _fail("TC-04c", "session_phase chưa import/dùng _is_rotatable_error")
        return
    # Verify _is_tls_error KHÔNG còn dùng (đã được _is_rotatable_error bao trùm).
    if "_is_tls_error" in src:
        _fail("TC-04c", "session_phase vẫn còn ref tới _is_tls_error (cần thay hết)")
        return
    _ok("TC-04c", "session_phase._do_bootstrap dùng _is_rotatable_error (thay _is_tls_error)")


def tc05_upi_login_pattern_intact() -> None:
    """Regression guard — không động vào pattern UPI."""
    src = (ROOT / "web/upi_runner.py").read_text(encoding="utf-8")
    required = (
        "LOGIN_MAX_ATTEMPTS = 3",
        "NON_RETRYABLE_PATTERNS",
        "_is_login_error_retryable",
        "login_proxy = first_proxy if (proxy_from_step <= 1 and first_proxy) else None",
    )
    missing = [t for t in required if t not in src]
    if missing:
        _fail("TC-05", f"upi_runner pattern bị động: missing {missing}")
        return
    _ok("TC-05", "upi_runner login retry pattern intact (regression OK)")


def tc06_import() -> None:
    """Import được module sau patch (không circular / runtime error)."""
    try:
        import importlib

        for name in ("request_phase", "session_phase"):
            mod = importlib.import_module(name)
            assert mod is not None
        # web.manager import lazily (cần FastAPI app context); chỉ verify file
        # parse + symbol exist trong module global. Tránh side-effect import.
    except Exception as exc:
        _fail("TC-06", f"import fail: {type(exc).__name__}: {exc}")
        return
    _ok("TC-06", "import request_phase + session_phase OK")


def main() -> int:
    print("=== check_session_login_retry ===", flush=True)
    tc01_syntax()
    tc02_session_run_job_direct()
    tc03_session_login_retry_pattern()
    tc04_prime_session_retry()
    tc04b_cf_403_triggers_rotation()
    tc04c_session_phase_uses_rotatable()
    tc05_upi_login_pattern_intact()
    tc06_import()
    print(f"=== Done. Failures: {FAILED} ===", flush=True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
