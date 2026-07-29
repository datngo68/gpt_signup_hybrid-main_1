"""Verify fix bug "multi-signup launch-hang" — OFFLINE (không cần Camoufox thật).

Camoufox browser binary CHƯA fetch trên máy CI/dev này → không thể launch
browser thật. Test này verify CƠ CHẾ fix mà không cần browser:

  T1 BOUND TIMEOUT fail-fast:
      _NoPoolThreadAffinityWrapper với inner GIẢ block lâu → op (launch/mint)
      raise HybridBrowserPoolError trong ~timeout, KHÔNG treo vô hạn.

  T2 REAPER delta + descendant scoping (mock ps):
      kill_new CHỈ nhắm process MỚI (delta sau mark_baseline) — KHÔNG đụng
      process baseline (signup khác / Firefox user). Verify qua _signal recorder.

  T3 REAPER force-kill THẬT (controlled descendant):
      spawn child python (argv chứa 'camoufox') = descendant → reaper SIGTERM→
      SIGKILL nó thật; child trong baseline KHÔNG bị kill.

Chạy:  .venv/bin/python test/check_reaper_and_timeout.py
"""
from __future__ import annotations

import sys
import time
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DUMMY = str(Path(__file__).resolve().parent / "_reaper_dummy_sleeper.py")

_results: list[tuple[str, bool, str]] = []


def _check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n        {detail}" if detail else ""))
    return ok


# ──────────────────────────────────────────────────────────────────────
# T1 — bound timeout fail-fast
# ──────────────────────────────────────────────────────────────────────

def t1_bound_timeout() -> bool:
    import reg_hybrid.browser_pool as bp

    # Rút ngắn timeout để test nhanh (patch module global — _run_bounded đọc
    # tại call-time).
    orig_launch = bp._LAUNCH_TIMEOUT_SECONDS
    orig_mint = bp._MINT_TIMEOUT_SECONDS
    bp._LAUNCH_TIMEOUT_SECONDS = 1.0
    bp._MINT_TIMEOUT_SECONDS = 1.0

    class _HangInner:
        _log = lambda self, m: None  # noqa: E731

        def set_device_id(self, did):
            time.sleep(30)  # block — mô phỏng launch treo

        def mint_token(self, flow):
            time.sleep(30)

        def mint_so(self, flow):
            time.sleep(30)

        def export_cookies(self):
            return []

        def close(self):
            pass

    try:
        # Mỗi op dùng wrapper RIÊNG: _PlaywrightThread là 1 worker FIFO, op treo
        # trước sẽ chặn op sau trên CÙNG wrapper (production cũng vậy nhưng mỗi
        # signup là 1 wrapper mới). Test từng path độc lập cho sạch.

        # set_device_id (launch) phải fail-fast ~1s, KHÔNG treo 30s.
        print("        [t1] build w1 + set_device_id ...", flush=True)
        w1 = bp._NoPoolThreadAffinityWrapper(_HangInner())
        t0 = time.monotonic()
        raised = ""
        try:
            w1.set_device_id("dev-1")
        except bp.HybridBrowserPoolError as exc:
            raised = str(exc)
        el = time.monotonic() - t0
        print(f"        [t1] set_device_id done el={el:.2f}s raised={bool(raised)}", flush=True)
        ok_launch = bool(raised) and el < 5.0 and "timeout" in raised.lower()

        # mint_token cũng fail-fast (wrapper riêng).
        w2 = bp._NoPoolThreadAffinityWrapper(_HangInner())
        t1 = time.monotonic()
        raised2 = ""
        try:
            w2.mint_token("oauth_signin")
        except bp.HybridBrowserPoolError as exc:
            raised2 = str(exc)
        el2 = time.monotonic() - t1
        ok_mint = bool(raised2) and el2 < 5.0

        # close trên wrapper có inner nhanh: không treo, reaper sweep trống.
        class _FastInner(_HangInner):
            def close(self):
                pass
        w3 = bp._NoPoolThreadAffinityWrapper(_FastInner())
        t2 = time.monotonic()
        w3.close()
        el3 = time.monotonic() - t2
        ok_close = el3 < 10.0

        return _check(
            "T1 bound timeout fail-fast (launch/mint/close không treo vô hạn)",
            ok_launch and ok_mint and ok_close,
            f"launch={el:.2f}s raised={raised!r}; mint={el2:.2f}s raised={raised2!r}; "
            f"close={el3:.2f}s",
        )
    finally:
        bp._LAUNCH_TIMEOUT_SECONDS = orig_launch
        bp._MINT_TIMEOUT_SECONDS = orig_mint


