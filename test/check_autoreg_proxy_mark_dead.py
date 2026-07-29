"""Verify AutoReg runner mark_dead proxy lỗi network + giữ helper signature đúng.

Kiểm:
- T01 syntax_ok
- T02 _note_proxy_failure được gọi đúng 2 chỗ (result.success=False, except Exception)
- T03 helper _note_proxy_failure tồn tại + dùng _is_proxy_network_error
      / _is_browser_closed_proxy_error + ProxyPool.mark_dead
- T04 email_proxy_line được init `None` trước try (safe access trong except)
- T05 dùng mock pool: lỗi network → mark_dead; lỗi nghiệp vụ → KHÔNG mark_dead
- T06 helper _is_browser_closed_proxy_error gating đúng: browser-closed AND
      OAI domain → True; thiếu 1 trong 2 → False (false positive guard)
- T07 runtime: TargetClosedError lúc navigate sentinel.openai.com → mark_dead
"""
from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "autoreg" / "runner.py"
SRC = RUNNER.read_text(encoding="utf-8")


def t01_syntax_ok() -> int:
    try:
        globals()["_TREE"] = ast.parse(SRC)
    except SyntaxError as exc:  # noqa: BLE001
        print(f"[FAIL] t01 syntax :: {exc}", flush=True)
        return 1
    print("[PASS] t01 runner.py parse AST OK", flush=True)
    return 0


def t02_note_proxy_failure_call_sites() -> int:
    """Phải gọi _note_proxy_failure ít nhất 2 lần trong _process_email."""
    tree = globals()["_TREE"]
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "_note_proxy_failure":
            count += 1
    if count < 2:
        print(f"[FAIL] t02 expect ≥2 call _note_proxy_failure :: got {count}", flush=True)
        return 1
    print(f"[PASS] t02 _note_proxy_failure gọi {count} lần", flush=True)
    return 0


def t03_helper_defined() -> int:
    tree = globals()["_TREE"]
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_note_proxy_failure":
            found = True
            body_src = ast.unparse(node)
            if "_is_proxy_network_error" not in body_src:
                print("[FAIL] t03 helper không gọi _is_proxy_network_error", flush=True)
                return 1
            if "_is_browser_closed_proxy_error" not in body_src:
                print("[FAIL] t03 helper không gọi _is_browser_closed_proxy_error", flush=True)
                return 1
            if "mark_dead" not in body_src:
                print("[FAIL] t03 helper không gọi mark_dead", flush=True)
                return 1
            break
    if not found:
        print("[FAIL] t03 không thấy def _note_proxy_failure", flush=True)
        return 1
    print("[PASS] t03 helper _note_proxy_failure dùng cả 2 detector + mark_dead", flush=True)
    return 0


def t04_email_proxy_line_init_none() -> int:
    """Trước try, phải có `email_proxy_line: ... = None` để except branch safe."""
    if "email_proxy_line: str | None = None" not in SRC:
        print("[FAIL] t04 không thấy init email_proxy_line = None trước try", flush=True)
        return 1
    print("[PASS] t04 email_proxy_line init None trước try", flush=True)
    return 0


def t05_runtime_mark_dead_behavior() -> int:
    """Mock pool + manager helper, gọi _note_proxy_failure thực tế."""
    sys.path.insert(0, str(ROOT))
    # Setup module mock cho web.manager + web.proxy_pool trước khi import runner
    import types

    fake_manager = types.ModuleType("web.manager")
    def _is_net(exc_or_msg) -> bool:
        s = str(exc_or_msg).lower()
        return any(k in s for k in ("proxy", "timeout", "connection refused", "tunnel"))
    fake_manager._is_proxy_network_error = _is_net
    fake_manager._mask_proxy = lambda s: "***"
    sys.modules["web.manager"] = fake_manager

    fake_pool_mod = types.ModuleType("web.proxy_pool")
    class _FakePool:
        def __init__(self) -> None:
            self.dead: list[str] = []
        def mark_dead(self, line: str) -> bool:
            if line in self.dead:
                return False
            self.dead.append(line)
            return True
    _pool = _FakePool()
    fake_pool_mod.get_proxy_pool = lambda: _pool
    sys.modules["web.proxy_pool"] = fake_pool_mod

    from autoreg.runner import AutoRegRunner

    # Tạo bare instance, bỏ qua __init__ (cần nhiều dep) — chỉ test method.
    runner = AutoRegRunner.__new__(AutoRegRunner)
    async def _no_log(level, msg, meta=None):  # noqa: ARG001
        return None
    runner._log = _no_log  # type: ignore[attr-defined]

    # Cần 1 event loop để asyncio.create_task không raise
    async def _exercise() -> None:
        # Case 1: lỗi network → mark_dead
        runner._note_proxy_failure("h:1:u:p", "proxy timeout", prefix="[t]")
        # Case 2: lỗi nghiệp vụ → KHÔNG mark_dead
        runner._note_proxy_failure("h:2:u:p", "stripe declined card", prefix="[t]")
        # Case 3: line=None → no-op
        runner._note_proxy_failure(None, "proxy timeout", prefix="[t]")
        # Case 4: gọi lại line đã dead → idempotent
        runner._note_proxy_failure("h:1:u:p", "proxy timeout", prefix="[t]")
        await asyncio.sleep(0)  # cho task log chạy 1 nhịp

    asyncio.run(_exercise())

    if _pool.dead != ["h:1:u:p"]:
        print(f"[FAIL] t05 dead set sai :: {_pool.dead}", flush=True)
        return 1
    print("[PASS] t05 mark_dead chỉ kích hoạt cho lỗi network + idempotent", flush=True)
    return 0


