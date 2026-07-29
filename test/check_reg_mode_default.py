"""Verify default reg_mode = 'browser' across backend modules.

Chạy: python3 test/check_reg_mode_default.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _parse(path: Path) -> ast.Module:
    src = path.read_text(encoding="utf-8")
    return ast.parse(src, filename=str(path))


def check_models_signup_request() -> None:
    """models.py SignupRequest.reg_mode default phải là 'browser'."""
    mod = _parse(ROOT / "models.py")
    for node in ast.walk(mod):
        if isinstance(node, ast.ClassDef) and node.name == "SignupRequest":
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) and item.target.id == "reg_mode":
                    # Field(default="browser", ...) → kw default
                    assert isinstance(item.value, ast.Call), "reg_mode value must be Field(...) call"
                    found = None
                    for kw in item.value.keywords:
                        if kw.arg == "default":
                            found = ast.literal_eval(kw.value)
                    assert found == "browser", f"models.SignupRequest.reg_mode default expected 'browser', got {found!r}"
                    print("[PASS] models.SignupRequest.reg_mode default == 'browser'", flush=True)
                    return
    raise AssertionError("models.SignupRequest.reg_mode field not found")


def check_dataclass_default(path: Path, class_name: str) -> None:
    """Class `class_name` (dataclass hoặc pydantic BaseModel) — reg_mode default 'browser'."""
    mod = _parse(path)
    for node in ast.walk(mod):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) and item.target.id == "reg_mode":
                    val: object
                    if isinstance(item.value, ast.Call):
                        # pydantic Field(default=..., ...)
                        val = None
                        for kw in item.value.keywords:
                            if kw.arg == "default":
                                val = ast.literal_eval(kw.value)
                    else:
                        val = ast.literal_eval(item.value) if item.value else None
                    assert val == "browser", f"{path.name}::{class_name}.reg_mode default expected 'browser', got {val!r}"
                    print(f"[PASS] {path.name}::{class_name}.reg_mode default == 'browser'", flush=True)
                    return
            raise AssertionError(f"{class_name} has no reg_mode field")
    raise AssertionError(f"class {class_name} not found in {path}")


def check_function_param_default(path: Path, func_name: str, *, scope_class: str | None = None) -> None:
    """Hàm `func_name` (có thể nested trong class) — param reg_mode default 'browser'."""
    mod = _parse(path)
    targets: list[ast.FunctionDef] = []
    if scope_class:
        for node in ast.walk(mod):
            if isinstance(node, ast.ClassDef) and node.name == scope_class:
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == func_name:
                        targets.append(item)
    else:
        for node in ast.walk(mod):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
                targets.append(node)
    if not targets:
        scope = f"{scope_class}." if scope_class else ""
        raise AssertionError(f"function {scope}{func_name} not found in {path}")
    for fn in targets:
        # Tìm reg_mode trong args + kwonlyargs
        args = list(fn.args.args) + list(fn.args.kwonlyargs)
        defaults = list(fn.args.defaults) + list(fn.args.kw_defaults)
        # Pad defaults cho positional args
        pos_defaults: list = [None] * (len(fn.args.args) - len(fn.args.defaults)) + list(fn.args.defaults)
        kw_defaults: list = list(fn.args.kw_defaults)
        all_args = list(fn.args.args) + list(fn.args.kwonlyargs)
        all_defaults = pos_defaults + kw_defaults
        for arg, default in zip(all_args, all_defaults):
            if arg.arg == "reg_mode" and default is not None:
                val = ast.literal_eval(default)
                assert val == "browser", (
                    f"{path.name}::{scope_class+'.' if scope_class else ''}{func_name} "
                    f"reg_mode default expected 'browser', got {val!r}"
                )
                scope = f"{scope_class}." if scope_class else ""
                print(f"[PASS] {path.name}::{scope}{func_name}(reg_mode=...) default == 'browser'", flush=True)
                return
    scope = f"{scope_class}." if scope_class else ""
    raise AssertionError(f"reg_mode default not found in {scope}{func_name}")


def main() -> int:
    print("[check_reg_mode_default] start", flush=True)
    try:
        check_models_signup_request()

        # web/manager.py — 3 dataclass: Job, SessionJob, LinkJob
        manager = ROOT / "web" / "manager.py"
        check_dataclass_default(manager, "Job")
        check_dataclass_default(manager, "SessionJob")
        check_dataclass_default(manager, "LinkJob")

        # web/manager.py — 3 add_jobs methods + 1 _parse_combo helper
        check_function_param_default(manager, "add_jobs", scope_class="JobManager")
        check_function_param_default(manager, "add_jobs", scope_class="SessionJobManager")
        check_function_param_default(manager, "add_jobs", scope_class="LinkJobManager")
        check_function_param_default(manager, "_parse_combo", scope_class="LinkJobManager")

        # web/mail_modes.py — 4 builder functions
        mail_modes = ROOT / "web" / "mail_modes.py"
        check_function_param_default(mail_modes, "_build_outlook_request")
        check_function_param_default(mail_modes, "_build_worker_request")
        check_function_param_default(mail_modes, "_build_gmail_advanced_request")
        check_function_param_default(mail_modes, "_build_dongvanfb_request")

        # web/server.py — 2 BaseModel: AddSessionJobsRequest, AddLinkJobsRequest
        server = ROOT / "web" / "server.py"
        check_dataclass_default(server, "AddSessionJobsRequest")
        check_dataclass_default(server, "AddLinkJobsRequest")

        print("[ALL PASS] reg_mode default = 'browser' across backend", flush=True)
        return 0
    except AssertionError as exc:
        print(f"[FAIL] {exc}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
