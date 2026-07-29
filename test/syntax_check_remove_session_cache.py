"""Syntax + reference check sau khi gỡ session-cookie-cache.

Chạy: python3 test/syntax_check_remove_session_cache.py

- AST-parse các file đã sửa (bắt SyntaxError).
- Assert 2 module đã xóa không còn tồn tại trên disk.
- Grep token cấm (session_provider / session_store / get_session_store /
  SessionProvider / SessionStore / save_login_result) trong các file runtime.
In [PASS]/[FAIL] realtime theo từng bước.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TOUCHED = [
    "session_phase.py",
    "web/server.py",
    "web/manager.py",
    "web/upi_runner.py",
    "db/repositories.py",
]

DELETED = [
    "session_provider.py",
    "session_store.py",
]

FORBIDDEN = (
    "session_provider",
    "session_store",
    "SessionProvider",
    "SessionStore",
    "get_session_store",
    "save_login_result",
    "seed_from_session_results",
)

_fails = 0


def _check(ok: bool, tag: str, desc: str, detail: str = "") -> None:
    global _fails
    status = "PASS" if ok else "FAIL"
    if not ok:
        _fails += 1
    print(f"[{status}] {tag} — {desc} :: {detail}", flush=True)


def main() -> int:
    # 1) AST-parse các file đã sửa
    for i, rel in enumerate(TOUCHED, 1):
        p = ROOT / rel
        try:
            ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            _check(True, f"AST-{i:02d}", f"parse {rel}", "ok")
        except (SyntaxError, OSError) as exc:
            _check(False, f"AST-{i:02d}", f"parse {rel}", f"{type(exc).__name__}: {exc}")

    # 2) Module đã xóa không còn trên disk
    for i, rel in enumerate(DELETED, 1):
        gone = not (ROOT / rel).exists()
        _check(gone, f"DEL-{i:02d}", f"{rel} đã xóa", "absent" if gone else "VẪN TỒN TẠI")

    # 3) Không còn token cấm trong file runtime
    for i, rel in enumerate(TOUCHED, 1):
        text = (ROOT / rel).read_text(encoding="utf-8")
        hits = sorted({tok for tok in FORBIDDEN if tok in text})
        # session_phase.py được phép chứa 'SessionError' nhưng KHÔNG chứa token cấm.
        _check(not hits, f"REF-{i:02d}", f"{rel} sạch token cấm",
               "clean" if not hits else f"còn: {hits}")

    print(f"\n=== {'ALL PASS' if _fails == 0 else str(_fails) + ' FAIL'} ===", flush=True)
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
