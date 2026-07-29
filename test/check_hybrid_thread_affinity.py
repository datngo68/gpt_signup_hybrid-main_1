"""Verify fix bug Playwright Sync API in asyncio loop + thread-affinity.

Test cases (không launch Camoufox thật — mock Playwright sync API):
    [1/5] _PlaywrightThread.run() chạy fn trong thread khác main.
    [2/5] _PlaywrightThread re-entrancy guard: nested run() gọi inline, không deadlock.
    [3/5] _PlaywrightThread thread-affinity: 2 call từ 2 thread khác nhau đều
          chạy trên CÙNG MỘT thread executor.
    [4/5] _NoPoolThreadAffinityWrapper proxy method route qua _PlaywrightThread.
    [5/5] _CamoufoxRunner thuộc tính `thread` expose _PlaywrightThread instance,
          các method internal `_*_in_thread` tồn tại với signature đúng.

Chạy:
    .venv/bin/python test/check_hybrid_thread_affinity.py
"""
from __future__ import annotations

import inspect
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _check(name: str, fn) -> bool:
    try:
        fn()
        print(f"[PASS] {name}", flush=True)
        return True
    except AssertionError as exc:
        print(f"[FAIL] {name} — {exc}", flush=True)
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {name} — {type(exc).__name__}: {exc}", flush=True)
        return False


def tc1_run_in_other_thread() -> bool:
    from reg_hybrid.browser_pool import _PlaywrightThread

    def _inline_thread_id_check() -> None:
        t = _PlaywrightThread(name="tc1")
        caller_thread = threading.get_ident()
        try:
            inside = t.run(threading.get_ident)
            assert inside != caller_thread, (
                f"run() phải chạy fn ở thread khác (caller={caller_thread}, "
                f"inside={inside})"
            )
        finally:
            t.shutdown()

    return _check("[1/5] _PlaywrightThread.run() chạy fn ở thread khác main", _inline_thread_id_check)


def tc2_reentrancy_guard() -> bool:
    from reg_hybrid.browser_pool import _PlaywrightThread

    def _reentrancy() -> None:
        t = _PlaywrightThread(name="tc2")
        try:
            # Inside fn outer (chạy trong executor thread), gọi t.run(inner) lần
            # nữa. Nếu KHÔNG có re-entrancy guard → deadlock (executor max=1,
            # outer chiếm slot, inner chờ slot → timeout).
            results: dict[str, int] = {}

            def inner() -> int:
                results["inner_tid"] = threading.get_ident()
                return 42

            def outer() -> int:
                results["outer_tid"] = threading.get_ident()
                # Gọi nested — phải chạy inline không deadlock.
                v = t.run(inner)
                results["inner_returned"] = v
                return v + 1

            # Deadlock test với timeout cứng (thread.run() không có timeout
            # API → dùng threading.Thread để time-out canh ngoài).
            box: dict[str, int] = {}

            def _worker() -> None:
                box["v"] = t.run(outer)

            th = threading.Thread(target=_worker, daemon=True)
            th.start()
            th.join(timeout=3.0)
            assert not th.is_alive(), (
                "Nested run() DEADLOCK — re-entrancy guard chưa hoạt động"
            )
            assert box.get("v") == 43, f"outer return sai: {box}"
            assert results["outer_tid"] == results["inner_tid"], (
                "outer và inner phải chạy cùng thread (executor max=1) — "
                f"got {results}"
            )
            assert results["inner_returned"] == 42, (
                f"inner return sai: {results['inner_returned']}"
            )
        finally:
            t.shutdown()

    return _check("[2/5] _PlaywrightThread re-entrancy guard không deadlock", _reentrancy)


def tc3_thread_affinity() -> bool:
    from reg_hybrid.browser_pool import _PlaywrightThread

    def _affinity() -> None:
        t = _PlaywrightThread(name="tc3")
        try:
            ids: list[int] = []

            def _capture() -> None:
                ids.append(threading.get_ident())
                time.sleep(0.02)

            # 2 caller thread khác nhau submit op vào executor — cả 2 phải
            # land trên cùng 1 executor thread (max_workers=1).
            threads = [
                threading.Thread(target=lambda: t.run(_capture)),
                threading.Thread(target=lambda: t.run(_capture)),
            ]
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=3.0)
            assert len(ids) == 2, f"Expected 2 ids, got {ids}"
            assert ids[0] == ids[1], (
                f"Thread-affinity broken: 2 op chạy ở thread khác nhau "
                f"({ids})"
            )
        finally:
            t.shutdown()

    return _check("[3/5] _PlaywrightThread thread-affinity (max_workers=1)", _affinity)


