"""Verify thêm option Multi (100), Multi (200) — UPI mở rộng range [1, 200].

Check:
- Syntax Python OK cho db/repositories.py, web/manager.py, web/server.py.
- _allowed_modes cho reg.mode chứa multi100, multi200.
- _validate_type_constraint("upi.max_concurrent", 200) pass.
- _validate_type_constraint("upi.max_concurrent", 201) raise.
- UpiJobManager.set_max_concurrent(200) pass, (201) raise.
- index.html chứa option multi100, multi200.
- app.js / session.js / upi.js modeMap chứa multi100/multi200.

Chạy: python3 test/check_multi_100_200.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))


def _ok(label: str, msg: str = "") -> None:
    print(f"[PASS] {label} :: {msg}", flush=True)


def _fail(label: str, msg: str) -> None:
    print(f"[FAIL] {label} :: {msg}", flush=True)
    sys.exit(1)


def t01_syntax() -> None:
    label = "TC-01 syntax check Python files"
    targets = (
        ROOT / "db" / "repositories.py",
        ROOT / "web" / "manager.py",
        ROOT / "web" / "server.py",
    )
    for p in targets:
        try:
            ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        except SyntaxError as exc:
            _fail(label, f"{p.name}: {exc}")
    _ok(label, "repositories.py + manager.py + server.py parse OK")


def t02_allowed_modes() -> None:
    label = "TC-02 reg.mode whitelist gồm multi100, multi200"
    from gpt_signup_hybrid_new.db.repositories import _validate_type_constraint
    for v in ("multi100", "multi200"):
        try:
            _validate_type_constraint("reg.mode", v)
        except Exception as exc:
            _fail(label, f"{v} bị reject: {exc}")
    _ok(label, "multi100, multi200 accepted")


def t03_upi_range_200_ok() -> None:
    label = "TC-03 upi.max_concurrent accept [1, 200]"
    from gpt_signup_hybrid_new.db.repositories import _validate_type_constraint
    for v in (1, 50, 100, 150, 200):
        try:
            _validate_type_constraint("upi.max_concurrent", v)
        except Exception as exc:
            _fail(label, f"value={v} bị reject: {exc}")
    _ok(label, "1, 50, 100, 150, 200 accepted")


def t04_upi_range_overflow_rejected() -> None:
    label = "TC-04 upi.max_concurrent reject < 1 và > 200"
    from gpt_signup_hybrid_new.db.repositories import _validate_type_constraint
    for v in (0, 201, 500):
        try:
            _validate_type_constraint("upi.max_concurrent", v)
        except Exception:
            continue
        _fail(label, f"value={v} không bị reject")
    _ok(label, "0, 201, 500 bị reject đúng")


def t05_upi_manager_set_200() -> None:
    label = "TC-05 UpiJobManager.set_max_concurrent(200) pass, (201) raise"
    from gpt_signup_hybrid_new.web.manager import UpiJobManager
    mgr = UpiJobManager(max_concurrent=1)
    try:
        # set_max_concurrent gọi _ensure_workers — stub để không cần event loop.
        mgr._ensure_workers = lambda: None  # type: ignore[method-assign]
        mgr.set_max_concurrent(200)
        if mgr.max_concurrent != 200:
            _fail(label, f"set 200 nhưng max_concurrent={mgr.max_concurrent}")
        try:
            mgr.set_max_concurrent(201)
        except ValueError:
            _ok(label, "set(200) ok, set(201) raise đúng")
            return
        _fail(label, "set(201) không raise")
    finally:
        try:
            del mgr
        except Exception:
            pass


def t06_upi_manager_apply_settings_200() -> None:
    label = "TC-06 UpiJobManager.apply_settings hydrate upi.max_concurrent=200"
    from gpt_signup_hybrid_new.web.manager import UpiJobManager
    mgr = UpiJobManager(max_concurrent=1)
    mgr.apply_settings({"upi.max_concurrent": 200})
    if mgr.max_concurrent != 200:
        _fail(label, f"apply 200 nhưng max_concurrent={mgr.max_concurrent}")
    # Out-of-range silent ignore (per pattern hiện tại).
    mgr.apply_settings({"upi.max_concurrent": 201})
    if mgr.max_concurrent != 200:
        _fail(label, f"apply 201 không bị ignore, max_concurrent={mgr.max_concurrent}")
    _ok(label, "hydrate 200 ok, 201 silent-ignore")


def t07_index_html_options() -> None:
    label = "TC-07 index.html chứa option multi100 + multi200"
    body = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
    for needle in ('value="multi100"', 'value="multi200"', "Multi (100)", "Multi (200)"):
        if needle not in body:
            _fail(label, f"thiếu {needle!r}")
    _ok(label, "có đủ 2 option mới")


def t08_js_modemap() -> None:
    label = "TC-08 app.js / session.js / upi.js modeMap chứa multi100/multi200"
    files = (
        ROOT / "web" / "static" / "app.js",
        ROOT / "web" / "static" / "session.js",
        ROOT / "web" / "static" / "upi.js",
    )
    for p in files:
        body = p.read_text(encoding="utf-8")
        for needle in ("multi100: 100", "multi200: 200"):
            if needle not in body:
                _fail(label, f"{p.name} thiếu {needle!r}")
    _ok(label, "cả 3 file JS đã map đủ multi100/multi200")


def t09_server_clamp_200() -> None:
    label = "TC-09 server.py UPI clamp dùng 200"
    body = (ROOT / "web" / "server.py").read_text(encoding="utf-8")
    if "min(payload.max_concurrent, 200)" not in body:
        _fail(label, "không thấy 'min(payload.max_concurrent, 200)'")
    # Đảm bảo Reg vẫn clamp về 2 (không bị đụng nhầm).
    if "min(payload.max_concurrent, 2)" not in body:
        _fail(label, "Reg clamp [1,2] biến mất — coi chừng vỡ regression")
    _ok(label, "UPI clamp 200, Reg vẫn 2")


def main() -> None:
    cases = (
        t01_syntax,
        t02_allowed_modes,
        t03_upi_range_200_ok,
        t04_upi_range_overflow_rejected,
        t05_upi_manager_set_200,
        t06_upi_manager_apply_settings_200,
        t07_index_html_options,
        t08_js_modemap,
        t09_server_clamp_200,
    )
    total = len(cases)
    for idx, fn in enumerate(cases, 1):
        print(f"[{idx}/{total}] running {fn.__name__}", flush=True)
        fn()
    print(f"\n[OK] {total}/{total} cases passed", flush=True)


if __name__ == "__main__":
    main()
