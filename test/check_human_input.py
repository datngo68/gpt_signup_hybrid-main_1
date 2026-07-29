"""Phase 2 verify — `_human_input.py` helpers + integration browser_phase.

Mục tiêu:
    - Module `_human_input.py` export 4 helpers (human_type, human_click,
      random_mouse_wander, dwell).
    - `dwell` sample uniform [min, max].
    - `_sample_delay_ms` Gaussian distribution với mean ≈ (min+max)/2,
      95% sample trong khoảng [min, max].
    - `human_type` async — dùng FakeLocator để verify call sequence
      (click → fill → type per char với delay).
    - `random_mouse_wander` không raise khi page chết.
    - browser_phase.py KHÔNG còn `_REGISTER_USER_JS`/`_PAGE_CREATE_ACCOUNT_JS`/
      `_register_with_password` (chỉ comment lịch sử).
    - browser_phase.py import `human_type`, `human_click`, `dwell`,
      `random_mouse_wander`.

Chạy: .venv/bin/python3 test/check_human_input.py
"""
from __future__ import annotations

import asyncio
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ─────────────────────────────────────────────────────────────────────
# Mocks
# ─────────────────────────────────────────────────────────────────────


class _FakeLocator:
    """Mock Playwright Locator — record calls cho assertion."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def click(self, *, force: bool = False, timeout: int = 0) -> None:
        self.calls.append(("click", {"force": force, "timeout": timeout}))

    async def fill(self, value: str, **_: object) -> None:
        self.calls.append(("fill", {"value": value}))

    async def type(self, ch: str, *, delay: int = 0) -> None:
        self.calls.append(("type", {"ch": ch, "delay": delay}))

    async def press(self, key: str) -> None:
        self.calls.append(("press", {"key": key}))

    async def bounding_box(self, *, timeout: int = 0):
        return {"x": 100, "y": 200, "width": 80, "height": 30}

    async def is_visible(self, *, timeout: int = 0) -> bool:
        return True


class _FakePage:
    def __init__(self, vw: int = 1280, vh: int = 720) -> None:
        self._vw = vw
        self._vh = vh
        self.mouse = _FakeMouse()
        self._eval_calls: list[str] = []

    async def evaluate(self, expr: str, *args):
        self._eval_calls.append(expr)
        if "innerWidth" in expr:
            return [self._vw, self._vh]
        return None

    def locator(self, sel: str):
        return _FakeLocator()


class _FakeMouse:
    def __init__(self) -> None:
        self.moves: list[tuple[float, float, int]] = []

    async def move(self, x: float, y: float, *, steps: int = 1) -> None:
        self.moves.append((x, y, steps))


# ─────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────


def test_module_exports() -> tuple[bool, str]:
    from _human_input import (
        human_type,
        human_click,
        random_mouse_wander,
        dwell,
        DEFAULT_DELAY_MIN_MS,
        DEFAULT_DELAY_MAX_MS,
    )
    if DEFAULT_DELAY_MIN_MS == 45 and DEFAULT_DELAY_MAX_MS == 110:
        return True, "exports + defaults OK"
    return False, f"defaults wrong: {DEFAULT_DELAY_MIN_MS}/{DEFAULT_DELAY_MAX_MS}"


def test_sample_delay_distribution() -> tuple[bool, str]:
    from _human_input import _sample_delay_ms
    samples = [_sample_delay_ms(120, 260) for _ in range(2000)]
    mean = statistics.mean(samples)
    stdev = statistics.stdev(samples)
    # Mean phải gần 190 (giữa 120-260), stdev > 10 (có biến thiên)
    if 175 <= mean <= 205 and stdev > 10 and all(120 <= s <= 260 for s in samples):
        return True, f"mean={mean:.1f} stdev={stdev:.1f} (clamp [120,260])"
    return False, f"distribution off: mean={mean:.1f} stdev={stdev:.1f}"


async def _async_test_human_type() -> tuple[bool, str]:
    from _human_input import human_type
    loc = _FakeLocator()
    await human_type(loc, "abc", delay_min_ms=50, delay_max_ms=80)
    # Expected sequence: click → fill('') → type 3 chars
    types = [c for c in loc.calls if c[0] == "type"]
    if len(types) == 3:
        chars = [c[1]["ch"] for c in types]
        delays = [c[1]["delay"] for c in types]
        if chars == ["a", "b", "c"] and all(50 <= d <= 80 for d in delays):
            has_click = any(c[0] == "click" for c in loc.calls)
            has_fill = any(c[0] == "fill" and c[1]["value"] == "" for c in loc.calls)
            if has_click and has_fill:
                return True, f"3 chars typed with delays {delays}"
    return False, f"calls: {loc.calls}"


async def _async_test_dwell() -> tuple[bool, str]:
    from _human_input import dwell
    samples_dur = []
    for _ in range(5):
        t0 = time.monotonic()
        await dwell(0.05, 0.10)
        samples_dur.append(time.monotonic() - t0)
    if all(0.04 <= d <= 0.15 for d in samples_dur):
        return True, f"dwell durations {[f'{d:.3f}' for d in samples_dur]} ∈ [0.04, 0.15]"
    return False, f"dwell out of range: {samples_dur}"


async def _async_test_mouse_wander() -> tuple[bool, str]:
    from _human_input import random_mouse_wander
    page = _FakePage(vw=1280, vh=720)
    await random_mouse_wander(page, count=3, settle_min_s=0.0, settle_max_s=0.0)
    if len(page.mouse.moves) == 3:
        for x, y, steps in page.mouse.moves:
            if not (1280 * 0.1 <= x <= 1280 * 0.9):
                return False, f"x out of range: {x}"
            if not (720 * 0.1 <= y <= 720 * 0.9):
                return False, f"y out of range: {y}"
            if not (8 <= steps <= 20):
                return False, f"steps out of range: {steps}"
        return True, f"3 mouse moves: {page.mouse.moves}"
    return False, f"expected 3 moves, got {len(page.mouse.moves)}"


def test_browser_phase_dead_code_removed() -> tuple[bool, str]:
    src = (ROOT / "browser_phase.py").read_text(encoding="utf-8")
    # KHÔNG còn assignment hoặc call thực
    bad_patterns = [
        '_REGISTER_USER_JS = r"""',
        '_PAGE_CREATE_ACCOUNT_JS = r"""',
        "page.evaluate(_REGISTER_USER_JS",
        "page.evaluate(\n        _REGISTER_USER_JS",
        "page.evaluate(_PAGE_CREATE_ACCOUNT_JS",
        "async def _register_with_password(",
    ]
    found = [p for p in bad_patterns if p in src]
    if not found:
        return True, "all dead code (JS const + helper) removed"
    return False, f"still found: {found}"


