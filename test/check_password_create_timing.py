"""Verify P2 changes trong password_create branch của _drive_signup_flow.

Kiểm tra:
  1. Syntax OK (AST parse)
  2. password_create branch KHÔNG còn `random_mouse_wander` call
  3. `_wait_oai_sc` timeout 8.0 (giảm từ 15)
  4. Password input selector loop có timeout 15000 cho selector đầu
  5. Thứ tự execution: find input → wait oai-sc → dwell → human_type
     (KHÔNG còn pattern oai-sc → mouse_wander → find input)
  6. Submit button is_enabled timeout 2000 (tăng từ 500)
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BROWSER_PHASE = ROOT / "browser_phase.py"


def _get_password_create_branch(src: str) -> str:
    """Trích đoạn source từ ``if screen == "password_create":`` cho tới
    branch kế (``if screen ==`` tiếp theo) hoặc end of _drive_signup_flow.
    """
    start_re = re.compile(r'^\s*if screen == "password_create":\s*$', re.MULTILINE)
    m = start_re.search(src)
    if not m:
        raise SystemExit("FAIL: không tìm thấy `if screen == \"password_create\":` branch")
    start = m.start()
    # Tìm branch kế tiếp `if screen ==` ở cùng indent (= 8 spaces dựa code base)
    next_re = re.compile(r'\n(\s{8})if screen == "', re.MULTILINE)
    m2 = next_re.search(src, m.end())
    end = m2.start() if m2 else len(src)
    return src[start:end]


def main() -> int:
    print("[1/6] AST parse browser_phase.py...")
    src = BROWSER_PHASE.read_text()
    ast.parse(src, filename=str(BROWSER_PHASE))
    print("      ✓ syntax OK")

    branch = _get_password_create_branch(src)
    print(f"      branch length: {len(branch)} chars")

    print("[2/6] password_create branch KHÔNG còn random_mouse_wander call...")
    if "random_mouse_wander(" in branch:
        raise SystemExit(
            "FAIL: password_create vẫn còn `random_mouse_wander(` call"
        )
    print("      ✓ random_mouse_wander đã bỏ")

    print("[3/6] _wait_oai_sc timeout = 8.0...")
    # Match: _wait_oai_sc(ctx, timeout_seconds=8.0, ...)
    if not re.search(r"_wait_oai_sc\(ctx,\s*timeout_seconds\s*=\s*8\.0\b", branch):
        raise SystemExit(
            "FAIL: _wait_oai_sc trong password_create không đúng timeout=8.0"
        )
    print("      ✓ _wait_oai_sc timeout 8.0")

    print("[4/6] Password input selector đầu có timeout 15000...")
    # Match sel_timeout = 15000 if idx == 0 else ... 
    if not re.search(r"15000\s+if\s+idx\s*==\s*0", branch):
        raise SystemExit(
            "FAIL: selector đầu không có timeout 15000 (cho SPA render)"
        )
    print("      ✓ selector đầu timeout 15000ms (cho SPA render)")

    print("[5/6] Thứ tự: find input TRƯỚC wait oai-sc TRƯỚC human_type...")
    idx_find = branch.find("pwd_input = None")
    idx_oai = branch.find("_wait_oai_sc(")
    idx_type = branch.find("human_type(")
    if not (0 <= idx_find < idx_oai < idx_type):
        raise SystemExit(
            f"FAIL: thứ tự sai. find={idx_find} oai_sc={idx_oai} type={idx_type}"
        )
    print(f"      ✓ find input ({idx_find}) → wait oai-sc ({idx_oai}) → human_type ({idx_type})")

    print("[6/6] Submit button is_enabled timeout 2000ms...")
    # Tìm trong branch — pattern is_enabled(timeout=2000) hoặc tương đương
    if not re.search(r"is_enabled\(timeout\s*=\s*2000\b", branch):
        raise SystemExit(
            "FAIL: submit button is_enabled chưa tăng timeout lên 2000"
        )
    # Đảm bảo KHÔNG còn `is_enabled(timeout=500)` trong password_create branch
    if re.search(r"is_enabled\(timeout\s*=\s*500\b", branch):
        raise SystemExit(
            "FAIL: vẫn còn is_enabled(timeout=500) trong password_create branch"
        )
    print("      ✓ submit is_enabled timeout 2000ms")

    print("\nAll P2 checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
