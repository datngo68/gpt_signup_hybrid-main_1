"""Verify syntax + import + signature mới của upi_runner restart logic.

Chạy: python3 test/check_upi_restart_logic.py
"""
from __future__ import annotations

import ast
import inspect
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJ = ROOT.parent  # parent for `gpt_signup_hybrid_new` package
sys.path.insert(0, str(PROJ))

PKG = "gpt_signup_hybrid_new"


def _ok(idx: str, label: str, detail: str = "") -> None:
    print(f"[PASS] {idx} — {label} :: {detail}", flush=True)


def _fail(idx: str, label: str, detail: str) -> None:
    print(f"[FAIL] {idx} — {label} :: {detail}", flush=True)


def t01_ast_parse() -> bool:
    """Parse upi_runner.py + manager.py + server.py qua AST."""
    files = [
        ROOT / "web" / "upi_runner.py",
        ROOT / "web" / "manager.py",
        ROOT / "web" / "server.py",
        ROOT / "db" / "repositories.py",
    ]
    for f in files:
        try:
            ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        except SyntaxError as exc:
            _fail("TC-01", f"AST parse {f.name}", f"{exc.lineno}: {exc.msg}")
            return False
    _ok("TC-01", "AST parse 4 files", "ok")
    return True


def t02_import_runner() -> bool:
    """Import module + check symbols mới."""
    try:
        from gpt_signup_hybrid_new.web.upi_runner import (
            APPROVE_BACKEND_EXCEPTION_CONSECUTIVE,
            APPROVE_DELAY,
            APPROVE_MAX_RESTARTS,
            APPROVE_PROXY_BATCH,
            APPROVE_RESTART_THRESHOLD,
            CONFIRM_VARIANTS,
            UpiQrError,
            UpiQrResult,
            run_upi_qr_probe,
        )
    except Exception as exc:  # noqa: BLE001
        _fail("TC-02", "import upi_runner", f"{type(exc).__name__}: {exc}")
        return False
    assert APPROVE_BACKEND_EXCEPTION_CONSECUTIVE == 0
    assert APPROVE_RESTART_THRESHOLD == 30, APPROVE_RESTART_THRESHOLD
    assert APPROVE_MAX_RESTARTS == 3, APPROVE_MAX_RESTARTS
    assert callable(run_upi_qr_probe)
    _ok("TC-02", "import upi_runner",
        f"defaults restart_threshold={APPROVE_RESTART_THRESHOLD} max_restarts={APPROVE_MAX_RESTARTS}")
    return True


def t03_signature() -> bool:
    """run_upi_qr_probe có 2 params mới: restart_threshold, max_restarts."""
    from gpt_signup_hybrid_new.web.upi_runner import run_upi_qr_probe

    sig = inspect.signature(run_upi_qr_probe)
    params = sig.parameters
    if "restart_threshold" not in params:
        _fail("TC-03", "signature", "missing restart_threshold param")
        return False
    if "max_restarts" not in params:
        _fail("TC-03", "signature", "missing max_restarts param")
        return False
    rt = params["restart_threshold"]
    mr = params["max_restarts"]
    if rt.default != 0 or mr.default != 0:
        _fail("TC-03", "signature defaults",
              f"expected 0/0, got {rt.default}/{mr.default}")
        return False
    _ok("TC-03", "signature",
        f"restart_threshold (default={rt.default}) max_restarts (default={mr.default})")
    return True


def t04_settings_keys_whitelist() -> bool:
    from gpt_signup_hybrid_new.db.repositories import (
        _EXACT_KEYS,
        _validate_type_constraint,
    )

    keys = ("upi.approve.restart_threshold", "upi.approve.max_restarts")
    for k in keys:
        if k not in _EXACT_KEYS:
            _fail("TC-04", "whitelist", f"{k} chưa có trong _EXACT_KEYS")
            return False

    # Validate range OK
    _validate_type_constraint("upi.approve.restart_threshold", 0)
    _validate_type_constraint("upi.approve.restart_threshold", 1000)
    _validate_type_constraint("upi.approve.max_restarts", 0)
    _validate_type_constraint("upi.approve.max_restarts", 2000)  # ceiling mở từ 100 lên 2000

    # Validate range fail
    for k, bad in [
        ("upi.approve.restart_threshold", -1),
        ("upi.approve.restart_threshold", 1001),
        ("upi.approve.max_restarts", -1),
        ("upi.approve.max_restarts", 2001),
    ]:
        try:
            _validate_type_constraint(k, bad)
        except Exception:
            continue
        _fail("TC-04", "validator range", f"{k}={bad} expected reject, accepted")
        return False

    # Type fail
    try:
        _validate_type_constraint("upi.approve.restart_threshold", "30")
    except Exception:
        pass
    else:
        _fail("TC-04", "validator type", "string accepted for int field")
        return False

    _ok("TC-04", "settings whitelist + validators", "2 keys OK, range + type checks OK")
    return True


