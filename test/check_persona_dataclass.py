"""Task 3.1 verify — BrowserPersona dataclass + 2 instances + backward compat.

Mục tiêu:
    - 2 personas: CHROME_145_WIN, FIREFOX_135_MAC.
    - Firefox persona: sec_ch_ua=None, accept_language=q=0.5, camoufox_os=("mac",).
    - Chrome persona: sec_ch_ua đầy đủ, q=0.9, camoufox_os=("windows",).
    - get_persona("chrome_win") + get_persona("firefox_mac") work.
    - Top-level constants vẫn export (backward compat).
    - common_headers() tôn trọng persona (Chrome có sec-ch-ua, Firefox không).
    - navigator_payload() Chrome có brands, Firefox brands=[].

Chạy: .venv/bin/python3 test/check_persona_dataclass.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    failures: list[str] = []

    # ── TC-01 Module exports ──
    import user_agent_profile as uap

    expected_new = [
        "BrowserPersona", "CHROME_145_WIN", "FIREFOX_135_MAC",
        "get_persona", "sentinel_navigator_payload", "common_chrome_headers",
    ]
    for sym in expected_new:
        if hasattr(uap, sym):
            print(f"  [PASS] export {sym}")
        else:
            failures.append(f"missing export: {sym}")

    expected_compat = [
        "WINDOWS_USER_AGENT", "SEC_CH_UA", "SEC_CH_UA_MOBILE", "SEC_CH_UA_PLATFORM",
        "CURL_IMPERSONATE_PRIMARY", "CURL_IMPERSONATE_CANDIDATES", "CAMOUFOX_OS",
        "NAVIGATOR_LANGUAGE", "HARDWARE_CONCURRENCY", "DEVICE_MEMORY_GB",
    ]
    for sym in expected_compat:
        if hasattr(uap, sym):
            print(f"  [PASS] backward compat {sym}")
        else:
            failures.append(f"missing backward compat: {sym}")

    # ── TC-02 Chrome persona invariants ──
    chrome = uap.CHROME_145_WIN
    assert chrome.name == "chrome_win", chrome.name
    assert "Chrome/145" in chrome.user_agent
    assert "Windows NT 10.0" in chrome.user_agent
    assert chrome.sec_ch_ua is not None and "Chromium" in chrome.sec_ch_ua
    assert chrome.sec_ch_ua_platform == '"Windows"'
    assert chrome.accept_language == "en-US,en;q=0.9"  # Chrome q-value
    assert chrome.camoufox_os == ("windows",)
    assert chrome.curl_impersonate_primary == "chrome145"
    assert chrome.arch == "x86"
    print("  [PASS] CHROME_145_WIN invariants")

    # ── TC-03 Firefox persona invariants ──
    firefox = uap.FIREFOX_135_MAC
    assert firefox.name == "firefox_mac", firefox.name
    assert "Firefox/135" in firefox.user_agent
    assert "Macintosh" in firefox.user_agent
    assert firefox.sec_ch_ua is None  # Firefox KHÔNG gửi
    assert firefox.sec_ch_ua_mobile is None
    assert firefox.sec_ch_ua_platform is None
    assert firefox.accept_language == "en-US,en;q=0.5"  # Firefox q-value
    assert firefox.camoufox_os == ("mac",)
    assert firefox.arch == "arm"
    print("  [PASS] FIREFOX_135_MAC invariants")

    # ── TC-04 get_persona lookup ──
    p1 = uap.get_persona("chrome_win")
    assert p1 is chrome
    p2 = uap.get_persona("firefox_mac")
    assert p2 is firefox
    try:
        uap.get_persona("safari")
        failures.append("get_persona unknown should raise ValueError")
    except ValueError:
        pass
    print("  [PASS] get_persona lookup + reject unknown")

    # ── TC-05 common_headers — Chrome có sec-ch-ua, Firefox không ──
    h_chrome = chrome.common_headers(referer="https://x.com/")
    assert h_chrome["User-Agent"].startswith("Mozilla/5.0 (Windows")
    assert h_chrome["Accept-Language"] == "en-US,en;q=0.9"
    assert "sec-ch-ua" in h_chrome
    assert h_chrome["sec-ch-ua-mobile"] == "?0"
    assert h_chrome["sec-ch-ua-platform"] == '"Windows"'
    assert h_chrome["Referer"] == "https://x.com/"
    print("  [PASS] CHROME common_headers (3 sec-ch-ua + UA + Accept-Lang)")

    h_firefox = firefox.common_headers()
    assert h_firefox["User-Agent"].startswith("Mozilla/5.0 (Macintosh")
    assert h_firefox["Accept-Language"] == "en-US,en;q=0.5"
    assert "sec-ch-ua" not in h_firefox
    assert "sec-ch-ua-mobile" not in h_firefox
    assert "sec-ch-ua-platform" not in h_firefox
    print("  [PASS] FIREFOX common_headers (KHÔNG sec-ch-ua — đặc trưng)")

    # ── TC-06 navigator_payload — Chrome có brands, Firefox brands=[] ──
    chrome_p = chrome.navigator_payload()
    assert chrome_p["user_agent"] == chrome.user_agent
    assert isinstance(chrome_p["sec_ch_ua_brands"], list)
    assert len(chrome_p["sec_ch_ua_brands"]) >= 2
    assert any(b["brand"] == "Google Chrome" for b in chrome_p["sec_ch_ua_brands"])
    assert chrome_p["sec_ch_ua_platform"] == "Windows"
    assert chrome_p["sec_ch_ua_arch"] == "x86"
    print("  [PASS] CHROME navigator_payload (brands + platform Windows)")

    firefox_p = firefox.navigator_payload()
    assert firefox_p["user_agent"] == firefox.user_agent
    assert firefox_p["sec_ch_ua_brands"] == []   # ← critical
    assert firefox_p["sec_ch_ua_mobile"] is False
    assert firefox_p["sec_ch_ua_platform"] == ""
    assert firefox_p["sec_ch_ua_arch"] == "arm"
    print("  [PASS] FIREFOX navigator_payload (brands=[] — sentinel sees no userAgentData)")

    # ── TC-07 sentinel_navigator_payload backward compat ──
    payload_default = uap.sentinel_navigator_payload()  # no arg = Chrome
    assert payload_default["user_agent"] == chrome.user_agent
    payload_firefox = uap.sentinel_navigator_payload(firefox)
    assert payload_firefox["user_agent"] == firefox.user_agent
    print("  [PASS] sentinel_navigator_payload backward compat + persona arg")

    # ── TC-08 curl_impersonate_candidates ──
    c_cand = chrome.curl_impersonate_candidates
    assert c_cand[0] == "chrome145"
    assert c_cand == ("chrome145", "chrome142", "chrome136")
    f_cand = firefox.curl_impersonate_candidates
    assert f_cand[0] == "firefox135"
    print("  [PASS] curl_impersonate_candidates rotation chains")

    # ── TC-09 Top-level constants alias đúng Chrome ──
    assert uap.WINDOWS_USER_AGENT == chrome.user_agent
    assert uap.SEC_CH_UA == chrome.sec_ch_ua
    assert uap.CAMOUFOX_OS == chrome.camoufox_os
    assert uap.CURL_IMPERSONATE_PRIMARY == "chrome145"
    print("  [PASS] backward compat constants = CHROME_145_WIN values")

    # ── TC-10 Frozen dataclass — không thể mutate ──
    try:
        chrome.user_agent = "fake"  # type: ignore[misc]
        failures.append("BrowserPersona phải frozen — không cho mutate")
    except (AttributeError, Exception):
        pass
    print("  [PASS] BrowserPersona frozen (immutable)")

    print()
    if failures:
        print(f"[FAIL] {len(failures)} failures:")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("[OK] All Task 3.1 BrowserPersona invariants pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
