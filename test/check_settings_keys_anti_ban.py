"""Task 1.3 verify — 6 settings keys mới cho anti-ban hardening.

Mục tiêu:
    - 6 keys vào _EXACT_KEYS whitelist.
    - _validate_type_constraint accept giá trị hợp lệ, reject giá trị sai.
    - Audit log redact đúng (sensitive chỉ áp các key cũ, 6 keys mới
      không sensitive).

Chạy: python3 test/check_settings_keys_anti_ban.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


KEYS = [
    "reg.persona",
    "reg.fresh_profile",
    "reg.har_validate",
    "reg.human_typing_delay_ms_min",
    "reg.human_typing_delay_ms_max",
    "reg.locale_auto_geo",
]


def _section(title: str) -> None:
    print()
    print(f"── {title} ──")


def main() -> int:
    from db.repositories import (
        _EXACT_KEYS,
        _SENSITIVE_KEYS,
        _validate_type_constraint,
        RepositoryError,
    )

    failures: list[str] = []

    _section("TC-01 Whitelist membership")
    for k in KEYS:
        if k in _EXACT_KEYS:
            print(f"  [PASS] {k} in _EXACT_KEYS")
        else:
            print(f"  [FAIL] {k} MISSING from _EXACT_KEYS")
            failures.append(f"{k} not in _EXACT_KEYS")

    _section("TC-02 Sensitive — không leak runtime persona")
    # Đảm bảo 6 keys mới KHÔNG đánh dấu sensitive (chúng là config, không
    # phải secret). Ngược lại sẽ làm log debug khó đọc.
    for k in KEYS:
        if k in _SENSITIVE_KEYS:
            print(f"  [FAIL] {k} bị đánh dấu sensitive (không nên)")
            failures.append(f"{k} unexpected sensitive")
        else:
            print(f"  [PASS] {k} không sensitive")

    _section("TC-03 reg.persona — accept enum string")
    for v in ("firefox_mac", "chrome_win"):
        try:
            _validate_type_constraint("reg.persona", v)
            print(f"  [PASS] accept reg.persona={v!r}")
        except RepositoryError as e:
            failures.append(f"persona {v!r} should accept: {e}")
            print(f"  [FAIL] reject {v!r}: {e}")
    for v in ("safari", "edge", "", None, 1, True):
        try:
            _validate_type_constraint("reg.persona", v)
            failures.append(f"persona {v!r} should reject")
            print(f"  [FAIL] accept invalid {v!r}")
        except RepositoryError:
            print(f"  [PASS] reject invalid reg.persona={v!r}")

    _section("TC-04 Bool keys — accept True/False, reject str/int")
    for k in ("reg.fresh_profile", "reg.har_validate", "reg.locale_auto_geo"):
        for v in (True, False):
            try:
                _validate_type_constraint(k, v)
                print(f"  [PASS] {k}={v}")
            except RepositoryError as e:
                failures.append(f"{k}={v} should accept: {e}")
                print(f"  [FAIL] {k}={v}: {e}")
        for v in ("true", "1", 1, 0, None):
            try:
                _validate_type_constraint(k, v)
                failures.append(f"{k}={v!r} should reject")
                print(f"  [FAIL] {k} accept invalid {v!r}")
            except RepositoryError:
                print(f"  [PASS] {k} reject {v!r}")

    _section("TC-05 Typing delay int range")
    cases_min = [
        ("reg.human_typing_delay_ms_min", 40, True),
        ("reg.human_typing_delay_ms_min", 120, True),
        ("reg.human_typing_delay_ms_min", 500, True),
        ("reg.human_typing_delay_ms_min", 39, False),    # below
        ("reg.human_typing_delay_ms_min", 501, False),   # above
        ("reg.human_typing_delay_ms_min", 100.0, False),  # float
        ("reg.human_typing_delay_ms_min", True, False),  # bool blocked
    ]
    cases_max = [
        ("reg.human_typing_delay_ms_max", 60, True),
        ("reg.human_typing_delay_ms_max", 260, True),
        ("reg.human_typing_delay_ms_max", 800, True),
        ("reg.human_typing_delay_ms_max", 59, False),
        ("reg.human_typing_delay_ms_max", 801, False),
        ("reg.human_typing_delay_ms_max", "260", False),
    ]
    for key, value, should_pass in cases_min + cases_max:
        try:
            _validate_type_constraint(key, value)
            if should_pass:
                print(f"  [PASS] {key}={value}")
            else:
                failures.append(f"{key}={value!r} should reject")
                print(f"  [FAIL] {key} accept invalid {value!r}")
        except RepositoryError as e:
            if should_pass:
                failures.append(f"{key}={value} should accept: {e}")
                print(f"  [FAIL] {key}={value}: {e}")
            else:
                print(f"  [PASS] {key} reject {value!r}")

    print()
    if failures:
        print(f"[FAIL] {len(failures)} failures:")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("[OK] All Task 1.3 settings keys invariants pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
