"""Verify whitelist + validator cho session.mode / upi.mode trong Settings store.

Run: python3 test/check_per_tab_mode_settings.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.repositories import (  # noqa: E402
    _EXACT_KEYS,
    _validate_type_constraint,
    RepositoryError,
)


def tc(idx: int, total: int, name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    suffix = f" :: {detail}" if detail else ""
    print(f"[{tag}] [{idx}/{total}] {name}{suffix}", flush=True)
    return ok


def main() -> int:
    cases: list[tuple[str, callable]] = []

    cases.append((
        "TC-01 whitelist contains session.mode",
        lambda: ("session.mode" in _EXACT_KEYS, str("session.mode" in _EXACT_KEYS)),
    ))
    cases.append((
        "TC-02 whitelist contains upi.mode",
        lambda: ("upi.mode" in _EXACT_KEYS, str("upi.mode" in _EXACT_KEYS)),
    ))
    cases.append((
        "TC-03 reg.mode still in whitelist",
        lambda: ("reg.mode" in _EXACT_KEYS, str("reg.mode" in _EXACT_KEYS)),
    ))

    def _validate_ok(key, value):
        try:
            _validate_type_constraint(key, value)
            return True, "no exception"
        except RepositoryError as exc:
            return False, f"raised RepositoryError: {exc}"

    def _validate_should_fail(key, value):
        try:
            _validate_type_constraint(key, value)
            return False, "no exception (expected fail)"
        except RepositoryError as exc:
            return True, f"raised correctly ({type(exc).__name__})"

    cases.append(("TC-04 session.mode='multi10' OK",
                  lambda: _validate_ok("session.mode", "multi10")))
    cases.append(("TC-05 session.mode='multi30' OK (max default)",
                  lambda: _validate_ok("session.mode", "multi30")))
    cases.append(("TC-06 session.mode='multi200' OK ở backend (cap kiểm soát ở FE)",
                  lambda: _validate_ok("session.mode", "multi200")))
    cases.append(("TC-07 session.mode='garbage' phải fail",
                  lambda: _validate_should_fail("session.mode", "garbage")))
    cases.append(("TC-08 session.mode=42 phải fail (kiểu int)",
                  lambda: _validate_should_fail("session.mode", 42)))

    cases.append(("TC-09 upi.mode='multi30' OK",
                  lambda: _validate_ok("upi.mode", "multi30")))
    cases.append(("TC-10 upi.mode='multi200' OK",
                  lambda: _validate_ok("upi.mode", "multi200")))
    cases.append(("TC-11 upi.mode='unknown' phải fail",
                  lambda: _validate_should_fail("upi.mode", "unknown")))

    cases.append(("TC-12 reg.mode='multi10' vẫn OK",
                  lambda: _validate_ok("reg.mode", "multi10")))
    cases.append(("TC-13 reg.mode='multi200' OK ở backend",
                  lambda: _validate_ok("reg.mode", "multi200")))

    total = len(cases)
    passed = 0
    for i, (name, fn) in enumerate(cases, 1):
        ok, detail = fn()
        if tc(i, total, name, ok, detail):
            passed += 1
    print(f"=== Summary: {passed}/{total} PASS ===", flush=True)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
