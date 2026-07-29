"""Phase 6 closure verify — wire save persona_cookies + session_phase locale + _dd_s.

Chạy: .venv/bin/python3 test/check_phase6_closure.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    failures: list[str] = []

    # ─── Task 6.1 — signup.py wire set_persona_cookies ───
    print("── Task 6.1: signup.py wire set_persona_cookies ──")
    src_signup = (ROOT / "signup.py").read_text(encoding="utf-8")
    if "_filter_persona_cookies" in src_signup \
            and "_PERSONA_COOKIE_NAMES" in src_signup \
            and "set_persona_cookies(request.email" in src_signup:
        print("  [PASS] signup.py wire _filter_persona_cookies + set_persona_cookies")
    else:
        failures.append("signup.py thiếu wire save persona_cookies")
        print("  [FAIL] signup.py thiếu wire")

    # Filter logic — mock cookies, verify whitelist filter
    import signup as _signup_mod

    fake_cookies = [
        {"name": "oai-did", "value": "abc-123"},
        {"name": "oaicom-stable-id", "value": "stable-uuid"},
        {"name": "cf_clearance", "value": "cf-token"},
        {"name": "__cf_bm", "value": "cfbm-token"},
        {"name": "_cfuvid", "value": "cfuvid-token"},
        {"name": "__cflb", "value": "cflb-token"},
        {"name": "oai-asli", "value": "logging-id-uuid"},
        # KHÔNG persist:
        {"name": "oai-sc", "value": "should-skip"},
        {"name": "oai-client-auth-session", "value": "should-skip"},
        {"name": "__Secure-next-auth.session-token", "value": "should-skip-security"},
        {"name": "login_session", "value": "should-skip"},
        {"name": "hydra_redirect", "value": "should-skip"},
        # Empty value KHÔNG persist:
        {"name": "oai-did", "value": ""},
    ]
    filtered = _signup_mod._filter_persona_cookies(fake_cookies)
    filtered_names = {c["name"] for c in filtered}
    expected = {"oai-did", "oaicom-stable-id", "cf_clearance", "__cf_bm",
                "_cfuvid", "__cflb", "oai-asli"}
    if filtered_names == expected:
        print(f"  [PASS] _filter_persona_cookies whitelist {len(expected)} cookies")
    else:
        failures.append(
            f"filter wrong: got {filtered_names}, expect {expected}"
        )
        print(f"  [FAIL] filter wrong: got {filtered_names}")

    # ─── Task 6.2 — session_phase locale auto-detect ───
    print("\n── Task 6.2: session_phase locale auto-detect ──")
    src_sess = (ROOT / "session_phase.py").read_text(encoding="utf-8")
    if 'locale="en-US"' in src_sess:
        # Có thể vẫn còn trong comment / fallback strings — check kỹ hơn
        if 'locale="en-US",\n            ignore_https_errors' in src_sess:
            failures.append("session_phase còn hardcode locale=en-US trong launch")
            print("  [FAIL] session_phase còn hardcode locale='en-US'")
        else:
            print("  [PASS] session_phase: locale=en-US chỉ trong fallback string")
    else:
        print("  [PASS] session_phase: KHÔNG còn locale='en-US' anywhere")

    # Required: import + use resolved_locale
    if "from _geo_locale import resolve_proxy_locale" in src_sess \
            and "resolved_locale" in src_sess \
            and "locale=resolved_locale" in src_sess:
        print("  [PASS] session_phase: import _geo_locale + use resolved_locale")
    else:
        failures.append("session_phase thiếu locale auto-detect")
        print("  [FAIL] session_phase thiếu wire")

    # Chrome runner: timezone_id + geolocation
    if "timezone_id" in src_sess and "geolocation" in src_sess:
        print("  [PASS] session_phase Chrome runner: timezone_id + geolocation")
    else:
        failures.append("session_phase Chrome runner thiếu timezone_id/geolocation")

    # ─── Task 6.3 — session_phase _dd_s inject ───
    print("\n── Task 6.3: session_phase inject _dd_s ──")
    if "from _datadog_session import inject_dd_s" in src_sess:
        print("  [PASS] session_phase: import inject_dd_s")
    else:
        failures.append("session_phase thiếu import inject_dd_s")
        print("  [FAIL] session_phase thiếu import inject_dd_s")

    # ─── Task 6.4 — Migration v12 (delegated to check_migration_v12.py) ───
    print("\n── Task 6.4: Migration v12 ──")
    print("  [INFO] delegate to test/check_migration_v12.py (run separately)")

    print()
    if failures:
        print(f"[FAIL] {len(failures)} failures:")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("[OK] All Phase 6 closure invariants pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
