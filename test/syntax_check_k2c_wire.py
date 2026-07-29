"""Phase 11.5 K2c verify — AST-parse + structural checks.

Checks:
  1. ``sentinel_sidecar.intercept_create_account_token`` exists with
     required kwonly args (device_id, name, birthdate, caller_cookies).
  2. ``request_phase._run_request_phase_sync`` calls
     ``sidecar.intercept_create_account_token`` BEFORE ``sidecar.get_so_token``.
  3. K2c call passes caller_cookies (cookie sync) + name + birthdate.

Run: python3 test/syntax_check_k2c_wire.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    rp = ROOT / "request_phase.py"
    sc = ROOT / "sentinel_sidecar.py"

    # 1. AST parse both files
    try:
        rp_tree = ast.parse(rp.read_text(encoding="utf-8"), filename=str(rp))
        sc_tree = ast.parse(sc.read_text(encoding="utf-8"), filename=str(sc))
    except SyntaxError as exc:
        print(f"FAIL: SyntaxError {exc.filename}:{exc.lineno}: {exc.msg}")
        return 1
    print("OK: AST parse — request_phase.py + sentinel_sidecar.py")

    # 2. sentinel_sidecar has intercept_create_account_token with right kwargs
    k2c_method: ast.AST | None = None
    for node in ast.walk(sc_tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "intercept_create_account_token"
        ):
            k2c_method = node
            break
    if k2c_method is None:
        print("FAIL: sentinel_sidecar.intercept_create_account_token missing")
        return 1
    kwargs_present = {a.arg for a in k2c_method.args.kwonlyargs}
    required = {"device_id", "name", "birthdate", "caller_cookies"}
    if not required.issubset(kwargs_present):
        print(
            f"FAIL: intercept_create_account_token missing kwargs: "
            f"{required - kwargs_present}"
        )
        return 1
    print(
        f"OK: intercept_create_account_token signature has "
        f"{sorted(required)}"
    )

    # 3. request_phase calls intercept_create_account_token + ordering
    run_fn = None
    for node in ast.walk(rp_tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "_run_request_phase_sync"
        ):
            run_fn = node
            break
    if run_fn is None:
        print("FAIL: _run_request_phase_sync not found")
        return 1

    k2c_lineno: int | None = None
    so_token_lineno: int | None = None
    k2c_kwargs: set[str] = set()
    for node in ast.walk(run_fn):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        func = node.func
        if not isinstance(func.value, ast.Name) or func.value.id != "sidecar":
            continue
        if func.attr == "intercept_create_account_token":
            if k2c_lineno is None or node.lineno < k2c_lineno:
                k2c_lineno = node.lineno
                k2c_kwargs = {kw.arg for kw in node.keywords if kw.arg}
        elif func.attr == "get_so_token":
            if so_token_lineno is None or node.lineno < so_token_lineno:
                so_token_lineno = node.lineno

    if k2c_lineno is None:
        print("FAIL: K2c call (sidecar.intercept_create_account_token) missing in _run_request_phase_sync")
        return 1
    if so_token_lineno is None:
        print("FAIL: sidecar.get_so_token fallback call missing")
        return 1
    if k2c_lineno >= so_token_lineno:
        print(
            f"FAIL: K2c (line {k2c_lineno}) must come BEFORE "
            f"get_so_token fallback (line {so_token_lineno})"
        )
        return 1
    print(
        f"OK: K2c call (line {k2c_lineno}) precedes "
        f"get_so_token fallback (line {so_token_lineno})"
    )

    # K2c kwargs
    required_call = {"device_id", "name", "birthdate", "caller_cookies"}
    missing = required_call - k2c_kwargs
    if missing:
        print(f"FAIL: K2c call missing kwargs: {missing}")
        return 1
    print(f"OK: K2c call passes {sorted(required_call)}")

    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
