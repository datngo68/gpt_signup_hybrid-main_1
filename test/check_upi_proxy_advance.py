"""Verify (1) syntax upi_runner.py vẫn parse OK sau khi sửa approve loop,
(2) logic proxy advance đúng cho các case batch/pool size khác nhau.

Run: python3 test/check_upi_proxy_advance.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "web" / "upi_runner.py"


def t01_syntax() -> int:
    """Parse AST upi_runner.py — phải compile OK."""
    src = TARGET.read_text(encoding="utf-8")
    try:
        ast.parse(src)
    except SyntaxError as exc:
        print(f"[FAIL] TC-01 syntax :: {exc}", flush=True)
        return 1
    print("[PASS] TC-01 syntax :: upi_runner.py parse OK", flush=True)
    return 0


def t02_threshold_value() -> int:
    """Threshold = 0 (DISABLED — backend_exception không bao giờ fatal-break)."""
    src = TARGET.read_text(encoding="utf-8")
    needle = "APPROVE_BACKEND_EXCEPTION_CONSECUTIVE: int = 0"
    if needle not in src:
        print(f"[FAIL] TC-02 threshold :: thiếu {needle!r}", flush=True)
        return 1
    print("[PASS] TC-02 threshold :: = 0 (disabled)", flush=True)
    return 0


def t03_proxy_advance_block() -> int:
    """Block advance proxy phải có trong source."""
    src = TARGET.read_text(encoding="utf-8")
    needles = [
        "proxy_virtual_attempt = 0",
        "_proxy_advance_enabled_static",
        "if _proxy_advance_enabled_static:",
        "proxy_virtual_attempt = (current_batch + 1) * APPROVE_PROXY_BATCH",
    ]
    for n in needles:
        if n not in src:
            print(f"[FAIL] TC-03 advance block :: thiếu {n!r}", flush=True)
            return 1
    print("[PASS] TC-03 advance block :: 4/4 markers found", flush=True)
    return 0


def _simulate_loop(
    approve_retries: int,
    proxy_pool: list[str],
    batch: int,
    proxy_from_step: int,
    *,
    exception_pattern: list[bool],
) -> list[str | None]:
    """Mô phỏng đúng logic approve loop để xác định proxy nào được dùng cho
    mỗi attempt. Trả về list proxy mask (None nếu không dùng proxy).

    `exception_pattern[i]` = True nghĩa là attempt thứ (i+1) bị backend_exception.
    Loop dừng nếu hết attempts hoặc hết exception_pattern.
    """
    used_proxies: list[str | None] = []
    proxy_advance_enabled = (
        proxy_from_step <= 6 and batch > 1 and len(proxy_pool) > 1
    )
    proxy_virtual_attempt = 0

    for i in range(approve_retries):
        proxy_virtual_attempt += 1
        # Replicate _proxy_url_for_retry behavior
        if 6 < proxy_from_step or not proxy_pool:
            approve_proxy: str | None = None
        else:
            idx = ((proxy_virtual_attempt - 1) // batch) % len(proxy_pool)
            approve_proxy = proxy_pool[idx]
        used_proxies.append(approve_proxy)

        if i >= len(exception_pattern):
            break
        if exception_pattern[i]:
            if proxy_advance_enabled:
                current_batch = (proxy_virtual_attempt - 1) // batch
                position_in_batch = proxy_virtual_attempt - current_batch * batch
                if position_in_batch < batch:
                    proxy_virtual_attempt = (current_batch + 1) * batch

    return used_proxies


def t04_advance_skips_remaining_batch() -> int:
    """Pool=[A,B,C], batch=3: attempt 1 = A, exception → attempt 2,3 = B, B, B
    (advance đã chuyển sang B, B dùng đủ batch). Sau 3 attempts B → đến C.
    """
    pool = ["A", "B", "C"]
    pattern = [True] + [False] * 9  # exception ngay attempt 1
    used = _simulate_loop(10, pool, batch=3, proxy_from_step=3,
                          exception_pattern=pattern)
    expected = ["A", "B", "B", "B", "C", "C", "C", "A", "A", "A"]
    if used != expected:
        print(f"[FAIL] TC-04 advance after 1st :: expected {expected} got {used}",
              flush=True)
        return 1
    print(f"[PASS] TC-04 advance after 1st :: {used[:7]}", flush=True)
    return 0


def t05_no_advance_at_batch_end() -> int:
    """Position=batch (cuối batch) → KHÔNG advance vì lần sau += 1 đã sang batch kế.
    Pool=[A,B], batch=3: attempt 3 = A (cuối batch A), exception → attempt 4 = B.
    """
    pool = ["A", "B"]
    # exception đúng attempt 3 (cuối batch A)
    pattern = [False, False, True, False, False, False]
    used = _simulate_loop(6, pool, batch=3, proxy_from_step=3,
                          exception_pattern=pattern)
    expected = ["A", "A", "A", "B", "B", "B"]
    if used != expected:
        print(f"[FAIL] TC-05 no advance at end :: expected {expected} got {used}",
              flush=True)
        return 1
    print(f"[PASS] TC-05 no advance at end :: {used}", flush=True)
    return 0


def t06_no_advance_when_pool_size_1() -> int:
    """Pool có 1 proxy → advance không có ý nghĩa, không bị crash."""
    pool = ["X"]
    pattern = [True, True, True, True]
    used = _simulate_loop(4, pool, batch=3, proxy_from_step=3,
                          exception_pattern=pattern)
    expected = ["X", "X", "X", "X"]
    if used != expected:
        print(f"[FAIL] TC-06 single proxy :: expected {expected} got {used}",
              flush=True)
        return 1
    print(f"[PASS] TC-06 single proxy :: {used}", flush=True)
    return 0


def t07_no_advance_when_batch_1() -> int:
    """Batch=1 → mỗi attempt đã đổi proxy, advance bị skip."""
    pool = ["A", "B", "C"]
    pattern = [True, True, True]
    used = _simulate_loop(3, pool, batch=1, proxy_from_step=3,
                          exception_pattern=pattern)
    expected = ["A", "B", "C"]
    if used != expected:
        print(f"[FAIL] TC-07 batch=1 :: expected {expected} got {used}",
              flush=True)
        return 1
    print(f"[PASS] TC-07 batch=1 :: {used}", flush=True)
    return 0


def t08_consecutive_exceptions_skip_multiple_proxies() -> int:
    """5 exceptions liên tiếp + pool=[A,B,C,D] batch=3: mỗi exception advance
    sang proxy kế ngay → A, B, C, D, A.
    """
    pool = ["A", "B", "C", "D"]
    pattern = [True] * 5
    used = _simulate_loop(5, pool, batch=3, proxy_from_step=3,
                          exception_pattern=pattern)
    expected = ["A", "B", "C", "D", "A"]
    if used != expected:
        print(f"[FAIL] TC-08 consec advance :: expected {expected} got {used}",
              flush=True)
        return 1
    print(f"[PASS] TC-08 consec advance :: {used}", flush=True)
    return 0


def t09_no_proxy_when_step_disabled() -> int:
    """PROXY_FROM_STEP > 6 → không dùng proxy (None mọi attempt), advance noop."""
    pool = ["A", "B", "C"]
    pattern = [True, True, True]
    used = _simulate_loop(3, pool, batch=3, proxy_from_step=7,
                          exception_pattern=pattern)
    expected = [None, None, None]
    if used != expected:
        print(f"[FAIL] TC-09 step disabled :: expected {expected} got {used}",
              flush=True)
        return 1
    print(f"[PASS] TC-09 step disabled :: {used}", flush=True)
    return 0


def t10_test_files_threshold_synced() -> int:
    """check_upi_module_imports.py + check_upi_runner_consecutive_fix.py
    phải đã update sang 0 (disabled)."""
    files_to_check = [
        ROOT / "test" / "check_upi_module_imports.py",
        ROOT / "test" / "check_upi_runner_consecutive_fix.py",
    ]
    legacy_needles = (
        "APPROVE_BACKEND_EXCEPTION_CONSECUTIVE == 15",
        "APPROVE_BACKEND_EXCEPTION_CONSECUTIVE: int = 15",
        "APPROVE_BACKEND_EXCEPTION_CONSECUTIVE == 5\n",
        "APPROVE_BACKEND_EXCEPTION_CONSECUTIVE: int = 5\"",
    )
    for fp in files_to_check:
        src = fp.read_text(encoding="utf-8")
        for needle in legacy_needles:
            if needle in src:
                print(f"[FAIL] TC-10 sync :: {fp.name} vẫn còn legacy {needle!r}",
                      flush=True)
                return 1
    print("[PASS] TC-10 sync :: 2 file test đã update sang 0 (disabled)", flush=True)
    return 0


def t11_batch_cache_materialize_coexist() -> int:
    """Approve loop nay lazy-materialize raw_pool theo batch-index (F-O) — markers
    cache phải coexist với advance logic (cùng dùng proxy_virtual_attempt/batch)."""
    src = TARGET.read_text(encoding="utf-8")
    needles = [
        "_approve_mat_cache",
        "batch_idx = (proxy_virtual_attempt - 1) // APPROVE_PROXY_BATCH",
        "if batch_idx not in _approve_mat_cache:",
        "_safe_materialize(raw_approve_proxy)",
        "len(proxy_pool) > 1",  # advance gate giữ nguyên (raw_pool len)
    ]
    for n in needles:
        if n not in src:
            print(f"[FAIL] TC-11 batch-cache :: thiếu {n!r}", flush=True)
            return 1
    print("[PASS] TC-11 batch-cache materialize coexist với advance (F-O)", flush=True)
    return 0


def main() -> int:
    print("=== check_upi_proxy_advance ===", flush=True)
    tests = [
        t01_syntax,
        t02_threshold_value,
        t03_proxy_advance_block,
        t04_advance_skips_remaining_batch,
        t05_no_advance_at_batch_end,
        t06_no_advance_when_pool_size_1,
        t07_no_advance_when_batch_1,
        t08_consecutive_exceptions_skip_multiple_proxies,
        t09_no_proxy_when_step_disabled,
        t10_test_files_threshold_synced,
        t11_batch_cache_materialize_coexist,
    ]
    failures = 0
    for fn in tests:
        try:
            rc = fn()
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {fn.__name__} :: raised {type(exc).__name__}: {exc}",
                  flush=True)
            rc = 1
        if rc != 0:
            failures += 1
    print(f"=== done :: {len(tests) - failures}/{len(tests)} pass ===",
          flush=True)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
