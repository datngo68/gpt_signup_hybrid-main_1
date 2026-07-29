"""Check cap reg.max_concurrent đã nâng lên 30 (đồng bộ UI).

Chạy: .venv/bin/python test/check_reg_max_concurrent_cap.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db.repositories import _validate_type_constraint, RepositoryError  # noqa: E402

_fails = 0


def _check(ok: bool, tag: str, desc: str, detail: str = "") -> None:
    global _fails
    status = "PASS" if ok else "FAIL"
    if not ok:
        _fails += 1
    print(f"[{status}] {tag} — {desc} :: {detail}", flush=True)


def main() -> int:
    # Trường hợp gây lỗi user báo: 10, 20 — phải PASS sau fix.
    for i, v in enumerate([1, 5, 10, 20, 30], 1):
        try:
            _validate_type_constraint("reg.max_concurrent", v)
            _check(True, f"OK-{i:02d}", f"reg.max_concurrent={v} chấp nhận")
        except RepositoryError as exc:
            _check(False, f"OK-{i:02d}", f"reg.max_concurrent={v} BỊ TỪ CHỐI", str(exc))

    # Trường hợp boundary: 0, 31 phải reject.
    for i, v in enumerate([0, 31, 100, -1], 1):
        try:
            _validate_type_constraint("reg.max_concurrent", v)
            _check(False, f"BAD-{i:02d}", f"reg.max_concurrent={v} bị accept (phải reject)")
        except RepositoryError:
            _check(True, f"BAD-{i:02d}", f"reg.max_concurrent={v} reject đúng")

    # Type mismatch.
    for i, v in enumerate([True, "10", 10.5, None], 1):
        try:
            _validate_type_constraint("reg.max_concurrent", v)
            _check(False, f"TYPE-{i:02d}", f"reg.max_concurrent={v!r} bị accept")
        except RepositoryError:
            _check(True, f"TYPE-{i:02d}", f"reg.max_concurrent={v!r} reject đúng")

    print(f"\n=== {'ALL PASS' if _fails == 0 else str(_fails) + ' FAIL'} ===", flush=True)
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