# Sample exception messages từ stack trace thực tế (giữ nguyên format Playwright).
_TARGET_CLOSED_SENTINEL = (
    'Page.goto: Target page, context or browser has been closed\n'
    'Call log:\n'
    '  - navigating to "https://sentinel.openai.com/backend-api/sentinel/frame.html", '
    'waiting until "domcontentloaded"'
)
_TARGET_CLOSED_CHATGPT = (
    'Page.goto: BrowserContext has been closed\n'
    '  - navigating to "https://chatgpt.com/auth/login"'
)
_TARGET_CLOSED_NO_DOMAIN = (
    # Browser bị reaper kill do timeout — không kèm domain OAI.
    'Page.goto: Target page, context or browser has been closed'
)


def t06_browser_closed_helper_gating() -> int:
    """Helper _is_browser_closed_proxy_error: gating đúng (closed AND OAI domain)."""
    sys.path.insert(0, str(ROOT))
    from autoreg.runner import _is_browser_closed_proxy_error as is_bc

    # Đủ điều kiện → True
    if not is_bc(_TARGET_CLOSED_SENTINEL):
        print("[FAIL] t06 sentinel.openai.com + TargetClosed → expect True", flush=True)
        return 1
    if not is_bc(_TARGET_CLOSED_CHATGPT):
        print("[FAIL] t06 chatgpt.com + BrowserContext closed → expect True", flush=True)
        return 1

    # Browser closed nhưng KHÔNG có domain OAI → False (reaper kill, anti-bot)
    if is_bc(_TARGET_CLOSED_NO_DOMAIN):
        print("[FAIL] t06 closed-without-OAI-domain phải False (chống false positive)", flush=True)
        return 1

    # Có domain OAI nhưng KHÔNG match browser-closed pattern → False
    if is_bc("HTTP 500 Internal Server Error from openai.com"):
        print("[FAIL] t06 OAI-domain-without-browser-closed phải False", flush=True)
        return 1

    # None / empty → False
    if is_bc(None) or is_bc(""):
        print("[FAIL] t06 None/empty phải False", flush=True)
        return 1

    print("[PASS] t06 _is_browser_closed_proxy_error gating đúng (AND condition)", flush=True)
    return 0


def t07_browser_closed_marks_dead() -> int:
    """Runtime: TargetClosedError lúc navigate sentinel.openai.com → mark_dead.

    Tái sử dụng cùng mock pool setup của t05; chỉ thêm assertion cho browser-closed
    pattern. Nếu t05 đã chạy trước → web.manager + web.proxy_pool đã được mock.
    """
    sys.path.insert(0, str(ROOT))

    from autoreg.runner import AutoRegRunner

    # Pool đã được mock ở t05 hoặc cần re-mock nếu t05 chưa chạy.
    pool_mod = sys.modules.get("web.proxy_pool")
    if pool_mod is None:
        # T05 chưa chạy → setup mock độc lập
        import types
        fake_manager = types.ModuleType("web.manager")
        fake_manager._is_proxy_network_error = lambda _: False  # noqa: E731
        fake_manager._mask_proxy = lambda s: "***"
        sys.modules["web.manager"] = fake_manager
        fake_pool_mod = types.ModuleType("web.proxy_pool")
        class _FakePool:
            def __init__(self):
                self.dead = []
            def mark_dead(self, line):
                if line in self.dead:
                    return False
                self.dead.append(line)
                return True
        fake_pool_mod.get_proxy_pool = lambda: _FakePool()
        sys.modules["web.proxy_pool"] = fake_pool_mod
        pool_mod = fake_pool_mod

    pool = pool_mod.get_proxy_pool()
    # Reset dead-list cho test này
    if hasattr(pool, "dead"):
        pool.dead.clear()

    runner = AutoRegRunner.__new__(AutoRegRunner)
    async def _no_log(level, msg, meta=None):  # noqa: ARG001
        return None
    runner._log = _no_log  # type: ignore[attr-defined]

    async def _exercise():
        # Case A: browser closed + sentinel.openai.com → mark dead
        runner._note_proxy_failure("bc:1:u:p", _TARGET_CLOSED_SENTINEL, prefix="[t7]")
        # Case B: browser closed + chatgpt.com → mark dead
        runner._note_proxy_failure("bc:2:u:p", _TARGET_CLOSED_CHATGPT, prefix="[t7]")
        # Case C: browser closed KHÔNG có OAI domain → KHÔNG mark
        runner._note_proxy_failure("bc:3:u:p", _TARGET_CLOSED_NO_DOMAIN, prefix="[t7]")
        await asyncio.sleep(0)

    asyncio.run(_exercise())

    if "bc:1:u:p" not in pool.dead:
        print(f"[FAIL] t07 sentinel browser-closed phải mark_dead :: dead={pool.dead}", flush=True)
        return 1
    if "bc:2:u:p" not in pool.dead:
        print(f"[FAIL] t07 chatgpt browser-closed phải mark_dead :: dead={pool.dead}", flush=True)
        return 1
    if "bc:3:u:p" in pool.dead:
        print(f"[FAIL] t07 closed-không-OAI-domain bị mark oan :: dead={pool.dead}", flush=True)
        return 1

    print(f"[PASS] t07 browser-closed + OAI domain → mark_dead; thiếu domain → skip", flush=True)
    return 0


def main() -> int:
    print("=== check_autoreg_proxy_mark_dead ===", flush=True)
    rc = 0
    for fn in (
        t01_syntax_ok,
        t02_note_proxy_failure_call_sites,
        t03_helper_defined,
        t04_email_proxy_line_init_none,
        t05_runtime_mark_dead_behavior,
        t06_browser_closed_helper_gating,
        t07_browser_closed_marks_dead,
    ):
        rc |= fn()
    print("=== DONE ===" if rc == 0 else "=== FAILED ===", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
