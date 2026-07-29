"""Verify wiring cờ --hide-reg: setup.sh → CLI web → server inject → FE ẩn tab Reg.

Run: python3 test/check_hide_reg.py

Kiểm:
  [1] Syntax cli.py + web/server.py.
  [2] server.py: _hide_reg + set_hide_reg() + _hide_reg_enabled() (env) + replace __HIDE_REG__.
  [3] index.html: body data-hide-reg="__HIDE_REG__".
  [4] style.css: rule chỉ ẩn nút nav Reg khi hide-reg.
  [5] app.js: _isHideRegMode + initTabs đẩy 'reg' vào hidden + fallback session.
  [6] cli.py: option --hide-reg + gọi set_hide_reg().
  [7] setup.sh: parse --hide-reg + truyền $HIDE_REG_FLAG vào lệnh web.
  [8] Functional: index() render body data-hide-reg="1"/"0" theo set_hide_reg().
"""
from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _parse(p: Path) -> None:
    ast.parse(p.read_text(encoding="utf-8"), filename=str(p))


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, label: str) -> None:
        if cond:
            print(f"[PASS] {label}", flush=True)
        else:
            failures.append(label)
            print(f"[FAIL] {label}", flush=True)

    # [1] Syntax
    for f in [ROOT / "cli.py", ROOT / "web" / "server.py"]:
        try:
            _parse(f)
            print(f"[PASS] syntax {f.relative_to(ROOT)}", flush=True)
        except SyntaxError as e:
            failures.append(f"syntax {f}: {e}")
            print(f"[FAIL] syntax {f.relative_to(ROOT)} :: {e}", flush=True)

    server_src = (ROOT / "web" / "server.py").read_text(encoding="utf-8")
    html_src = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
    css_src = (ROOT / "web" / "static" / "style.css").read_text(encoding="utf-8")
    app_src = (ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
    cli_src = (ROOT / "cli.py").read_text(encoding="utf-8")
    setup_src = (ROOT / "setup.sh").read_text(encoding="utf-8")

    # [2] server.py wiring
    check("_hide_reg" in server_src, "server.py: có global _hide_reg")
    check("def set_hide_reg" in server_src, "server.py: có set_hide_reg()")
    check("def _hide_reg_enabled" in server_src,
          "server.py: có _hide_reg_enabled() (đọc env)")
    check("os.environ[_ENV_HIDE_REG]" in server_src,
          "server.py: set_hide_reg ghi os.environ (vượt module-identity)")
    check("__HIDE_REG__" in server_src and "_hide_reg_enabled()" in server_src,
          "server.py: index() replace __HIDE_REG__ theo _hide_reg_enabled()")
    # Không còn dấu vết only-upi cũ
    check("only_upi" not in server_src and "__ONLY_UPI__" not in server_src,
          "server.py: đã bỏ hết only_upi cũ")

    # [3] index.html
    check('data-hide-reg="__HIDE_REG__"' in html_src,
          'index.html: body có data-hide-reg="__HIDE_REG__"')

    # [4] style.css
    check('body[data-hide-reg="1"] .tab-btn[data-tab="reg"]' in css_src,
          "style.css: ẩn nút nav Reg khi hide-reg")

    # [5] app.js
    check("_isHideRegMode" in app_src, "app.js: có _isHideRegMode()")
    check("dataset.hideReg === '1'" in app_src, "app.js: đọc dataset.hideReg")
    check("if (_isHideRegMode()) hiddenTabs.push('reg')" in app_src,
          "app.js: thêm 'reg' vào hiddenTabs khi hide-reg")
    check("_isHideRegMode() ? 'session' : 'reg'" in app_src,
          "app.js: fallback sang 'session' khi initial là tab ẩn")

    # [6] cli.py
    check('"--hide-reg"' in cli_src, "cli.py: có option --hide-reg")
    check("set_hide_reg(hide_reg)" in cli_src, "cli.py: gọi set_hide_reg(hide_reg)")

    # [7] setup.sh
    check("--hide-reg) HIDE_REG=" in setup_src, "setup.sh: parse case --hide-reg")
    check('HIDE_REG_FLAG="--hide-reg"' in setup_src, "setup.sh: set HIDE_REG_FLAG")
    check('web --host "$HOST" --port "$PORT" $HIDE_REG_FLAG' in setup_src,
          "setup.sh: truyền $HIDE_REG_FLAG vào lệnh web")

    # [8] Functional render
    try:
        from web.server import index, set_hide_reg

        async def _render() -> str:
            resp = await index()
            return resp.body.decode("utf-8")

        set_hide_reg(True)
        html_on = asyncio.run(_render())
        check('data-hide-reg="1"' in html_on,
              "render: hide-reg ON → body data-hide-reg=\"1\"")
        check("__HIDE_REG__" not in html_on,
              "render: placeholder __HIDE_REG__ đã được thay")

        set_hide_reg(False)
        html_off = asyncio.run(_render())
        check('data-hide-reg="0"' in html_off,
              "render: hide-reg OFF → body data-hide-reg=\"0\"")
    except Exception as e:  # noqa: BLE001
        failures.append(f"functional render: {type(e).__name__}: {e}")
        print(f"[FAIL] functional render :: {e}", flush=True)

    print("", flush=True)
    if failures:
        print(f"=== {len(failures)} FAILURE(S) ===", flush=True)
        for x in failures:
            print(f"  - {x}", flush=True)
        return 1
    print("=== ALL PASS ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
