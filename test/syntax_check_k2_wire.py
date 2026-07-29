"""Phase 11.4 K2 wiring verify — AST-parse các file đã sửa.

Verify:
  1. ``request_phase.py`` parse OK (no SyntaxError sau khi wire K2).
  2. ``sentinel_sidecar.py`` parse OK.
  3. Inside ``_run_request_phase_sync``: tồn tại call tới
     ``sidecar.intercept_register_token(...)`` đặt TRƯỚC call tới
     ``sidecar.get_sentinel_token(...)``.
  4. K2 call dùng đúng 3 kwarg: email, device_id, logging_id.

Run: python3 test/syntax_check_k2_wire.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def parse(path: Path) -> ast.Module:
    src = path.read_text(encoding="utf-8")
    return ast.parse(src, filename=str(path))


def find_func(tree: ast.Module, name: str) -> ast.AST | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def main() -> int:
    request_phase = ROOT / "request_phase.py"
    sidecar = ROOT / "sentinel_sidecar.py"

    # 1+2: syntax
    try:
        rp_tree = parse(request_phase)
        sc_tree = parse(sidecar)
    except SyntaxError as exc:
        print(f"FAIL: SyntaxError {exc.filename}:{exc.lineno}: {exc.msg}")
        return 1
    print("OK: AST parse — request_phase.py + sentinel_sidecar.py")

    # 3: ordering trong _run_request_phase_sync
    run_fn = find_func(rp_tree, "_run_request_phase_sync")
    if run_fn is None:
        print("FAIL: _run_request_phase_sync not found")
        return 1

    intercept_lineno: int | None = None
    get_token_lineno: int | None = None
    intercept_kwargs: set[str] = set()
    for node in ast.walk(run_fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        owner = (
            func.value.id if isinstance(func.value, ast.Name) else ""
        )
        if owner != "sidecar":
            continue
        if func.attr == "intercept_register_token":
            if intercept_lineno is None or node.lineno < intercept_lineno:
                intercept_lineno = node.lineno
                intercept_kwargs = {kw.arg for kw in node.keywords if kw.arg}
        elif func.attr == "get_sentinel_token":
            if get_token_lineno is None or node.lineno < get_token_lineno:
                get_token_lineno = node.lineno

    if intercept_lineno is None:
        print("FAIL: sidecar.intercept_register_token call missing in _run_request_phase_sync")
        return 1
    if get_token_lineno is None:
        print("FAIL: sidecar.get_sentinel_token call missing (fallback path expected)")
        return 1
    if intercept_lineno >= get_token_lineno:
        print(
            f"FAIL: K2 (line {intercept_lineno}) must come BEFORE "
            f"get_sentinel_token (line {get_token_lineno})"
        )
        return 1
    print(
        f"OK: K2 intercept (line {intercept_lineno}) precedes "
        f"get_sentinel_token (line {get_token_lineno})"
    )

    # 4: K2 kwargs
    required = {"email", "device_id", "logging_id"}
    missing = required - intercept_kwargs
    if missing:
        print(f"FAIL: K2 missing required kwargs: {missing}")
        return 1
    print(f"OK: K2 kwargs include {sorted(required)}")

    # 5: sentinel_sidecar.intercept_register_token method exists
    sc_method = None
    for node in ast.walk(sc_tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "intercept_register_token"
        ):
            sc_method = node
            break
    if sc_method is None:
        print("FAIL: sentinel_sidecar.intercept_register_token method missing")
        return 1
    method_kwargs = {a.arg for a in sc_method.args.kwonlyargs}
    if not required.issubset(method_kwargs):
        print(
            f"FAIL: intercept_register_token signature missing kwargs: "
            f"{required - method_kwargs}"
        )
        return 1
    print(f"OK: sentinel_sidecar.intercept_register_token signature has {sorted(required)}")

    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
