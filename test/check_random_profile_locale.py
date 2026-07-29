"""Task 1.6 verify — random_profile_for_locale chọn name pool theo locale.

Mục tiêu:
    - en-IN, hi-* → tên Ấn (in pool _IN_FIRST_NAMES + _IN_LAST_NAMES).
    - en-US, en-GB, en-AU, en, None → tên US/EU (in pool _FIRST_NAMES + _LAST_NAMES).
    - SignupRequest có field locale (default None).
    - signup.py wire random_profile_for_locale(request.locale).

Chạy: .venv/bin/python3 test/check_random_profile_locale.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    failures: list[str] = []

    from random_profile import (
        random_profile_for_locale,
        _IN_FIRST_NAMES,
        _IN_LAST_NAMES,
        _FIRST_NAMES,
        _LAST_NAMES,
    )

    # Sample size đủ lớn để bắt false positive (nếu in/us pool có overlap thì
    # cần explicitly check name không thuộc set khác).
    SAMPLE = 50

    # ── TC-01 en-IN → tên Ấn ────────────────────────────
    in_pool = set(_IN_FIRST_NAMES) | set(_IN_LAST_NAMES)
    us_pool = set(_FIRST_NAMES) | set(_LAST_NAMES)
    in_minus_us = in_pool - us_pool   # tên CHỈ thuộc India pool
    us_minus_in = us_pool - in_pool

    indian_locales = ("en-IN", "EN-in", "hi", "hi-IN", "ta-IN", "te-IN", "bn-IN")
    for loc in indian_locales:
        seen_in = 0
        for _ in range(SAMPLE):
            p = random_profile_for_locale(loc)
            assert "name" in p and "age" in p and "password" in p and "birthdate" in p, \
                f"profile missing keys: {p.keys()}"
            first, _, last = p["name"].partition(" ")
            if first in in_minus_us or last in in_minus_us:
                seen_in += 1
        # Heuristic: ít nhất 60% sample phải có ít nhất 1 token thuộc pool India only.
        # Pool India + US có overlap ("Aadi", ...) — không thể 100%.
        if seen_in / SAMPLE >= 0.6:
            print(f"  [PASS] locale={loc!r}: {seen_in}/{SAMPLE} có India-only token (≥60%)")
        else:
            failures.append(
                f"locale={loc!r}: chỉ {seen_in}/{SAMPLE} có India token (cần ≥60%)"
            )
            print(f"  [FAIL] locale={loc!r}: {seen_in}/{SAMPLE}")

    # ── TC-02 en-US, en-GB, None → tên US/EU ────────────────
    us_locales = (None, "en-US", "en-GB", "en-AU", "en-CA", "en", "fr-FR")
    for loc in us_locales:
        seen_us = 0
        seen_indian_only = 0
        for _ in range(SAMPLE):
            p = random_profile_for_locale(loc)
            first, _, last = p["name"].partition(" ")
            if first in us_minus_in or last in us_minus_in:
                seen_us += 1
            if first in in_minus_us or last in in_minus_us:
                seen_indian_only += 1
        if seen_us / SAMPLE >= 0.6 and seen_indian_only == 0:
            print(f"  [PASS] locale={loc!r}: {seen_us}/{SAMPLE} US-only, 0 India-only token")
        else:
            failures.append(
                f"locale={loc!r}: us={seen_us}/{SAMPLE}, india-only={seen_indian_only}"
            )
            print(f"  [FAIL] locale={loc!r}: us={seen_us}, india={seen_indian_only}")

    # ── TC-03 Profile shape ────────────────────────────────
    p = random_profile_for_locale("en-IN")
    expected_keys = {"name", "age", "password", "birthdate"}
    if set(p.keys()) == expected_keys:
        print(f"  [PASS] India profile shape: {expected_keys}")
    else:
        failures.append(f"India profile keys = {set(p.keys())}, expect {expected_keys}")

    # ── TC-04 SignupRequest field locale ───────────────────
    from models import SignupRequest

    req = SignupRequest(email="x@y.z")
    if req.locale is None:
        print("[PASS] SignupRequest.locale default = None")
    else:
        failures.append(f"SignupRequest.locale default = {req.locale!r}")
        print(f"[FAIL] SignupRequest.locale default = {req.locale!r}")

    req2 = SignupRequest(email="x@y.z", locale="en-IN")
    if req2.locale == "en-IN":
        print("[PASS] SignupRequest.locale accept 'en-IN'")
    else:
        failures.append(f"SignupRequest.locale != en-IN: {req2.locale!r}")

    # ── TC-05 signup.py wire ────────────────────────────
    src = (ROOT / "signup.py").read_text(encoding="utf-8")
    if "random_profile_for_locale" in src:
        print("[PASS] signup.py imports + uses random_profile_for_locale")
    else:
        failures.append("signup.py chưa wire random_profile_for_locale")
        print("[FAIL] signup.py chưa wire random_profile_for_locale")

    print()
    if failures:
        print(f"[FAIL] {len(failures)} failures:")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("[OK] All Task 1.6 random_profile_for_locale invariants pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
