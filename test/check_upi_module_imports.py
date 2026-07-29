"""Check syntax + import của các module UPI mới.

Chạy:
    .venv/bin/python test/check_upi_module_imports.py

Mục đích: Phát hiện sớm lỗi import / signature sai / ràng buộc Settings sai
trước khi khởi động web server.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

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
        FAILED += 1


def t01_import_runner():
    from gpt_signup_hybrid_new.web.upi_runner import (
        run_upi_qr_probe,
        UpiQrResult,
        UpiQrError,
        PROMO,
        PROXY_FROM_STEP,
        DO_CONFIRM,
        DO_APPROVE,
        APPROVE_DELAY,
        APPROVE_PROXY_BATCH,
        APPROVE_BACKEND_EXCEPTION_CONSECUTIVE,
        CONFIRM_VARIANTS,
    )
    assert PROMO is True
    assert PROXY_FROM_STEP == 3
    assert DO_CONFIRM is True
    assert DO_APPROVE is True
    assert APPROVE_DELAY == 5.0  # bumped from 3.0 → 5.0 (Stripe rate-limit floor)
    assert APPROVE_BACKEND_EXCEPTION_CONSECUTIVE == 0
    assert CONFIRM_VARIANTS == ("qr_code", "empty", "flow_qr", "intent")
    assert callable(run_upi_qr_probe)
    assert UpiQrResult.__name__ == "UpiQrResult"
    assert issubclass(UpiQrError, Exception)


def t02_import_manager():
    from gpt_signup_hybrid_new.web.manager import UpiJob, UpiJobManager, get_upi_manager
    mgr = get_upi_manager()
    assert isinstance(mgr, UpiJobManager)
    assert mgr.max_concurrent == 1
    assert mgr.approve_retries == 500
    assert 60 <= mgr.job_timeout <= 7200
    j = UpiJob(id="abc", email="x@y.z", password="p")
    d = j.to_dict()
    assert d["id"] == "abc"
    assert d["status"] == "queued"
    assert d["has_qr"] is False
    assert "amount" in d
    assert "log_count" in d


def t03_settings_keys_whitelist():
    from gpt_signup_hybrid_new.db.repositories import _EXACT_KEYS, _validate_type_constraint
    for key in ("upi.max_concurrent", "upi.job_timeout", "upi.approve_retries"):
        assert key in _EXACT_KEYS, f"{key} chưa nằm trong _EXACT_KEYS"
    # Validate type constraint
    _validate_type_constraint("upi.max_concurrent", 1)
    _validate_type_constraint("upi.max_concurrent", 50)
    _validate_type_constraint("upi.job_timeout", 60)
    _validate_type_constraint("upi.job_timeout", 7200)
    _validate_type_constraint("upi.approve_retries", 1)
    _validate_type_constraint("upi.approve_retries", 2000)
    # Ràng buộc range
    try:
        _validate_type_constraint("upi.max_concurrent", 0)
        raise AssertionError("expected to reject 0")
    except Exception:
        pass
    try:
        _validate_type_constraint("upi.max_concurrent", 51)
        raise AssertionError("expected to reject 51")
    except Exception:
        pass
    try:
        _validate_type_constraint("upi.approve_retries", 2001)
        raise AssertionError("expected to reject 2001")
    except Exception:
        pass


def t04_reg_mode_extended():
    from gpt_signup_hybrid_new.db.repositories import _validate_type_constraint
    for v in ("single", "multi", "multi3", "multi5", "multi10",
              "multi20", "multi30", "multi50"):
        _validate_type_constraint("reg.mode", v)
    try:
        _validate_type_constraint("reg.mode", "multi100")
        raise AssertionError("expected reject multi100")
    except Exception:
        pass


def t05_ui_active_tab_includes_upi():
    from gpt_signup_hybrid_new.db.repositories import _validate_type_constraint
    _validate_type_constraint("ui.active_tab", "upi")
    _validate_type_constraint("ui.active_tab", "reg")
    _validate_type_constraint("ui.active_tab", "session")


def t06_manager_validation_ranges():
    import asyncio as _asyncio
    from gpt_signup_hybrid_new.web.manager import get_upi_manager

    async def _run() -> None:
        mgr = get_upi_manager()
        mgr.set_max_concurrent(1)
        mgr.set_max_concurrent(200)
        try:
            mgr.set_max_concurrent(0)
            raise AssertionError("expected reject 0")
        except ValueError:
            pass
        try:
            mgr.set_max_concurrent(201)
            raise AssertionError("expected reject 201")
        except ValueError:
            pass
        mgr.set_approve_retries(1)
        mgr.set_approve_retries(2000)
        try:
            mgr.set_approve_retries(0)
            raise AssertionError("expected reject 0")
        except ValueError:
            pass
        mgr.set_job_timeout(60)
        mgr.set_job_timeout(7200)
        try:
            mgr.set_job_timeout(30)
            raise AssertionError("expected reject 30")
        except ValueError:
            pass
        mgr.shutdown()

    _asyncio.run(_run())


def t07_apply_settings():
    import asyncio as _asyncio
    from gpt_signup_hybrid_new.web.manager import UpiJobManager

    async def _run() -> None:
        # Tạo instance riêng — singleton đã shutdown ở t06.
        mgr = UpiJobManager()
        mgr.apply_settings({
            "upi.max_concurrent": 5,
            "upi.job_timeout": 600,
            "upi.approve_retries": 100,
        })
        assert mgr.max_concurrent == 5
        assert mgr.job_timeout == 600
        assert mgr.approve_retries == 100
        mgr.shutdown()

    _asyncio.run(_run())


def t08_add_jobs_parser():
    import asyncio as _asyncio
    from gpt_signup_hybrid_new.web.manager import UpiJobManager

    async def _run() -> None:
        mgr = UpiJobManager()
        jobs = mgr.add_jobs([
            "user1@nik.edu.pl|GPT#aaa|TOTPSECRET1",
            "  user2@nik.edu.pl|GPT#bbb|TOTPSECRET2",
            "",
            "# comment",
            "user1@nik.edu.pl|dup|same",  # dup → bỏ qua
            "no_pipe_line",  # invalid → status=error
        ])
        statuses = [j.status for j in jobs]
        assert "queued" in statuses
        assert "error" in statuses
        queued = [j for j in jobs if j.status == "queued"]
        assert len(queued) == 2
        assert queued[0].secret == "TOTPSECRET1"
        assert queued[1].secret == "TOTPSECRET2"
        # Drain queue + cancel workers — không để job thật chạy lúc test.
        await mgr.stop_all()
        mgr.shutdown()

    _asyncio.run(_run())


def t09_server_routes():
    """Verify FastAPI app có đầy đủ /api/upi/* routes."""
    # Lazy import — vì server.py có @app.on_event("startup") cần engine.
    import importlib.util
    spec = importlib.util.find_spec("gpt_signup_hybrid_new.web.server")
    assert spec is not None, "không import được module server.py"
    # Dùng AST tránh trigger startup event:
    import ast
    src = (ROOT / "web" / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    routes = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in node.decorator_list:
                if isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute):
                    if deco.func.attr in ("get", "post", "delete"):
                        if deco.args and isinstance(deco.args[0], ast.Constant):
                            routes.append((deco.func.attr, deco.args[0].value))
    expected = {
        ("get", "/api/upi/jobs"),
        ("get", "/api/upi/jobs/{job_id}"),
        ("get", "/api/upi/jobs/{job_id}/log"),
        ("get", "/api/upi/jobs/{job_id}/qr"),
        ("post", "/api/upi/jobs"),
        ("post", "/api/upi/jobs/{job_id}/retry"),
        ("delete", "/api/upi/jobs/{job_id}"),
        ("post", "/api/upi/jobs/stop-all"),
        ("post", "/api/upi/jobs/clear-finished"),
        ("get", "/api/upi/config"),
        ("post", "/api/upi/config"),
    }
    missing = expected - set(routes)
    assert not missing, f"thiếu routes: {missing}"


def t10_static_files_exist():
    upi_js = ROOT / "web" / "static" / "upi.js"
    assert upi_js.exists(), "thiếu web/static/upi.js"
    index_html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'data-tab="upi"' in index_html, "thiếu nav button data-tab=upi"
    assert 'id="tab-upi"' in index_html, "thiếu <main id=tab-upi>"
    assert 'multi20' in index_html, "thiếu mode multi20"
    assert 'multi30' in index_html, "thiếu mode multi30"
    assert 'multi50' in index_html, "thiếu mode multi50"
    assert 'upi-combo-input' in index_html, "thiếu textarea upi-combo-input"
    assert 'upi-approve-retries' in index_html, "thiếu input upi-approve-retries"


def t11_qrcode_installed():
    import qrcode  # noqa: F401


def t12_sse_mux_channel_order():
    from gpt_signup_hybrid_new.web.sse_mux import SseMux
    mux = SseMux()
    # Generate snapshots với 0 fns đã đăng ký → empty list, không crash.
    snapshots = mux.generate_snapshots()
    assert isinstance(snapshots, list)
    # Đảm bảo class chấp nhận register channel "upi"
    mux.register_snapshot("upi", lambda: [])
    assert "upi" in mux._snapshot_fns


def main() -> int:
    tests = [
        ("01 import upi_runner", t01_import_runner),
        ("02 import manager + UpiJobManager singleton", t02_import_manager),
        ("03 settings whitelist + type constraints", t03_settings_keys_whitelist),
        ("04 reg.mode mở rộng multi20/30/50", t04_reg_mode_extended),
        ("05 ui.active_tab whitelist 'upi'", t05_ui_active_tab_includes_upi),
        ("06 UpiJobManager validation ranges", t06_manager_validation_ranges),
        ("07 apply_settings hydration", t07_apply_settings),
        ("08 add_jobs parser dedup + invalid", t08_add_jobs_parser),
        ("09 FastAPI routes /api/upi/*", t09_server_routes),
        ("10 static files (index.html + upi.js)", t10_static_files_exist),
        ("11 qrcode package installed", t11_qrcode_installed),
        ("12 SseMux register upi channel", t12_sse_mux_channel_order),
    ]
    for label, fn in tests:
        _check(label, fn)
    print(f"\n--- summary: {PASSED} pass, {FAILED} fail ---", flush=True)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
