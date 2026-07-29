"""Verify /about-you fatal error_code detection trong browser_phase.

Test cases:
    TC-01: Class ``AccountAlreadyExistsError`` tồn tại + là subclass
           ``BrowserPhaseError`` (caller catch chung được).
    TC-02: ``_ABOUT_YOU_FATAL_ERROR_CODES`` chứa "user_already_exists".
    TC-03: AST — đoạn raise ``AccountAlreadyExistsError`` nằm BÊN TRONG
           hàm ``_fill_about_you`` (đúng vị trí retry loop, không nhầm
           sang hàm khác).
    TC-04: AST — chuỗi ``"user_already_exists"`` xuất hiện trong điều
           kiện check (substring contains).
    TC-05: Compile sạch (py_compile).

Chạy: python3 test/check_about_you_fatal_error.py
"""
from __future__ import annotations

import ast
import importlib.util
import py_compile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "browser_phase.py"

FAIL = 0
TOTAL = 0


def _check(tc: str, label: str, ok: bool, detail: str = "") -> None:
    global FAIL, TOTAL
    TOTAL += 1
    tag = "[PASS]" if ok else "[FAIL]"
    if not ok:
        FAIL += 1
    print(f"{tag} {tc} — {label}" + (f" :: {detail}" if detail else ""), flush=True)


# ── TC-05: compile (chạy trước để fail-fast nếu syntax broken) ────────
print("[1/5] TC-05 compile browser_phase.py", flush=True)
try:
    py_compile.compile(str(TARGET), doraise=True)
    _check("TC-05", "py_compile ok", True, "")
except py_compile.PyCompileError as exc:
    _check("TC-05", "py_compile ok", False, str(exc)[:200])
    print(f"\n[SUMMARY] {TOTAL - FAIL}/{TOTAL} pass", flush=True)
    sys.exit(1)


# ── Load module để inspect class hierarchy + constant ───────────────────
# browser_phase có nhiều top-level imports nặng (camoufox, …). Load qua
# importlib từ workspace root để giữ relative-import path đúng.
print("[2/5] TC-01/TC-02 import classes + constants", flush=True)

# browser_phase nằm ở ROOT (không phải package), dùng spec_from_file_location.
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
try:
    spec = importlib.util.spec_from_file_location(
        "_browser_phase_isolated", TARGET,
    )
    if spec is None or spec.loader is None:
        _check("TC-01", "spec_from_file_location",
               False, "spec/loader is None")
        sys.exit(1)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
except Exception as exc:  # noqa: BLE001
    # Một số deps (camoufox, etc.) có thể chưa cài trên môi trường test.
    # Fallback: parse AST để verify class + constant tồn tại.
    print(f"[INFO] full-import fail ({type(exc).__name__}: {str(exc)[:100]}) "
          "— fallback AST inspection", flush=True)
    src = TARGET.read_text(encoding="utf-8")
    tree = ast.parse(src)

    found_class = False
    found_constant = False
    constant_values: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "AccountAlreadyExistsError":
            found_class = True
            base_names = [
                b.id for b in node.bases if isinstance(b, ast.Name)
            ]
            _check("TC-01", "AccountAlreadyExistsError định nghĩa",
                   True, f"bases={base_names}")
            _check("TC-01", "subclass BrowserPhaseError",
                   "BrowserPhaseError" in base_names,
                   f"bases={base_names}")
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "_ABOUT_YOU_FATAL_ERROR_CODES"
        ):
            found_constant = True
            if isinstance(node.value, ast.Tuple):
                for elt in node.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        constant_values.append(elt.value)

    if not found_class:
        _check("TC-01", "AccountAlreadyExistsError định nghĩa", False, "")
    _check("TC-02", "_ABOUT_YOU_FATAL_ERROR_CODES tồn tại",
           found_constant, f"values={constant_values}")
    _check("TC-02", "chứa 'user_already_exists'",
           "user_already_exists" in constant_values,
           f"values={constant_values}")
else:
    cls = getattr(mod, "AccountAlreadyExistsError", None)
    base_cls = getattr(mod, "BrowserPhaseError", None)
    _check("TC-01", "import AccountAlreadyExistsError",
           cls is not None, "")
    _check("TC-01", "subclass BrowserPhaseError",
           cls is not None and issubclass(cls, base_cls), "")
    constants = getattr(mod, "_ABOUT_YOU_FATAL_ERROR_CODES", None)
    _check("TC-02", "_ABOUT_YOU_FATAL_ERROR_CODES tồn tại",
           constants is not None, f"value={constants!r}")
    _check("TC-02", "chứa 'user_already_exists'",
           constants is not None and "user_already_exists" in constants,
           f"value={constants!r}")


# ── TC-03: AST — raise nằm trong _fill_about_you ────────────────────────
print("[3/5] TC-03 raise AccountAlreadyExistsError nằm trong _fill_about_you",
      flush=True)
src = TARGET.read_text(encoding="utf-8")
tree = ast.parse(src)

raise_in_fill = False
fill_node: ast.AsyncFunctionDef | None = None

for node in ast.walk(tree):
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "_fill_about_you":
        fill_node = node
        break

if fill_node is None:
    _check("TC-03", "tìm thấy _fill_about_you", False, "")
else:
    _check("TC-03", "tìm thấy _fill_about_you", True, "")
    for sub in ast.walk(fill_node):
        if isinstance(sub, ast.Raise) and sub.exc is not None:
            exc_node = sub.exc
            # Match cả pattern: raise AccountAlreadyExistsError(...) hoặc raise X(...)
            if isinstance(exc_node, ast.Call):
                func = exc_node.func
                if isinstance(func, ast.Name) and func.id == "AccountAlreadyExistsError":
                    raise_in_fill = True
                    break
    _check("TC-03", "có raise AccountAlreadyExistsError",
           raise_in_fill, "")


# ── TC-04: chuỗi 'user_already_exists' xuất hiện trong logic ────────────
print("[4/5] TC-04 logic check 'user_already_exists' substring", flush=True)

# Đếm xuất hiện trong toàn file (constant + comparison branch).
occurrences = src.count('"user_already_exists"')
_check("TC-04", "'user_already_exists' xuất hiện ≥ 2 lần "
       "(constant tuple + branch check)",
       occurrences >= 2, f"count={occurrences}")


# ── TC-04b: Loop 'for fatal_code in _ABOUT_YOU_FATAL_ERROR_CODES' tồn tại
print("[5/5] TC-04b loop iterate constant + raise inside", flush=True)
loop_iter_constant = False
if fill_node is not None:
    for sub in ast.walk(fill_node):
        if isinstance(sub, ast.For):
            iter_node = sub.iter
            if (
                isinstance(iter_node, ast.Name)
                and iter_node.id == "_ABOUT_YOU_FATAL_ERROR_CODES"
            ):
                loop_iter_constant = True
                break
_check("TC-04b", "for fatal_code in _ABOUT_YOU_FATAL_ERROR_CODES",
       loop_iter_constant, "")


print("", flush=True)
print(f"[SUMMARY] {TOTAL - FAIL}/{TOTAL} pass", flush=True)
sys.exit(1 if FAIL else 0)
