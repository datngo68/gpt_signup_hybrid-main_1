"""Repro offline: launch Camoufox TUẦN TỰ N lần, mint + close, đo timing.

MỤC TIÊU (điều tra bug reliability "2 success rồi kẹt từ #3"):
    - KHÔNG reg account thật, KHÔNG network signup. Chỉ exercise lifecycle
      Camoufox cold-launch + close giống production no-pool path
      (``_NoPoolThreadAffinityWrapper(CamoufoxTokenGenerator)``).
    - Đo per-iteration: launch(set_device_id → _ensure_page) / mint_token /
      close. Phát hiện iteration thứ 3+ có treo / chậm bất thường không.
    - Đếm tiến trình firefox/camoufox trước-sau mỗi close → phát hiện orphan
      process tích lũy (close không thực sự kill Firefox).

WATCHDOG: mỗi bước bọc trong ThreadPoolExecutor + timeout cứng (không để test
treo 420s như live). Bước timeout → ghi nhận TREO + tiếp tục để thu pattern.

Chạy:  .venv/bin/python test/check_sequential_camoufox_launch.py

EXPECTED (nếu bug như nghi vấn): iteration 1-2 launch nhanh, iteration 3+
launch/close treo hoặc time spike + firefox process count không trở về baseline.
Nếu mọi iteration đều nhanh + process về baseline → hypothesis "lifecycle
Camoufox tuần tự" SAI, bug nằm chỗ khác (executor/asyncio orchestration).
"""
from __future__ import annotations

import concurrent.futures
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ITERATIONS = 4
# Cold launch Camoufox bình thường ~2-10s. Cho watchdog rộng nhưng << 420s để
# phát hiện treo mà không phải chờ live timeout.
STEP_TIMEOUT_SECONDS = 90.0
# Flow mint — dùng flow signin (golden) để khớp op đầu tiên relay gọi.
MINT_FLOW = "oauth_signin"


def _count_firefox_procs() -> int:
    """Đếm tiến trình firefox/camoufox đang sống (macOS/Linux ``ps``)."""
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "command"],
            capture_output=True, text=True, timeout=10,
        ).stdout.lower()
    except Exception:
        return -1
    n = 0
    for line in out.splitlines():
        if "firefox" in line or "camoufox" in line:
            # Bỏ chính dòng python test / grep.
            if "check_sequential_camoufox_launch" in line:
                continue
            n += 1
    return n


def _run_step(label: str, fn, *args):
    """Chạy fn với watchdog timeout. Trả (elapsed, ok, err_or_result)."""
    t0 = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(fn, *args)
        try:
            res = fut.result(timeout=STEP_TIMEOUT_SECONDS)
            return (time.monotonic() - t0, True, res)
        except concurrent.futures.TimeoutError:
            return (time.monotonic() - t0, False, f"TIMEOUT>{STEP_TIMEOUT_SECONDS:.0f}s")
        except Exception as exc:  # noqa: BLE001
            return (time.monotonic() - t0, False, f"{type(exc).__name__}: {exc}")


def _build_wrapper():
    """Build no-pool wrapper y hệt production (camoufox_factory no-pool path)."""
    from models import SignupRequest
    from random_profile import random_profile_for_locale
    from reg_hybrid.browser_pool import _NoPoolThreadAffinityWrapper
    from chatgpt_camoufox.chatgpt_camoufox.camoufox_vm import CamoufoxTokenGenerator
    from chatgpt_camoufox.chatgpt_camoufox.fingerprint import profile_for_locale
    import random as _random

    profile = profile_for_locale(
        locale="en-US", firefox_major=135, platform="Windows",
        rng=_random.Random(),
    )
    inner = CamoufoxTokenGenerator(
        profile=profile, proxy=None, headless=True, insecure=False,
    )
    return _NoPoolThreadAffinityWrapper(inner)


