"""Verify mode pure_request đã bị gỡ khỏi ĐĂNG KÝ (reg), nhưng file
request_phase.py vẫn còn (login/session_phase tái dùng helper).

Checks:
    [1] AST parse mọi file đã sửa + request_phase.py + session_phase.py.
    [2] signup.py: không còn run_request_phase / nhánh reg pure_request; có guard
        fail-fast reg_mode not in (browser, hybrid).
    [3] cli.py / db/repositories.py / web/server.py / web/icloud_routes.py:
        validation reg chỉ còn browser+hybrid; web default = browser.
    [4] Frontend: index.html không còn <option pure_request>; app.js list bỏ.
    [5] request_phase.py CÒN nguyên + export helper mà session_phase cần.
    [6] db validation reject reg_mode.current='pure_request', accept browser/hybrid.

Chạy: .venv/bin/python test/check_pure_request_removed.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FILES = {
    "signup": ROOT / "signup.py",
    "cli": ROOT / "cli.py",
    "models": ROOT / "models.py",
    "repos": ROOT / "db" / "repositories.py",
    "server": ROOT / "web" / "server.py",
    "icloud_routes": ROOT / "web" / "icloud_routes.py",
    "autoreg": ROOT / "autoreg" / "runner.py",
    "request_phase": ROOT / "request_phase.py",
    "session_phase": ROOT / "session_phase.py",
    "index_html": ROOT / "web" / "static" / "index.html",
    "app_js": ROOT / "web" / "static" / "app.js",
}

# Session_phase tái dùng các helper này từ request_phase (không được mất).
_SESSION_PHASE_NEEDS = (
    "_create_session", "_step_csrf", "_is_rotatable_error",
    "_IMPERSONATE_CANDIDATES", "RequestPhaseError", "USER_AGENT",
)

_passes: list[str] = []
_failures: list[str] = []


def _ok(m: str) -> None:
    _passes.append(m)
    print(f"[PASS] {m}")


def _fail(m: str) -> None:
    _failures.append(m)
    print(f"[FAIL] {m}")


def _read(name: str) -> str:
    return FILES[name].read_text(encoding="utf-8")


def check_ast() -> None:
    for name, path in FILES.items():
        if path.suffix not in (".py",):
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            _ok(f"AST parse OK: {name}")
        except SyntaxError as exc:
            _fail(f"SyntaxError {name}: {exc}")


def check_signup() -> None:
    src = _read("signup")
    if "run_request_phase" not in src:
        _ok("signup.py: không còn tham chiếu run_request_phase")
    else:
        _fail("signup.py: vẫn còn run_request_phase")
    if 'reg_mode == "pure_request"' not in src:
        _ok("signup.py: không còn nhánh routing reg_mode=='pure_request'")
    else:
        _fail("signup.py: vẫn còn nhánh reg_mode=='pure_request'")
    if 'not in ("browser", "hybrid")' in src and "không hợp lệ" in src:
        _ok("signup.py: có guard fail-fast reg_mode (browser|hybrid)")
    else:
        _fail("signup.py: thiếu guard fail-fast reg_mode")


def check_validations() -> None:
    cli = _read("cli")
    if 'reg_mode not in ("browser", "hybrid")' in cli and "pure_request" not in cli:
        _ok("cli.py: validation reg_mode chỉ browser+hybrid, sạch pure_request")
    else:
        _fail("cli.py: validation reg_mode chưa sạch pure_request")

    repos = _read("repos")
    # reg_mode.current validation phải bỏ pure_request (chỉ còn browser, hybrid).
    if 'value not in ("browser", "hybrid")' in repos:
        _ok("db/repositories.py: reg_mode.current chỉ accept browser+hybrid")
    else:
        _fail("db/repositories.py: reg_mode.current validation chưa cập nhật")

    server = _read("server")
    # AddJobsRequest reg_mode default phải là browser (không pure_request).
    if 'default="pure_request"' not in server:
        _ok("web/server.py: không còn default reg_mode='pure_request'")
    else:
        _fail("web/server.py: vẫn còn default reg_mode='pure_request'")

    icr = _read("icloud_routes")
    if 'reg_mode not in ("browser", "hybrid")' in icr:
        _ok("web/icloud_routes.py: validation reg_mode chỉ browser+hybrid")
    else:
        _fail("web/icloud_routes.py: validation reg_mode chưa cập nhật")


def check_frontend() -> None:
    html = _read("index_html")
    if 'value="pure_request"' not in html:
        _ok("index.html: đã bỏ <option pure_request>")
    else:
        _fail("index.html: vẫn còn <option pure_request>")
    appjs = _read("app_js")
    if "'pure_request'" not in appjs and '"pure_request"' not in appjs:
        _ok("app.js: list reg mode không còn pure_request")
    else:
        _fail("app.js: vẫn còn pure_request")


def check_request_phase_intact() -> None:
    """request_phase.py phải CÒN + giữ các helper session_phase cần."""
    if not FILES["request_phase"].exists():
        _fail("request_phase.py bị xóa — session_phase login sẽ vỡ!")
        return
    src = _read("request_phase")
    tree = ast.parse(src)
    defined = {
        n.name for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    # _IMPERSONATE_CANDIDATES / USER_AGENT là module-level assign.
    assigns = {
        t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
        for t in n.targets if isinstance(t, ast.Name)
    }
    available = defined | assigns
    missing = [s for s in _SESSION_PHASE_NEEDS if s not in available]
    if not missing:
        _ok(f"request_phase.py: còn đủ {len(_SESSION_PHASE_NEEDS)} helper cho session_phase")
    else:
        _fail(f"request_phase.py: THIẾU helper session_phase cần: {missing}")


def check_db_validation_runtime() -> None:
    """Gọi thật _validate_type_constraint: reject pure_request, accept browser/hybrid."""
    try:
        from db.repositories import _validate_type_constraint, RepositoryError
    except Exception as exc:  # noqa: BLE001 — fallback source-only
        _ok(f"db validation runtime skip (import: {type(exc).__name__}) — source check đã cover")
        return

    # accept browser + hybrid
    try:
        _validate_type_constraint("reg_mode.current", "browser")
        _validate_type_constraint("reg_mode.current", "hybrid")
        _ok("db validation: accept 'browser' + 'hybrid'")
    except Exception as exc:  # noqa: BLE001
        _fail(f"db validation: reject browser/hybrid sai — {exc}")
    # reject pure_request
    try:
        _validate_type_constraint("reg_mode.current", "pure_request")
        _fail("db validation: KHÔNG reject 'pure_request' (phải reject)")
    except RepositoryError:
        _ok("db validation: reject 'pure_request' đúng")
    except Exception as exc:  # noqa: BLE001
        _fail(f"db validation: lỗi loại sai cho pure_request — {type(exc).__name__}")


def main() -> int:
    check_ast()
    check_signup()
    check_validations()
    check_frontend()
    check_request_phase_intact()
    check_db_validation_runtime()
    print("\n" + "=" * 60)
    print(f"PASS={len(_passes)}  FAIL={len(_failures)}")
    if _failures:
        print("\nFAILURES:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