def tc4_nopool_wrapper_proxy() -> bool:
    from reg_hybrid.browser_pool import _NoPoolThreadAffinityWrapper

    def _proxy() -> None:
        # Fake inner — mock CamoufoxTokenGenerator interface.
        class _FakeInner:
            def __init__(self) -> None:
                self.calls: list[tuple] = []

            def set_device_id(self, did: str) -> None:
                self.calls.append(("set_device_id", did, threading.get_ident()))

            def export_cookies(self) -> list[dict]:
                self.calls.append(("export_cookies", threading.get_ident()))
                return [{"name": "cf_clearance", "value": "x"}]

            def mint_token(self, flow: str) -> str:
                self.calls.append(("mint_token", flow, threading.get_ident()))
                return f"token-{flow}"

            def mint_so(self, flow: str) -> str:
                self.calls.append(("mint_so", flow, threading.get_ident()))
                return f"so-{flow}"

            def close(self) -> None:
                self.calls.append(("close", threading.get_ident()))

        inner = _FakeInner()
        wrapper = _NoPoolThreadAffinityWrapper(inner)
        caller_tid = threading.get_ident()

        wrapper.set_device_id("dev-1")
        cookies = wrapper.export_cookies()
        token = wrapper.mint_token("oauth_create_account")
        so = wrapper.mint_so("oauth_create_account")
        wrapper.close()

        assert cookies == [{"name": "cf_clearance", "value": "x"}], f"cookies={cookies}"
        assert token == "token-oauth_create_account", f"token={token}"
        assert so == "so-oauth_create_account", f"so={so}"

        # Mọi call phải chạy ở thread khác caller + cùng 1 thread (executor max=1).
        method_tids = {c[-1] for c in inner.calls}
        assert caller_tid not in method_tids, (
            f"Wrapper phải route qua thread khác — caller={caller_tid} "
            f"in method_tids={method_tids}"
        )
        assert len(method_tids) == 1, (
            f"Wrapper phải route MỌI call qua CÙNG 1 thread (thread-affinity) — "
            f"got {method_tids}"
        )
        assert [c[0] for c in inner.calls] == [
            "set_device_id", "export_cookies", "mint_token", "mint_so", "close",
        ], f"call order sai: {inner.calls}"

    return _check(
        "[4/5] _NoPoolThreadAffinityWrapper proxy 5 method qua dedicated thread",
        _proxy,
    )


def tc5_runner_internal_methods_present() -> bool:
    from reg_hybrid.browser_pool import _CamoufoxRunner, _PlaywrightThread

    def _structure() -> None:
        # Không launch Camoufox — chỉ verify shape class + method signature.
        runner = _CamoufoxRunner(
            proxy=None, headless=True, insecure=False, log=lambda _m: None,
        )
        try:
            assert isinstance(runner.thread, _PlaywrightThread), (
                f"runner.thread phải là _PlaywrightThread instance, "
                f"got {type(runner.thread).__name__}"
            )
            # Method internal phải có (dùng cho thread.run()).
            required = (
                "_ensure_browser_in_thread",
                "_acquire_context_in_thread",
                "_release_context_in_thread",
                "_shutdown_in_thread",
            )
            missing = [m for m in required if not hasattr(runner, m)]
            assert not missing, f"_CamoufoxRunner thiếu internal method: {missing}"
            # Public API vẫn còn (không signature break).
            for m in ("acquire_context", "release_context", "shutdown"):
                method = getattr(runner, m)
                assert callable(method), f"{m} không callable"
                # KHÔNG raise khi inspect signature → method hợp lệ.
                inspect.signature(method)
        finally:
            runner.shutdown()

    return _check(
        "[5/5] _CamoufoxRunner expose thread + internal methods đúng shape",
        _structure,
    )


def main() -> int:
    results = [
        tc1_run_in_other_thread(),
        tc2_reentrancy_guard(),
        tc3_thread_affinity(),
        tc4_nopool_wrapper_proxy(),
        tc5_runner_internal_methods_present(),
    ]
    passed = sum(results)
    total = len(results)
    print(flush=True)
    if passed == total:
        print(f"=== THREAD AFFINITY CHECK PASSED ({passed}/{total}) ===", flush=True)
        return 0
    print(f"=== THREAD AFFINITY CHECK FAILED ({passed}/{total}) ===", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