def t05_manager_setters() -> bool:
    from gpt_signup_hybrid_new.web.manager import UpiJobManager

    mgr = UpiJobManager(max_concurrent=1)
    try:
        # Defaults — đổi từ 30/3 sang 1/500 (mỗi exception là 1 restart, quota
        # ≈ approve_retries → không cạn giữa job).
        assert mgr.restart_threshold == 1, mgr.restart_threshold
        assert mgr.max_restarts == 500, mgr.max_restarts

        # Setters — ceiling max_restarts đã mở từ 100 lên 2000.
        mgr.set_restart_threshold(0)
        mgr.set_restart_threshold(1000)
        mgr.set_max_restarts(0)
        mgr.set_max_restarts(2000)

        # Range fail
        for fn, bad in [
            (mgr.set_restart_threshold, -1),
            (mgr.set_restart_threshold, 1001),
            (mgr.set_max_restarts, -1),
            (mgr.set_max_restarts, 2001),
        ]:
            try:
                fn(bad)
            except ValueError:
                continue
            _fail("TC-05", "setter range", f"{fn.__name__}({bad}) accepted")
            return False

        # apply_settings hydrate
        mgr.apply_settings({
            "upi.approve.restart_threshold": 50,
            "upi.approve.max_restarts": 5,
        })
        assert mgr.restart_threshold == 50, mgr.restart_threshold
        assert mgr.max_restarts == 5, mgr.max_restarts
    finally:
        mgr.shutdown()

    _ok("TC-05", "UpiJobManager setters + apply_settings", "ok")
    return True


def t06_result_field() -> bool:
    from gpt_signup_hybrid_new.web.upi_runner import UpiQrResult

    r = UpiQrResult(ok=True, email="x@y", restart_count=2)
    d = r.to_dict()
    if "restart_count" not in d:
        _fail("TC-06", "to_dict restart_count", "missing key")
        return False
    if d["restart_count"] != 2:
        _fail("TC-06", "to_dict restart_count", f"expected 2, got {d['restart_count']}")
        return False
    _ok("TC-06", "UpiQrResult.restart_count", f"to_dict OK: restart_count={d['restart_count']}")
    return True


def t07_server_payload() -> bool:
    """SetUpiConfigRequest có 2 fields mới với constraints đúng."""
    from gpt_signup_hybrid_new.web.server import SetUpiConfigRequest

    obj = SetUpiConfigRequest(restart_threshold=30, max_restarts=3)
    assert obj.restart_threshold == 30
    assert obj.max_restarts == 3

    # Range fail — ceiling max_restarts mở từ 100 lên 2000 (≥ approve_retries).
    for kw in [
        {"restart_threshold": -1},
        {"restart_threshold": 1001},
        {"max_restarts": -1},
        {"max_restarts": 2001},
    ]:
        try:
            SetUpiConfigRequest(**kw)
        except Exception:
            continue
        _fail("TC-07", "pydantic range", f"{kw} accepted")
        return False
    _ok("TC-07", "SetUpiConfigRequest", "fields + ranges OK")
    return True


def main() -> int:
    cases = [t01_ast_parse, t02_import_runner, t03_signature,
             t04_settings_keys_whitelist, t05_manager_setters,
             t06_result_field, t07_server_payload]
    failed = 0
    for i, fn in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] running {fn.__name__}...", flush=True)
        try:
            ok = fn()
        except Exception:
            traceback.print_exc()
            ok = False
        if not ok:
            failed += 1
    print(f"\nResult: {len(cases) - failed}/{len(cases)} passed", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
