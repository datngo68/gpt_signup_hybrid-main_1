"""Chẩn đoán: web.server (CLI import) vs gpt_signup_hybrid.web.server (uvicorn
import string) có phải CÙNG module object không.

Nếu KHÁC → set_hide_reg() set trên module này nhưng route index() chạy trên
module kia → flag không có tác dụng. Đây là root cause của bug "launch flag
(--hide-reg) không tác động UI". Fix: truyền qua os.environ.

Run: python3 test/check_web_module_identity.py
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))           # cho `import web.server` (như cli.py)
sys.path.insert(0, str(ROOT.parent))    # cho `import gpt_signup_hybrid` (folder name)


def main() -> int:
    import web.server as a  # noqa: WPS433

    try:
        b = importlib.import_module("gpt_signup_hybrid.web.server")
    except Exception as e:  # noqa: BLE001
        print(f"[INFO] không import được gpt_signup_hybrid.web.server: {e}")
        print("[INFO] không thể tái hiện đúng môi trường uvicorn ở test này.")
        return 0

    a.set_hide_reg(True)

    same = a is b
    a_enabled = a._hide_reg_enabled()
    b_enabled = b._hide_reg_enabled()
    print(f"a (web.server) id           = {id(a)}")
    print(f"b (gpt_signup_hybrid...)  id = {id(b)}")
    print(f"same module object?           {same}")
    print(f"a._hide_reg (raw)           = {getattr(a, '_hide_reg', 'MISSING')}")
    print(f"b._hide_reg (raw)           = {getattr(b, '_hide_reg', 'MISSING')}")
    print(f"a._hide_reg_enabled()       = {a_enabled}")
    print(f"b._hide_reg_enabled()       = {b_enabled}")

    # Fix qua env: dù 2 module khác nhau, set_hide_reg() ghi os.environ nên
    # _hide_reg_enabled() ở module mà uvicorn dùng (b) phải thấy True.
    if b_enabled is True:
        print("\n>>> FIX OK: set_hide_reg() lan qua env → module uvicorn (b) đọc được True.")
        return 0
    print("\n>>> VẪN LỖI: module uvicorn không thấy flag — fix chưa hiệu lực.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
