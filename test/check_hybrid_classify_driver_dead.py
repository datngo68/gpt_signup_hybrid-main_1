"""Verify _classify_error categorize driver-pipe-dead errors thành sentinel_observer.

Lỗi điển hình từ log production (Playwright driver pipe chết):
    Exception: Page.evaluate: Connection closed while reading from the driver

Trước đây fall-through → ``terminal`` → không retry. Sau fix:
``_SENTINEL_OBSERVER_MARKERS`` thêm pattern "connection closed while reading from
the driver" / "transport closed" / "target page... closed" → category
``sentinel_observer`` → hybrid retry với fresh BrowserContext (đã có policy).

Test plan:
  T01 syntax_ok — reg_hybrid/runner.py parse AST.
  T02 classify_driver_pipe_dead — log message thực tế → category="sentinel_observer".
  T03 classify_transport_closed — variant Playwright transport → sentinel_observer.
  T04 classify_target_closed — TargetClosedError → sentinel_observer.
  T05 classify_proxy_first — proxy markers vẫn ưu tiên trước observer (đúng order).
  T06 classify_terminal_unchanged — non-matching message vẫn rơi vào terminal.

Run: .venv/bin/python test/check_hybrid_classify_driver_dead.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RUNNER = ROOT / "reg_hybrid" / "runner.py"


def t01_syntax_ok() -> int:
    try:
        ast.parse(RUNNER.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        print(f"[FAIL] t01 syntax :: {exc}", flush=True)
        return 1
    print("[PASS] t01 reg_hybrid/runner.py parse AST OK", flush=True)
    return 0


def _classify(exc_or_msg) -> str:
    """Helper: import _classify_error fresh + call."""
    from reg_hybrid.runner import _classify_error
    return _classify_error(exc_or_msg)


def t02_classify_driver_pipe_dead() -> int:
    """Log thực tế từ production — driver pipe chết ở Page.evaluate."""
    msg = (
        "Exception: Page.evaluate: Connection closed while reading from the driver"
    )
    got = _classify(Exception(msg))
    if got != "sentinel_observer":
        print(f"[FAIL] t02 expect sentinel_observer, got {got!r}", flush=True)
        return 1
    print(f"[PASS] t02 driver-pipe-dead → sentinel_observer (retry allowed)", flush=True)
    return 0


def t03_classify_transport_closed() -> int:
    """Playwright transport pipe death (browser process die)."""
    msg = "Error: Browser disconnected: Transport closed"
    got = _classify(Exception(msg))
    if got != "sentinel_observer":
        print(f"[FAIL] t03 expect sentinel_observer, got {got!r}", flush=True)
        return 1
    print(f"[PASS] t03 transport closed → sentinel_observer", flush=True)
    return 0


def t04_classify_target_closed() -> int:
    """TargetClosedError variant — browser context tear down."""
    msg = (
        "playwright._impl._errors.TargetClosedError: Page.goto: "
        "Target page, context or browser has been closed"
    )
    got = _classify(Exception(msg))
    if got != "sentinel_observer":
        print(f"[FAIL] t04 expect sentinel_observer, got {got!r}", flush=True)
        return 1
    print(f"[PASS] t04 target-closed → sentinel_observer", flush=True)
    return 0


def t05_classify_proxy_first() -> int:
    """Proxy markers vẫn ưu tiên trước observer — đảm bảo order check không bị đảo."""
    msg = (
        "Error: Page.goto: NS_ERROR_PROXY_CONNECTION_REFUSED\n"
        "Call log:\n  - navigating to https://sentinel.openai.com/..."
    )
    got = _classify(Exception(msg))
    if got != "proxy_dead":
        print(f"[FAIL] t05 expect proxy_dead (priority), got {got!r}", flush=True)
        return 1
    print(f"[PASS] t05 proxy markers vẫn priority trước observer", flush=True)
    return 0


def t06_classify_terminal_unchanged() -> int:
    """Non-matching message vẫn rơi vào terminal (no false positive)."""
    msg = "ValidationError: birthdate must be ISO format"
    got = _classify(Exception(msg))
    if got != "terminal":
        print(f"[FAIL] t06 expect terminal, got {got!r}", flush=True)
        return 1
    print(f"[PASS] t06 non-matching → terminal (no false positive)", flush=True)
    return 0


def main() -> int:
    print("=== check_hybrid_classify_driver_dead ===", flush=True)
    rc = 0
    for fn in (
        t01_syntax_ok,
        t02_classify_driver_pipe_dead,
        t03_classify_transport_closed,
        t04_classify_target_closed,
        t05_classify_proxy_first,
        t06_classify_terminal_unchanged,
    ):
        try:
            rc |= fn()
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {fn.__name__} raised :: {type(exc).__name__}: {exc}", flush=True)
            rc |= 1
    print("=== DONE ===" if rc == 0 else "=== FAILED ===", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
