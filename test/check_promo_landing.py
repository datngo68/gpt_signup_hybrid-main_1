"""Check: 2 mode reg (browser, pure_request) dùng promo landing.

Verify (không thực sự gọi network/browser):
    [1] AST parse 5 file đã sửa → no SyntaxError.
    [2] config.PROMO_LANDING_URL == link promo yêu cầu.
    [3] browser_phase: 2 landing goto dùng PROMO_LANDING_URL (camoufox+chromium).
    [4] request_phase._prime_chatgpt_session GET promo landing TRƯỚC /auth/login.
    [5] reg_hybrid relay KHÔNG còn _visit_promo_landing — promo bị bỏ ở hybrid
        (camoufox launch qua proxy bị timeout vô lý vì GET / extra).

Chạy: .venv/bin/python test/check_promo_landing.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EXPECTED_URL = "https://chatgpt.com/?promo_campaign=plus-1-month-free#pricing"

FILES = {
    "config": ROOT / "config.py",
    "browser_phase": ROOT / "browser_phase.py",
    "request_phase": ROOT / "request_phase.py",
    "relay": ROOT / "reg_hybrid" / "relay.py",
    "ccx_headers": ROOT / "chatgpt_camoufox" / "chatgpt_camoufox" / "headers.py",
}

_failures: list[str] = []
_passes: list[str] = []


def _ok(msg: str) -> None:
    _passes.append(msg)
    print(f"[PASS] {msg}")


def _fail(msg: str) -> None:
    _failures.append(msg)
    print(f"[FAIL] {msg}")


def _read(name: str) -> str:
    return FILES[name].read_text(encoding="utf-8")


# ── [1] AST parse mọi file đã sửa ─────────────────────────────────────
def check_ast() -> None:
    for name, path in FILES.items():
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            _ok(f"AST parse OK: {name} ({path.name})")
        except SyntaxError as exc:
            _fail(f"SyntaxError in {name}: {exc}")


# ── [2] config.PROMO_LANDING_URL ──────────────────────────────────────
def check_constant() -> None:
    try:
        import config  # noqa: WPS433
    except Exception as exc:  # noqa: BLE001
        _fail(f"import config failed: {exc}")
        return
    url = getattr(config, "PROMO_LANDING_URL", None)
    if url == EXPECTED_URL:
        _ok(f"config.PROMO_LANDING_URL == {EXPECTED_URL}")
    else:
        _fail(f"config.PROMO_LANDING_URL = {url!r} (expected {EXPECTED_URL!r})")


# ── [3] browser mode: 2 landing goto ─────────────────────────────────
def check_browser() -> None:
    src = _read("browser_phase")
    n = src.count('await page.goto(PROMO_LANDING_URL, wait_until="domcontentloaded")')
    if n >= 2:
        _ok(f"browser_phase: {n} landing goto dùng PROMO_LANDING_URL")
    else:
        _fail(f"browser_phase: chỉ {n} landing goto dùng PROMO_LANDING_URL (cần >=2)")

    # Không còn landing goto hardcode chatgpt.com/ trên cùng 1 dòng.
    leftover = src.count('await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")')
    if leftover == 0:
        _ok("browser_phase: không còn landing goto hardcode chatgpt.com/")
    else:
        _fail(f"browser_phase: vẫn còn {leftover} landing goto hardcode chatgpt.com/")

    if "from config import PROMO_LANDING_URL" in src:
        _ok("browser_phase: import PROMO_LANDING_URL")
    else:
        _fail("browser_phase: thiếu import PROMO_LANDING_URL")


# ── [4] pure_request: promo TRƯỚC /auth/login ────────────────────────
def check_request_phase() -> None:
    src = _read("request_phase")
    if "PROMO_LANDING_URL" not in src:
        _fail("request_phase: không tham chiếu PROMO_LANDING_URL")
        return
    idx_promo = src.find("session.get(\n            PROMO_LANDING_URL")
    if idx_promo == -1:
        idx_promo = src.find("PROMO_LANDING_URL, headers=promo_headers")
    idx_login = src.find('"https://chatgpt.com/auth/login"')
    if idx_promo != -1 and idx_login != -1 and idx_promo < idx_login:
        _ok("request_phase: GET promo landing đứng TRƯỚC GET /auth/login")
    else:
        _fail(
            f"request_phase: thứ tự sai (promo={idx_promo}, login={idx_login})"
        )
    # Promo GET là top-level no-referer.
    if 'promo_headers["Sec-Fetch-Site"] = "none"' in src and 'promo_headers.pop("Referer"' in src:
        _ok("request_phase: promo GET = top-level no-referer (Sec-Fetch-Site=none)")
    else:
        _fail("request_phase: promo GET thiếu Sec-Fetch-Site=none / pop Referer")


# ── [5] hybrid: KHÔNG còn _visit_promo_landing (promo đã bỏ) ─────────
def check_hybrid() -> None:
    # 5a. Golden chatgpt_camoufox KHÔNG bị sửa (cấm theo spec deferred-ban).
    hsrc = _read("ccx_headers")
    if "home_navigate" not in hsrc:
        _ok("ccx headers.py: golden bất biến (không có home_navigate)")
    else:
        _fail("ccx headers.py: còn home_navigate → vi phạm cấm sửa golden")

    # 5b. relay.run() KHÔNG còn gọi _visit_promo_landing — promo bỏ ở hybrid.
    # Lý do: GET promo landing TRƯỚC csrf qua Camoufox launch + proxy yếu hay
    # bị timeout 90s (set_device_id/launch). Bỏ → giảm 1 GET, launch nhanh hơn.
    rsrc = _read("relay")
    if "def _visit_promo_landing" in rsrc:
        _fail("relay: vẫn còn method _visit_promo_landing (đã yêu cầu bỏ)")
    elif "self._visit_promo_landing" in rsrc:
        _fail("relay.run(): vẫn còn call self._visit_promo_landing (đã yêu cầu bỏ)")
    else:
        _ok("relay: đã bỏ _visit_promo_landing (method + call) — hybrid không còn promo step")
    # Golden chatgpt_camoufox vẫn không bị đụng (không gọi home_navigate trong relay).
    if "home_navigate" not in rsrc:
        _ok("relay: không gọi home_navigate (golden bất biến)")
    else:
        _fail("relay: còn gọi home_navigate → vi phạm cấm sửa golden")


def main() -> int:
    check_ast()
    check_constant()
    check_browser()
    check_request_phase()
    check_hybrid()
    print("\n" + "=" * 60)
    print(f"PASS={len(_passes)}  FAIL={len(_failures)}")
    if _failures:
        print("\nFAILURES:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
