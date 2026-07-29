"""Task 1.4 verify — locale auto-detect theo proxy country.

Mục tiêu:
    - Module ``_geo_locale.py`` export 3 function chính.
    - ``locale_for_country`` map đúng cho top 10 country.
    - ``locale_for_country`` fallback US khi cc=None hoặc unknown.
    - ``lookup_proxy_country`` cache theo proxy URL.
    - browser_phase.py wire ``resolve_proxy_locale`` + dùng ``resolved_locale``.

KHÔNG test live HTTP (cần proxy thật). Chỉ test pure logic + source-level wire.

Chạy: .venv/bin/python3 test/check_locale_geo_mapping.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    failures: list[str] = []

    # ── TC-01 Module exports ──────────────────────────────
    from _geo_locale import (
        lookup_proxy_country,
        locale_for_country,
        resolve_proxy_locale,
        clear_cache,
    )
    print("[PASS] _geo_locale exports 4 helpers")

    # ── TC-02 locale_for_country mapping ──────────────────
    cases = [
        ("IN", "en-IN", "Asia/Kolkata"),
        ("US", "en-US", "America/New_York"),
        ("GB", "en-GB", "Europe/London"),
        ("AU", "en-AU", "Australia/Sydney"),
        ("CA", "en-CA", "America/Toronto"),
        ("DE", "de-DE", "Europe/Berlin"),
        ("FR", "fr-FR", "Europe/Paris"),
        ("JP", "ja-JP", "Asia/Tokyo"),
        ("BR", "pt-BR", "America/Sao_Paulo"),
        ("ID", "id-ID", "Asia/Jakarta"),
        ("VN", "vi-VN", "Asia/Ho_Chi_Minh"),
        ("in", "en-IN", "Asia/Kolkata"),  # case-insensitive
    ]
    for cc, want_loc, want_tz in cases:
        loc, tz, geo = locale_for_country(cc)
        if loc == want_loc and tz == want_tz and isinstance(geo, tuple) and len(geo) == 2:
            print(f"  [PASS] {cc} → {loc}/{tz}")
        else:
            failures.append(
                f"{cc}: got ({loc}, {tz}) expect ({want_loc}, {want_tz})"
            )
            print(f"  [FAIL] {cc} → ({loc}, {tz}) expect ({want_loc}, {want_tz})")

    # ── TC-03 Default fallback ────────────────────────────
    for cc in (None, "", "ZZ", "XX", "  "):
        loc, tz, geo = locale_for_country(cc)
        if loc == "en-US" and tz == "America/New_York":
            print(f"  [PASS] cc={cc!r} → default en-US/NY")
        else:
            failures.append(f"cc={cc!r}: got ({loc}, {tz}) expect default")

    # ── TC-04 Cache behavior ──────────────────────────────
    # lookup_proxy_country với proxy=None → trả None ngay, không hit network.
    clear_cache()
    cc = lookup_proxy_country(None)
    if cc is None:
        print("[PASS] lookup_proxy_country(None) → None (no network)")
    else:
        failures.append(f"lookup(None) = {cc}, expect None")

    # ── TC-05 resolve_proxy_locale với proxy=None → defaults ────
    loc, tz, geo, cc = resolve_proxy_locale(None)
    if loc == "en-US" and tz == "America/New_York" and cc is None:
        print("[PASS] resolve_proxy_locale(None) → defaults + cc=None")
    else:
        failures.append(f"resolve(None) = ({loc}, {tz}, {cc})")

    # ── TC-06 SignupRequest field locale + timezone ────────
    from models import SignupRequest

    req = SignupRequest(email="x@y.z", locale="en-IN", timezone="Asia/Kolkata")
    if req.locale == "en-IN" and req.timezone == "Asia/Kolkata":
        print("[PASS] SignupRequest accept locale + timezone fields")
    else:
        failures.append(f"SignupRequest fields: {req.locale!r}/{req.timezone!r}")

    # ── TC-07 browser_phase.py wire ───────────────────────
    src = (ROOT / "browser_phase.py").read_text(encoding="utf-8")
    must_have = [
        ("from _geo_locale import resolve_proxy_locale", "import resolve_proxy_locale"),
        ("resolved_locale", "biến resolved_locale"),
        ("resolved_timezone", "biến resolved_timezone"),
        ("resolved_geo", "biến resolved_geo"),
        ("locale=resolved_locale", "Camoufox locale=resolved_locale"),
    ]
    for needle, desc in must_have:
        if needle in src:
            print(f"  [PASS] browser_phase.py: {desc}")
        else:
            failures.append(f"browser_phase.py thiếu: {desc} (needle={needle!r})")
            print(f"  [FAIL] browser_phase.py thiếu: {desc}")

    must_not_have = ['locale="en-US"']
    for needle in must_not_have:
        if needle in src:
            failures.append(f"browser_phase.py vẫn còn hardcoded: {needle}")
            print(f"  [FAIL] browser_phase.py vẫn hardcoded {needle}")
        else:
            print(f"  [PASS] browser_phase.py: KHÔNG còn hardcoded {needle}")

    print()
    if failures:
        print(f"[FAIL] {len(failures)} failures:")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("[OK] All Task 1.4 locale auto-detect invariants pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