def test_browser_phase_human_input_imports() -> tuple[bool, str]:
    src = (ROOT / "browser_phase.py").read_text(encoding="utf-8")
    needed = ["human_type", "human_click", "random_mouse_wander", "dwell"]
    missing = [n for n in needed if n not in src]
    if not missing:
        return True, "browser_phase imports all 4 helpers"
    return False, f"missing: {missing}"


def test_browser_phase_no_legacy_delay() -> tuple[bool, str]:
    """`delay=50` và `delay=80` cố định cho password input không còn — chuyển
    sang human_type (settings-driven 120-260 Gaussian)."""
    src = (ROOT / "browser_phase.py").read_text(encoding="utf-8")
    bad = [
        ".type(request.password, delay=50)",
        '.type(name_input, name, delay=80)',
        '.type(name, delay=80)',  # alternate form
    ]
    found = [p for p in bad if p in src]
    if not found:
        return True, "no legacy fixed-delay typing"
    return False, f"legacy delay found: {found}"


def main() -> int:
    failures: list[str] = []

    sync_tests = [
        ("module exports + defaults", test_module_exports),
        ("_sample_delay_ms distribution", test_sample_delay_distribution),
        ("dead code removed (JS + helper)", test_browser_phase_dead_code_removed),
        ("human_input imports in browser_phase", test_browser_phase_human_input_imports),
        ("no legacy fixed-delay typing", test_browser_phase_no_legacy_delay),
    ]
    for name, fn in sync_tests:
        ok, msg = fn()
        if ok:
            print(f"  [PASS] {name}: {msg}")
        else:
            print(f"  [FAIL] {name}: {msg}")
            failures.append(name)

    async_tests = [
        ("human_type async sequence", _async_test_human_type),
        ("dwell uniform timing", _async_test_dwell),
        ("random_mouse_wander 3 moves", _async_test_mouse_wander),
    ]
    for name, coro_fn in async_tests:
        ok, msg = asyncio.run(coro_fn())
        if ok:
            print(f"  [PASS] {name}: {msg}")
        else:
            print(f"  [FAIL] {name}: {msg}")
            failures.append(name)

    print()
    if failures:
        print(f"[FAIL] {len(failures)} failure(s):")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("[OK] All Phase 2 human_input + browser_phase invariants pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
