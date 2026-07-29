"""Verify watchdog gia hạn deadline theo checkpoint OTP.

Chạy: python3 test/check_otp_grace.py

Kịch bản:
  TC-01 coro xong trong base deadline → trả kết quả, không timeout.
  TC-02 coro chạy quá base, KHÔNG checkpoint → raise asyncio.TimeoutError.
  TC-03 coro chạy quá base nhưng checkpoint gia hạn kịp → hoàn tất OK
        (đây chính là case "đã lấy được OTP thì không bị kill").
  TC-04 checkpoint chỉ gia hạn (max), không rút ngắn deadline.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from signup import await_with_extendable_deadline, make_otp_grace_checkpoint  # noqa: E402


def _p(ok: bool, tc: str, desc: str, detail: str) -> bool:
    tag = "[PASS]" if ok else "[FAIL]"
    print(f"{tag} {tc} — {desc} :: {detail}", flush=True)
    return ok


async def _main() -> int:
    loop = asyncio.get_event_loop()
    results: list[bool] = []

    # TC-01: xong trong base
    async def quick():
        await asyncio.sleep(0.05)
        return "done"
    holder = [loop.time() + 1.0]
    try:
        r = await await_with_extendable_deadline(quick(), holder, loop=loop)
        results.append(_p(r == "done", "TC-01", "coro xong trong base", f"result={r!r}"))
    except Exception as exc:
        results.append(_p(False, "TC-01", "coro xong trong base", f"unexpected {type(exc).__name__}: {exc}"))

    # TC-02: quá base, không checkpoint → TimeoutError
    async def slow():
        await asyncio.sleep(1.0)
        return "late"
    holder = [loop.time() + 0.2]
    try:
        await await_with_extendable_deadline(slow(), holder, loop=loop)
        results.append(_p(False, "TC-02", "quá base không gia hạn", "không raise (sai)"))
    except asyncio.TimeoutError:
        results.append(_p(True, "TC-02", "quá base không gia hạn", "raise TimeoutError đúng"))
    except Exception as exc:
        results.append(_p(False, "TC-02", "quá base không gia hạn", f"sai exception {type(exc).__name__}"))

    # TC-03: checkpoint gia hạn kịp → hoàn tất
    holder = [loop.time() + 0.2]
    cp = make_otp_grace_checkpoint(holder, grace=1.0, loop=loop)

    async def work_then_checkpoint():
        await asyncio.sleep(0.1)
        cp("otp")          # "lấy được OTP" → gia hạn +1.0s
        await asyncio.sleep(0.5)  # bước post-OTP, vượt base ban đầu (0.2s)
        return "finished"
    try:
        r = await await_with_extendable_deadline(work_then_checkpoint(), holder, loop=loop)
        results.append(_p(r == "finished", "TC-03", "checkpoint gia hạn kịp", f"result={r!r} (không bị kill sau OTP)"))
    except Exception as exc:
        results.append(_p(False, "TC-03", "checkpoint gia hạn kịp", f"bị kill sai: {type(exc).__name__}: {exc}"))

    # TC-04: checkpoint không rút ngắn deadline đang xa
    holder = [loop.time() + 10.0]
    cp = make_otp_grace_checkpoint(holder, grace=0.5, loop=loop)
    before = holder[0]
    cp("otp")
    results.append(_p(holder[0] == before, "TC-04", "checkpoint chỉ tăng không giảm", f"deadline giữ nguyên ({holder[0]-before:+.3f}s)"))

    print("", flush=True)
    n_pass = sum(results)
    print(f"[SUMMARY] {n_pass}/{len(results)} PASS", flush=True)
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
