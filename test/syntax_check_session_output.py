"""Syntax + structural check cho patch "Get Session output blocks".

Kiểm:
  1. AST parse web/manager.py + web/server.py.
  2. SessionJobManager.get_secrets_map defined.
  3. Endpoint GET /api/session/jobs/secrets defined trong server.py
     (đặt TRƯỚC route {job_id} để FastAPI khớp đúng).
  4. JS files (session.js) parse OK qua node --check (nếu có node).
  5. HTML chứa các id Free/Plus pane + buttons + counts.

Chạy: python3 test/syntax_check_session_output.py
"""
from __future__ import annotations

import ast
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def check_python_ast() -> int:
    failed = 0
    for rel in ("web/manager.py", "web/server.py"):
        try:
            ast.parse(_read(rel))
            print(f"[PASS] AST parse — {rel}", flush=True)
        except SyntaxError as exc:
            print(f"[FAIL] AST parse — {rel}: {exc}", flush=True)
            failed += 1
    return failed


def check_get_secrets_map_defined() -> int:
    """SessionJobManager.get_secrets_map phải defined ở web/manager.py."""
    src = _read("web/manager.py")
    tree = ast.parse(src)

    found_in_class = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "SessionJobManager":
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == "get_secrets_map":
                    found_in_class = True
                    break
            break

    if not found_in_class:
        print("[FAIL] SessionJobManager.get_secrets_map không defined", flush=True)
        return 1
    print("[PASS] SessionJobManager.get_secrets_map defined", flush=True)
    return 0


def check_secrets_endpoint_route_order() -> int:
    """Endpoint /api/session/jobs/secrets phải đăng ký TRƯỚC /api/session/jobs/{job_id}.

    FastAPI route matching theo thứ tự khai báo — nếu {job_id} đăng ký trước,
    GET /api/session/jobs/secrets sẽ bị nuốt thành job_id="secrets".
    """
    src = _read("web/server.py")

    # Tìm vị trí 2 decorator
    re_secrets = re.search(
        r'@app\.get\("/api/session/jobs/secrets"\)', src,
    )
    re_job_id = re.search(
        r'@app\.get\("/api/session/jobs/\{job_id\}"\)', src,
    )

    if not re_secrets:
        print("[FAIL] /api/session/jobs/secrets endpoint không tìm thấy", flush=True)
        return 1
    if not re_job_id:
        print("[FAIL] /api/session/jobs/{job_id} endpoint không tìm thấy", flush=True)
        return 1

    if re_secrets.start() >= re_job_id.start():
        print(
            "[FAIL] /api/session/jobs/secrets phải đăng ký TRƯỚC {job_id} "
            f"(secrets@{re_secrets.start()}, job_id@{re_job_id.start()})",
            flush=True,
        )
        return 1

    print("[PASS] secrets endpoint đăng ký trước {job_id} route", flush=True)
    return 0


def check_html_ids() -> int:
    """index.html phải chứa các id Free/Plus pane + count + button copy."""
    src = _read("web/static/index.html")
    required = [
        'id="ses-free-pane"',
        'id="ses-plus-pane"',
        'id="ses-free-count"',
        'id="ses-plus-count"',
        'id="ses-btn-copy-free"',
        'id="ses-btn-copy-plus"',
        'class="card card-success card-ses-free"',
        'class="card card-success card-ses-plus"',
    ]
    failed = 0
    for token in required:
        if token in src:
            print(f"[PASS] HTML có {token}", flush=True)
        else:
            print(f"[FAIL] HTML thiếu {token}", flush=True)
            failed += 1
    return failed


def check_css_grid() -> int:
    """style.css phải có grid-area free + plus + tab-session 4 rows."""
    src = _read("web/static/style.css")
    required = [
        ".card-ses-free { grid-area: free; }",
        ".card-ses-plus { grid-area: plus; }",
    ]
    failed = 0
    for token in required:
        if token in src:
            print(f"[PASS] CSS có '{token[:40]}…'", flush=True)
        else:
            print(f"[FAIL] CSS thiếu '{token}'", flush=True)
            failed += 1

    # Tìm grid-template-areas của #tab-session phải chứa "free  plus" và "error error"
    m = re.search(
        r"#tab-session\s*\{[^}]*?grid-template-areas:\s*([^;]+);",
        src,
        re.DOTALL,
    )
    if not m:
        print("[FAIL] #tab-session grid-template-areas không tìm thấy", flush=True)
        return failed + 1
    areas = m.group(1)
    for token in ('"free  plus"', '"error error"'):
        if token in areas:
            print(f"[PASS] tab-session grid có {token}", flush=True)
        else:
            print(f"[FAIL] tab-session grid thiếu {token}", flush=True)
            failed += 1
    return failed


def check_js_syntax() -> int:
    """session.js phải parse OK qua node --check (skip nếu không có node)."""
    js_path = ROOT / "web/static/session.js"
    node = shutil.which("node")
    if not node:
        print("[SKIP] node not found — bỏ qua JS syntax check", flush=True)
        return 0
    try:
        result = subprocess.run(
            [node, "--check", str(js_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        print(f"[FAIL] node --check failed: {exc}", flush=True)
        return 1
    if result.returncode != 0:
        print(
            f"[FAIL] session.js syntax error:\n{result.stderr or result.stdout}",
            flush=True,
        )
        return 1
    print("[PASS] session.js node --check OK", flush=True)
    return 0


def check_js_symbols() -> int:
    """session.js phải chứa các symbol mới (refreshSecrets, _isPlusPlan, etc.)."""
    src = _read("web/static/session.js")
    required = [
        "_pastedSecretsByEmail",
        "_capturePastedSecrets",
        "scheduleSecretsRefresh",
        "function refreshSecrets()",
        "function _isPlusPlan(",
        "function _formatAccountLine(",
        "freePane:",
        "plusPane:",
        "btnCopyFree:",
        "btnCopyPlus:",
        "/api/session/jobs/secrets",
    ]
    failed = 0
    for token in required:
        if token in src:
            print(f"[PASS] JS có '{token}'", flush=True)
        else:
            print(f"[FAIL] JS thiếu '{token}'", flush=True)
            failed += 1
    return failed


def main() -> int:
    fails = 0
    fails += check_python_ast()
    fails += check_get_secrets_map_defined()
    fails += check_secrets_endpoint_route_order()
    fails += check_html_ids()
    fails += check_css_grid()
    fails += check_js_syntax()
    fails += check_js_symbols()
    print()
    if fails:
        print(f"[FAIL] {fails} check(s) failed", flush=True)
        return 1
    print("[PASS] tất cả checks OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