# ──────────────────────────────────────────────────────────────────────
# T2 — reaper delta + descendant scoping (mock snapshot + signal recorder)
# ──────────────────────────────────────────────────────────────────────

def t2_reaper_scoping() -> bool:
    import reg_hybrid._proc_reaper as pr

    # Mô phỏng: trước launch có sẵn pid 100 (firefox user / signup khác).
    # Sau launch xuất hiện pid 200 (browser của wrapper này). kill_new CHỈ
    # được nhắm 200.
    state = {"phase": "baseline"}

    def _fake_descendants(root_pid):
        if state["phase"] == "baseline":
            return {100: "firefox-existing"}
        return {100: "firefox-existing", 200: "camoufox-new", 201: "node playwright driver.js"}

    signalled: list[tuple[int, int]] = []

    def _fake_signal(pid, sig, log):
        signalled.append((pid, sig))
        # mô phỏng process chết sau SIGTERM → biến mất khỏi snapshot sau đó
        # (để kill_new không cần SIGKILL). Ở đây giữ sống để test luôn SIGKILL.

    orig_desc = pr._descendant_browser_pids
    orig_sig = pr._signal
    pr._descendant_browser_pids = _fake_descendants
    pr._signal = _fake_signal
    try:
        import signal as _signal_mod
        reaper = pr.BrowserProcessReaper(log=lambda m: None)
        reaper.mark_baseline()  # phase=baseline → {100}
        state["phase"] = "after"
        killed = reaper.kill_new("test", grace_seconds=0.4)

        term_pids = {pid for pid, sig in signalled if sig == _signal_mod.SIGTERM}
        kill_pids = {pid for pid, sig in signalled if sig == _signal_mod.SIGKILL}

        # 100 (baseline) KHÔNG bao giờ bị signal. 200+201 (delta) bị TERM rồi KILL.
        ok = (
            100 not in term_pids and 100 not in kill_pids
            and term_pids == {200, 201}
            and kill_pids == {200, 201}
            and set(killed) == {200, 201}
        )
        return _check(
            "T2 reaper chỉ kill DELTA (200,201), giữ baseline (100)",
            ok,
            f"SIGTERM={sorted(term_pids)} SIGKILL={sorted(kill_pids)} killed={sorted(killed)}",
        )
    finally:
        pr._descendant_browser_pids = orig_desc
        pr._signal = orig_sig


# ──────────────────────────────────────────────────────────────────────
# T3 — reaper force-kill THẬT một descendant controlled
# ──────────────────────────────────────────────────────────────────────

def t3_reaper_real_kill() -> bool:
    import reg_hybrid._proc_reaper as pr

    # Child 1 = baseline (spawn TRƯỚC mark_baseline) → KHÔNG được kill.
    base_child = subprocess.Popen([sys.executable, DUMMY, "camoufox-baseline"])
    time.sleep(1.0)  # cho ps thấy nó

    reaper = pr.BrowserProcessReaper(log=lambda m: print(f"        {m}"))
    reaper.mark_baseline()

    # Child 2 = mới (spawn SAU mark_baseline) → delta → phải bị reaper kill.
    new_child = subprocess.Popen([sys.executable, DUMMY, "camoufox-new"])
    time.sleep(1.0)

    try:
        killed = reaper.kill_new("t3-real", grace_seconds=3.0)
        # new_child wedged (nuốt SIGTERM) → reaper phải SIGKILL. wait() để reap
        # zombie + xác nhận đã chết (returncode != None, kỳ vọng -SIGKILL).
        rc = None
        try:
            rc = new_child.wait(timeout=5)
        except Exception:
            rc = None
        new_dead = rc is not None
        base_alive = base_child.poll() is None
        ok = new_dead and base_alive and new_child.pid in killed
        return _check(
            "T3 reaper SIGKILL THẬT child wedged mới, giữ child baseline",
            ok,
            f"new_child(pid={new_child.pid}) rc={rc} dead={new_dead} "
            f"in_killed={new_child.pid in killed}; "
            f"base_child(pid={base_child.pid}) alive={base_alive}; killed={killed}",
        )
    finally:
        for p in (base_child, new_child):
            try:
                p.kill()
            except Exception:
                pass
            try:
                p.wait(timeout=5)
            except Exception:
                pass


def main() -> int:
    print("=" * 70)
    print("VERIFY fix multi-signup launch-hang (offline — không cần Camoufox)")
    print("=" * 70)
    t1_bound_timeout()
    t2_reaper_scoping()
    t3_reaper_real_kill()
    print("=" * 70)
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print(f"RESULT: {passed}/{total} PASS")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