def main() -> int:
    # Pre-check camoufox khả dụng.
    try:
        from chatgpt_camoufox.chatgpt_camoufox.camoufox_vm import camoufox_available
        if not camoufox_available():
            print("[SKIP] camoufox không cài được — chuyển sang phân tích tĩnh.")
            return 2
    except Exception as exc:  # noqa: BLE001
        print(f"[SKIP] import camoufox_vm fail: {exc}")
        return 2

    baseline = _count_firefox_procs()
    print(f"[baseline] firefox/camoufox procs = {baseline}")
    print(f"[config] iterations={ITERATIONS} step_timeout={STEP_TIMEOUT_SECONDS:.0f}s "
          f"flow={MINT_FLOW} cpu_count={os.cpu_count()}")
    print("=" * 78)

    rows: list[dict] = []
    device_id = "00000000-0000-4000-8000-000000000001"

    for i in range(1, ITERATIONS + 1):
        print(f"\n── iteration {i}/{ITERATIONS} ─────────────────────────────")
        row: dict = {"iter": i}

        t_build = time.monotonic()
        wrapper = _build_wrapper()
        row["build_s"] = round(time.monotonic() - t_build, 3)

        # LAUNCH: set_device_id trigger _ensure_page → Camoufox launch + goto
        # frame.html + bridge ready. Đây là khâu cold-launch nghi treo.
        el, ok, info = _run_step("launch", wrapper.set_device_id, device_id)
        row["launch_s"] = round(el, 3)
        row["launch_ok"] = ok
        if not ok:
            row["launch_info"] = info
            print(f"  launch: {el:.2f}s OK={ok} ({info})")
        else:
            print(f"  launch: {el:.2f}s OK={ok}")

        # MINT best-effort (cần network sentinel; fail vẫn ghi timing).
        procs_after_launch = _count_firefox_procs()
        row["procs_after_launch"] = procs_after_launch

        el, ok, info = _run_step("mint", wrapper.mint_token, MINT_FLOW)
        row["mint_s"] = round(el, 3)
        row["mint_ok"] = ok
        row["mint_info"] = "" if ok else str(info)
        print(f"  mint:   {el:.2f}s OK={ok}"
              + ("" if ok else f" ({str(info)[:80]})"))

        # CLOSE: cm.__exit__ — nghi vấn block/không kill firefox.
        el, ok, info = _run_step("close", wrapper.close)
        row["close_s"] = round(el, 3)
        row["close_ok"] = ok
        if not ok:
            row["close_info"] = info
        print(f"  close:  {el:.2f}s OK={ok}"
              + ("" if ok else f" ({info})"))

        # Chờ ngắn cho OS reclaim process rồi đếm.
        time.sleep(2.0)
        procs_now = _count_firefox_procs()
        row["procs_after_close"] = procs_now
        print(f"  procs: after_launch={procs_after_launch} "
              f"after_close={procs_now} (baseline={baseline})")
        rows.append(row)

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    hdr = (f"{'it':>3}{'build':>8}{'launch':>9}{'mint':>8}{'close':>8}"
           f"{'proc_launch':>13}{'proc_close':>12}")
    print(hdr)
    for r in rows:
        print(f"{r['iter']:>3}{r['build_s']:>8.2f}{r['launch_s']:>9.2f}"
              f"{r['mint_s']:>8.2f}{r['close_s']:>8.2f}"
              f"{r['procs_after_launch']:>13}{r['procs_after_close']:>12}")

    # ── Verdict ──
    print("\n" + "-" * 78)
    launch_times = [r["launch_s"] for r in rows if r["launch_ok"]]
    hung = [r["iter"] for r in rows
            if not r["launch_ok"] or not r["close_ok"]]
    proc_leak = any(
        r["procs_after_close"] > baseline + 1 for r in rows
    )
    if hung:
        print(f"[VERDICT] TREO phát hiện ở iteration(s): {hung} "
              f"→ HỖ TRỢ giả thuyết lifecycle Camoufox tuần tự gây kẹt.")
    elif len(launch_times) >= 3 and launch_times[2] > 3 * max(launch_times[0], 0.5):
        print(f"[VERDICT] launch iter>=3 chậm bất thường ({launch_times}) "
              f"→ degrade dần theo số lần launch.")
    else:
        print("[VERDICT] Mọi iteration launch/close OK, không treo.")
    if proc_leak:
        print(f"[VERDICT] ORPHAN process: firefox không trở về baseline "
              f"sau close → close không kill Firefox (leak tích lũy).")
    else:
        print("[VERDICT] Process trở về baseline sau close — không leak.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
