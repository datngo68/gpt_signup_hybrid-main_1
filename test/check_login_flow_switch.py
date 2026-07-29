#!/usr/bin/env python3
"""Verify switch session.login_flow (legacy | anti409, default anti409).

Chạy: .venv/bin/python test/check_login_flow_switch.py
"""
from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJ = ROOT.parent
sys.path.insert(0, str(PROJ))
PKG = "gpt_signup_hybrid_new"

rc = 0


def ok(idx: str, msg: str) -> None:
    print(f"[PASS] {idx} — {msg}", flush=True)


def fail(idx: str, msg: str) -> None:
    global rc
    rc = 1
    print(f"[FAIL] {idx} — {msg}", flush=True)


# TC-01: AST parse
def tc01() -> None:
    for f in ("session_phase.py", "db/repositories.py"):
        try:
            ast.parse((ROOT / f).read_text(encoding="utf-8"))
            ok("TC-01", f"AST {f}")
        except SyntaxError as e:
            fail("TC-01", f"{f}: {e}")


# TC-02: db constraint + whitelist
def tc02() -> None:
    from importlib import import_module
    repo = import_module(f"{PKG}.db.repositories")
    if "session.login_flow" in repo._EXACT_KEYS:
        ok("TC-02", "session.login_flow trong _EXACT_KEYS")
    else:
        fail("TC-02", "thiếu trong _EXACT_KEYS")
    for v in ("legacy", "anti409"):
        try:
            repo._validate_type_constraint("session.login_flow", v)
            ok("TC-02", f"accept {v!r}")
        except Exception as e:  # noqa: BLE001
            fail("TC-02", f"reject hợp lệ {v!r}: {e}")
    for v in ("auto", "", 1, True, None):
        try:
            repo._validate_type_constraint("session.login_flow", v)
            fail("TC-02", f"KHÔNG reject invalid {v!r}")
        except Exception:  # noqa: BLE001
            ok("TC-02", f"reject invalid {v!r}")


# TC-03: _resolve_login_flow ưu tiên + default
def tc03() -> None:
    from importlib import import_module
    sp = import_module(f"{PKG}.session_phase")
    r = sp._resolve_login_flow
    if r("legacy") == "legacy":
        ok("TC-03", "explicit legacy thắng")
    else:
        fail("TC-03", "explicit legacy không thắng")
    if r("anti409") == "anti409":
        ok("TC-03", "explicit anti409 thắng")
    else:
        fail("TC-03", "explicit anti409 không thắng")
    # explicit rác → bỏ qua, đọc DB/default. DB test env có thể trống → anti409.
    val = r(None)
    if val in ("legacy", "anti409"):
        ok("TC-03", f"resolve(None) hợp lệ = {val}")
    else:
        fail("TC-03", f"resolve(None) trả lạ: {val}")
    if r("garbage") in ("legacy", "anti409"):
        ok("TC-03", "explicit rác → fallback hợp lệ")
    else:
        fail("TC-03", "explicit rác không fallback")


# TC-04: signature + cả 2 nhánh tồn tại trong source
def tc04() -> None:
    from importlib import import_module
    sp = import_module(f"{PKG}.session_phase")
    sig = inspect.signature(sp.get_session_pure_request)
    if "login_flow" in sig.parameters:
        ok("TC-04", "get_session_pure_request có param login_flow")
    else:
        fail("TC-04", "thiếu param login_flow")
    src = (ROOT / "session_phase.py").read_text(encoding="utf-8")
    markers = {
        "anti409 branch": 'flow_mode == "anti409"',
        "password_verify sentinel": '"password_verify"',
        "auth_session_logging_id": "auth_session_logging_id",
        "legacy authorize/continue giữ": "_step_authorize_continue",
        "legacy _step_auth_url giữ": "_step_auth_url(",
        "warming chatgpt": "backend-anon/accounts/check",
    }
    for name, m in markers.items():
        if m in src:
            ok("TC-04", f"có '{name}'")
        else:
            fail("TC-04", f"thiếu '{name}' ({m})")


if __name__ == "__main__":
    for i, fn in enumerate((tc01, tc02, tc03, tc04), 1):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            import traceback
            fail(f"TC-0{i}", f"crash: {e}\n{traceback.format_exc()}")
    print("=== DONE ===", flush=True)
    sys.exit(rc)
