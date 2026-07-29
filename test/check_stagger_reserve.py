"""Kiểm chứng JobManager._reserve_next_start + _bump_stagger.

Mục tiêu: fix bug wait stagger phình tới hàng trăm giây (vd 381s) do tích luỹ
mốc tương lai ảo. Sau fix: mọi wait luôn ≤ max_seconds (clamp), reset bump gen.

Gọi method thuần qua duck object (method chỉ đụng _stagger_min/max_seconds,
_last_start_ts, _stagger_gen) — không cần khởi tạo JobManager đầy đủ.

Chạy: .venv/bin/python test/check_stagger_reserve.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from web.manager import JobManager  # noqa: E402

_failures = 0


def _result(tc: str, ok: bool, detail: str) -> None:
    global _failures
    tag = "PASS" if ok else "FAIL"
    if not ok:
        _failures += 1
    print(f"[{tag}] {tc} :: {detail}", flush=True)


class _Fake:
    """Duck object đủ field cho _reserve_next_start / _bump_stagger."""

    def __init__(self):
        self._stagger_min_seconds = 5.0
        self._stagger_max_seconds = 10.0
        self._last_start_ts = 0.0
        self._stagger_gen = 0


def run() -> int:
    MAX = 10.0
    MIN = 5.0
    EPS = 0.05

    # TC-01: reserve đầu tiên (last=0) → wait ≈ 0 (start ngay).
    o = _Fake()
    w = JobManager._reserve_next_start(o)
    _result("TC-01 first-reserve-zero", abs(w) <= EPS, f"wait={w:.3f}")

    # TC-02: mốc đã bị đẩy xa tương lai (giả lập tích luỹ ảo / job bị xoá) →
    # clamp wait ≤ max_seconds. Đây là regression cho bug 381s.
    o = _Fake()
    o._last_start_ts = time.monotonic() + 10_000.0  # 10000s ảo
    w = JobManager._reserve_next_start(o)
    _result("TC-02 clamp-no-381s", w <= MAX + EPS,
             f"wait={w:.3f} (phải ≤ {MAX})")

    # TC-03: reserve 100 lần liên tiếp (now gần như đứng yên) → KHÔNG lần nào
    # vượt max_seconds, và mốc cuối không lệch quá max so với now (không tích luỹ).
    o = _Fake()
    max_wait = 0.0
    for _ in range(100):
        w = JobManager._reserve_next_start(o)
        max_wait = max(max_wait, w)
    lead = o._last_start_ts - time.monotonic()
    _result("TC-03 bounded-accumulation",
             max_wait <= MAX + EPS and lead <= MAX + EPS,
             f"max_wait={max_wait:.3f} lead={lead:.3f} (cả 2 ≤ {MAX})")

    # TC-04: 2 reserve liên tiếp cách nhau (spacing) — reserve#2 ≥ min, ≤ max.
    o = _Fake()
    JobManager._reserve_next_start(o)  # #1 = 0
    w2 = JobManager._reserve_next_start(o)  # #2 cách #1 một gap
    _result("TC-04 spacing-gap", MIN - EPS <= w2 <= MAX + EPS,
             f"wait2={w2:.3f} (trong [{MIN}, {MAX}])")

    # TC-05: _bump_stagger reset baseline + bump gen.
    o = _Fake()
    o._last_start_ts = time.monotonic() + 500.0
    gen0 = o._stagger_gen
    JobManager._bump_stagger(o)
    ok = o._last_start_ts == 0.0 and o._stagger_gen == gen0 + 1
    # sau bump, reserve kế tiếp lại về ~0 (start ngay, xoá nợ tích luỹ)
    w = JobManager._reserve_next_start(o)
    _result("TC-05 bump-reset", ok and abs(w) <= EPS,
             f"last={o._last_start_ts} gen={o._stagger_gen} wait_after={w:.3f}")

    total = 5
    print(f"--- {total - _failures}/{total} PASS ---", flush=True)
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(run())
