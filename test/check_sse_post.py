"""Check /api/sse hỗ trợ POST + header no-transform (fix Cloudflare tunnel buffer).

Chạy: python3 test/check_sse_post.py
"""
from __future__ import annotations

import ast
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SERVER_PY = ROOT / "web" / "server.py"
APP_JS = ROOT / "web" / "static" / "app.js"

PASSED = 0
FAILED = 0


def _check(label: str, fn) -> None:
    global PASSED, FAILED
    try:
        fn()
        print(f"[PASS] {label}", flush=True)
        PASSED += 1
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {label} :: {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        FAILED += 1


def t01_server_py_parses():
    ast.parse(SERVER_PY.read_text(encoding="utf-8"))


def t02_route_methods():
    # AST-based (không import web.server — tránh import nặng camoufox/playwright).
    # Tìm decorator @app.api_route("/api/sse", methods=[...]) trên unified_sse.
    tree = ast.parse(SERVER_PY.read_text(encoding="utf-8"))
    methods: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if node.name != "unified_sse":
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            if not (dec.args and isinstance(dec.args[0], ast.Constant)
                    and dec.args[0].value == "/api/sse"):
                continue
            for kw in dec.keywords:
                if kw.arg == "methods" and isinstance(kw.value, ast.List):
                    methods = [
                        e.value for e in kw.value.elts
                        if isinstance(e, ast.Constant)
                    ]
    assert methods, "không tìm thấy decorator api_route('/api/sse', methods=[...])"
    assert "GET" in methods, f"thiếu GET: {methods}"
    assert "POST" in methods, f"thiếu POST: {methods}"


def t03_no_transform_header():
    src = SERVER_PY.read_text(encoding="utf-8")
    assert "no-cache, no-transform" in src, "thiếu Cache-Control no-transform"


def t04_app_js_uses_post_fetch():
    src = APP_JS.read_text(encoding="utf-8")
    assert "new EventSource" not in src, "app.js vẫn còn EventSource"
    assert "method: 'POST'" in src, "SseBus chưa dùng POST"
    assert "getReader()" in src, "SseBus chưa đọc ReadableStream"
    assert "X-API-Token" in src, "SseBus chưa gửi token qua header"


for _name in sorted(n for n in dir() if n.startswith("t0")):
    _check(_name, globals()[_name])

print(f"\n{PASSED} passed, {FAILED} failed", flush=True)
sys.exit(1 if FAILED else 0)
