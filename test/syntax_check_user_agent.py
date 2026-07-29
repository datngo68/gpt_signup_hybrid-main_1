"""Syntax + import check cho refactor user_agent_profile (Option A).

Verify:
    1. Tất cả file python sửa parse được (AST).
    2. user_agent_profile.py expose đúng symbol.
    3. Các module reg core import được không vòng lặp.
    4. Constant đồng bộ giữa UA / sec-ch-ua / impersonate.
    5. SignupRequest default đã đổi sang Windows Chrome.

Chạy: ``.venv/bin/python test/syntax_check_user_agent.py`` (từ repo root).
Output: từng bước [PASS]/[FAIL] flush realtime.
"""
from __future__ import annotations

import ast
import importlib
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_NAME = REPO_ROOT.name


def _log(label: str, msg: str) -> None:
    print(f"{label} {msg}", flush=True)


# ─── Files cần parse syntax ───────────────────────────────────────────

TARGET_FILES = [
    "user_agent_profile.py",
    "request_phase.py",
    "session_phase.py",
    "browser_phase.py",
    "sentinel_pow.py",
    "sentinel_quickjs.py",
    "mfa_phase.py",
    "models.py",
    "cli.py",
    "http_phase.py",
    # UPI flow
    "pay_upi_http.py",
    "payment_link.py",
    "stripe_token.py",
    "record_pay_upi.py",
    "web/upi_runner.py",
]


def step_syntax_parse() -> None:
    _log("[STEP]", "TC-01 — AST parse các file đã sửa")
    for idx, name in enumerate(TARGET_FILES, 1):
        path = REPO_ROOT / name
        if not path.exists():
            _log("[FAIL]", f"TC-01.{idx} — file missing: {name}")
            sys.exit(1)
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            _log("[FAIL]", f"TC-01.{idx} — {name}: {exc}")
            sys.exit(1)
        _log("[PASS]", f"TC-01.{idx} — {name} parse OK")


# ─── Import chain (không vòng) ────────────────────────────────────────

# Import qua package name vì các module dùng `from .config import ...`
# nên phải import qua parent package.
def step_import_modules() -> None:
    _log("[STEP]", "TC-02 — import package + symbol check")

    # Đặt parent dir vào sys.path để import package theo tên thư mục.
    parent = str(REPO_ROOT.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)

    targets = [
        (f"{PACKAGE_NAME}.user_agent_profile", [
            "WINDOWS_USER_AGENT", "SEC_CH_UA", "SEC_CH_UA_MOBILE",
            "SEC_CH_UA_PLATFORM", "CURL_IMPERSONATE_PRIMARY",
            "CURL_IMPERSONATE_CANDIDATES", "CAMOUFOX_OS",
            "common_chrome_headers", "sentinel_navigator_payload",
        ]),
        (f"{PACKAGE_NAME}.request_phase", ["USER_AGENT", "_IMPERSONATE_CANDIDATES"]),
        (f"{PACKAGE_NAME}.sentinel_pow", ["DEFAULT_UA", "DEFAULT_SEC_CH_UA"]),
        (f"{PACKAGE_NAME}.sentinel_quickjs", ["get_sentinel_token_via_quickjs"]),
        (f"{PACKAGE_NAME}.models", ["SignupRequest"]),
    ]
    for idx, (mod_name, syms) in enumerate(targets, 1):
        try:
            mod = importlib.import_module(mod_name)
        except Exception as exc:
            _log("[FAIL]", f"TC-02.{idx} — import {mod_name}: {type(exc).__name__}: {exc}")
            sys.exit(1)
        missing = [s for s in syms if not hasattr(mod, s)]
        if missing:
            _log("[FAIL]", f"TC-02.{idx} — {mod_name} thiếu symbol: {missing}")
            sys.exit(1)
        _log("[PASS]", f"TC-02.{idx} — {mod_name} OK ({len(syms)} symbols)")


# ─── Constant consistency ─────────────────────────────────────────────

def step_constant_consistency() -> None:
    _log("[STEP]", "TC-03 — constant đồng bộ Chrome major")
    from gpt_signup_hybrid_new import user_agent_profile as uap
    from gpt_signup_hybrid_new import request_phase as rp
    from gpt_signup_hybrid_new import sentinel_pow as sp
    from gpt_signup_hybrid_new import models as md

    major = uap.CHROME_MAJOR

    # 1. UA chứa "Chrome/{major}.0.0.0"
    if f"Chrome/{major}.0.0.0" not in uap.WINDOWS_USER_AGENT:
        _log("[FAIL]", f"TC-03.1 — UA không khớp CHROME_MAJOR={major}: {uap.WINDOWS_USER_AGENT}")
        sys.exit(1)
    _log("[PASS]", f"TC-03.1 — WINDOWS_USER_AGENT chứa Chrome/{major}")

    # 2. UA bắt đầu Windows NT
    if "Windows NT 10.0" not in uap.WINDOWS_USER_AGENT:
        _log("[FAIL]", f"TC-03.2 — UA không phải Windows: {uap.WINDOWS_USER_AGENT}")
        sys.exit(1)
    _log("[PASS]", "TC-03.2 — UA chứa 'Windows NT 10.0'")

    # 3. sec-ch-ua chứa version major
    if f'"{major}"' not in uap.SEC_CH_UA:
        _log("[FAIL]", f"TC-03.3 — SEC_CH_UA không khớp version: {uap.SEC_CH_UA}")
        sys.exit(1)
    _log("[PASS]", f"TC-03.3 — SEC_CH_UA chứa version {major}")

    # 4. sec-ch-ua-platform = "Windows"
    if uap.SEC_CH_UA_PLATFORM != '"Windows"':
        _log("[FAIL]", f"TC-03.4 — SEC_CH_UA_PLATFORM != Windows: {uap.SEC_CH_UA_PLATFORM}")
        sys.exit(1)
    _log("[PASS]", "TC-03.4 — sec-ch-ua-platform = Windows")

    # 5. CURL_IMPERSONATE_PRIMARY khớp Chrome major hoặc gần (curl_cffi support range)
    primary = uap.CURL_IMPERSONATE_PRIMARY
    m = re.match(r"chrome(\d+)", primary)
    if not m:
        _log("[FAIL]", f"TC-03.5 — impersonate không phải Chrome: {primary}")
        sys.exit(1)
    imp_major = int(m.group(1))
    if abs(int(major) - imp_major) > 10:
        _log("[FAIL]", f"TC-03.5 — Chrome major {major} cách xa impersonate {primary} (>10)")
        sys.exit(1)
    _log("[PASS]", f"TC-03.5 — impersonate {primary} gần Chrome {major}")

    # 6. request_phase USER_AGENT == WINDOWS_USER_AGENT
    if rp.USER_AGENT != uap.WINDOWS_USER_AGENT:
        _log("[FAIL]", f"TC-03.6 — request_phase.USER_AGENT khác WINDOWS_USER_AGENT")
        sys.exit(1)
    _log("[PASS]", "TC-03.6 — request_phase USER_AGENT đồng bộ")

    # 7. sentinel DEFAULT_UA == WINDOWS_USER_AGENT
    if sp.DEFAULT_UA != uap.WINDOWS_USER_AGENT:
        _log("[FAIL]", f"TC-03.7 — sentinel.DEFAULT_UA khác WINDOWS_USER_AGENT")
        sys.exit(1)
    _log("[PASS]", "TC-03.7 — sentinel DEFAULT_UA đồng bộ")

    # 8. _IMPERSONATE_CANDIDATES list bắt đầu primary
    if rp._IMPERSONATE_CANDIDATES[0] != primary:
        _log("[FAIL]", f"TC-03.8 — _IMPERSONATE_CANDIDATES[0]={rp._IMPERSONATE_CANDIDATES[0]} != primary={primary}")
        sys.exit(1)
    _log("[PASS]", f"TC-03.8 — _IMPERSONATE_CANDIDATES = {rp._IMPERSONATE_CANDIDATES}")

    # 9. SignupRequest default = Windows Chrome
    req = md.SignupRequest(email="x@y.z")
    if req.user_agent != uap.WINDOWS_USER_AGENT:
        _log("[FAIL]", f"TC-03.9 — SignupRequest.user_agent default sai: {req.user_agent}")
        sys.exit(1)
    if req.impersonate != primary:
        _log("[FAIL]", f"TC-03.9 — SignupRequest.impersonate default sai: {req.impersonate}")
        sys.exit(1)
    _log("[PASS]", "TC-03.9 — SignupRequest defaults đồng bộ")


# ─── curl_cffi support check ──────────────────────────────────────────

def step_curl_cffi_support() -> None:
    _log("[STEP]", "TC-04 — curl_cffi BrowserType có chứa các impersonate token")
    try:
        from curl_cffi.requests.impersonate import BrowserType
    except Exception as exc:
        _log("[FAIL]", f"TC-04 — không import được BrowserType: {exc}")
        sys.exit(1)

    available = {b.value for b in BrowserType}
    from gpt_signup_hybrid_new import user_agent_profile as uap
    candidates = list(uap.CURL_IMPERSONATE_CANDIDATES)
    missing = [c for c in candidates if c not in available]
    if missing:
        _log("[FAIL]", f"TC-04 — curl_cffi 0.x không support: {missing} (available chrome*: "
                       f"{sorted(x for x in available if x.startswith('chrome'))})")
        sys.exit(1)
    _log("[PASS]", f"TC-04 — tất cả {candidates} đều có trong curl_cffi BrowserType")


# ─── sentinel navigator payload ───────────────────────────────────────

def step_sentinel_payload() -> None:
    _log("[STEP]", "TC-05 — sentinel_navigator_payload chứa user_agent + Client Hints")
    from gpt_signup_hybrid_new import user_agent_profile as uap
    p = uap.sentinel_navigator_payload()
    required = {
        "user_agent", "language", "languages", "hardware_concurrency",
        "sec_ch_ua_brands", "sec_ch_ua_mobile", "sec_ch_ua_platform",
        "sec_ch_ua_platform_version",
    }
    missing = required - set(p.keys())
    if missing:
        _log("[FAIL]", f"TC-05 — payload thiếu key: {missing}")
        sys.exit(1)
    if "Windows" not in p["user_agent"]:
        _log("[FAIL]", f"TC-05 — payload.user_agent không phải Windows: {p['user_agent']}")
        sys.exit(1)
    brands = p["sec_ch_ua_brands"]
    if not isinstance(brands, list) or len(brands) != 3:
        _log("[FAIL]", f"TC-05 — brands phải là list 3 phần tử, got: {brands}")
        sys.exit(1)
    brand_names = {b["brand"] for b in brands}
    if "Chromium" not in brand_names or "Google Chrome" not in brand_names:
        _log("[FAIL]", f"TC-05 — brands thiếu Chromium/Google Chrome: {brand_names}")
        sys.exit(1)
    if p["sec_ch_ua_platform"] != "Windows":
        _log("[FAIL]", f"TC-05 — sec_ch_ua_platform != Windows: {p['sec_ch_ua_platform']}")
        sys.exit(1)
    _log("[PASS]", f"TC-05 — payload OK ({len(p)} keys, brands={brand_names})")


# ─── sec-ch-ua brand parser round-trip ────────────────────────────────

def step_sec_ch_ua_brand_parse() -> None:
    _log("[STEP]", "TC-07 — _parse_brands_from_sec_ch_ua round-trip")
    from gpt_signup_hybrid_new import user_agent_profile as uap

    parsed = uap._parse_brands_from_sec_ch_ua(uap.SEC_CH_UA)
    if len(parsed) != 3:
        _log("[FAIL]", f"TC-07 — parse expect 3 brand, got {len(parsed)}: {parsed}")
        sys.exit(1)
    # Mỗi brand phải có cả "brand" và "version" non-empty
    for idx, b in enumerate(parsed):
        if not b.get("brand") or not b.get("version"):
            _log("[FAIL]", f"TC-07.{idx} — brand entry malformed: {b}")
            sys.exit(1)
    chrome_entry = next((b for b in parsed if b["brand"] == "Google Chrome"), None)
    if chrome_entry is None or chrome_entry["version"] != uap.CHROME_MAJOR:
        _log("[FAIL]", f"TC-07 — Google Chrome brand version mismatch: {chrome_entry}")
        sys.exit(1)
    _log("[PASS]", f"TC-07 — brand parse OK: {parsed}")


# ─── sentinel_quickjs.js có expose userAgentData ──────────────────────

def step_sentinel_js_userAgentData() -> None:
    _log("[STEP]", "TC-08 — openai_sentinel_quickjs.js expose userAgentData + getHighEntropyValues")
    src = (REPO_ROOT / "openai_sentinel_quickjs.js").read_text(encoding="utf-8")
    required_markers = [
        "userAgentData",
        "getHighEntropyValues",
        "sec_ch_ua_brands",
        "platformVersion",
    ]
    missing = [m for m in required_markers if m not in src]
    if missing:
        _log("[FAIL]", f"TC-08 — JS file thiếu marker: {missing}")
        sys.exit(1)
    _log("[PASS]", f"TC-08 — JS file đầy đủ Client Hints API ({len(required_markers)} markers)")


# ─── UPI flow audit: không còn UA Mac/Firefox hardcode + impersonate đồng bộ ─

UPI_FILES = [
    "pay_upi_http.py",
    "payment_link.py",
    "stripe_token.py",
    "record_pay_upi.py",
    "web/upi_runner.py",
]

# Patterns FORBIDDEN trong UPI source (UA Mac Firefox cũ + impersonate hardcode)
UPI_FORBIDDEN_PATTERNS = [
    ('Mozilla/5.0 (Macintosh', 'UA Mac không được phép trong UPI flow'),
    ('Gecko/20100101 Firefox', 'UA Firefox không được phép trong UPI flow'),
    ('Chrome/148.0', 'UA Chrome 148 hardcode (cũ) không được phép'),
    ('impersonate="chrome136"', 'impersonate hardcode chrome136 phải đổi sang _IMPERSONATE'),
    ('impersonate="firefox', 'impersonate firefox không được phép'),
]


def step_upi_no_legacy_hardcode() -> None:
    _log("[STEP]", "TC-09 — UPI flow không còn UA/impersonate hardcode cũ")
    for idx, fname in enumerate(UPI_FILES, 1):
        src = (REPO_ROOT / fname).read_text(encoding="utf-8")
        for pat, reason in UPI_FORBIDDEN_PATTERNS:
            if pat in src:
                _log("[FAIL]", f"TC-09.{idx} — {fname}: {reason} (found {pat!r})")
                sys.exit(1)
        _log("[PASS]", f"TC-09.{idx} — {fname} sạch hardcode cũ")


def step_upi_imports_user_agent_profile() -> None:
    _log("[STEP]", "TC-10 — UPI flow import user_agent_profile")
    expected_imports = {
        "pay_upi_http.py": "from user_agent_profile import",
        "payment_link.py": "from user_agent_profile import",
        "stripe_token.py": "from user_agent_profile import",
        "record_pay_upi.py": "from user_agent_profile import",
        "web/upi_runner.py": "from user_agent_profile import",
    }
    for idx, (fname, marker) in enumerate(expected_imports.items(), 1):
        src = (REPO_ROOT / fname).read_text(encoding="utf-8")
        if marker not in src:
            _log("[FAIL]", f"TC-10.{idx} — {fname} chưa import user_agent_profile")
            sys.exit(1)
        _log("[PASS]", f"TC-10.{idx} — {fname} đã wire profile")


def step_upi_sec_ch_ua_present() -> None:
    _log("[STEP]", "TC-11 — UPI request headers có sec-ch-ua family")
    # Đếm số lần "sec-ch-ua" xuất hiện ở mỗi file (mỗi headers dict thật phải có 3 dòng)
    expectations = {
        "pay_upi_http.py": 5,        # 5 endpoint groups (chatgpt checkout/approve + 3 stripe)
        "web/upi_runner.py": 5,      # mirror pay_upi_http
        "payment_link.py": 1,        # 1 headers dict gốc (các call khác kế thừa)
        "stripe_token.py": 1,        # 1 common_headers
    }
    for idx, (fname, min_count) in enumerate(expectations.items(), 1):
        src = (REPO_ROOT / fname).read_text(encoding="utf-8")
        count = src.count('"sec-ch-ua":')
        if count < min_count:
            _log("[FAIL]", f"TC-11.{idx} — {fname}: cần ≥{min_count} 'sec-ch-ua', got {count}")
            sys.exit(1)
        _log("[PASS]", f"TC-11.{idx} — {fname} có {count} sec-ch-ua header (≥{min_count})")


# ─── Browser Camoufox os param ────────────────────────────────────────

def step_camoufox_os_pinned() -> None:
    _log("[STEP]", "TC-06 — Camoufox launch truyền os=['windows']")
    src1 = (REPO_ROOT / "browser_phase.py").read_text(encoding="utf-8")
    src2 = (REPO_ROOT / "session_phase.py").read_text(encoding="utf-8")

    # Cả 2 file phải có os=list(_CAMOUFOX_OS) trong AsyncCamoufox call
    if "os=list(_CAMOUFOX_OS)" not in src1:
        _log("[FAIL]", "TC-06.1 — browser_phase.py chưa pin Camoufox os")
        sys.exit(1)
    _log("[PASS]", "TC-06.1 — browser_phase.py pin os=windows")

    if "os=list(_CAMOUFOX_OS)" not in src2:
        _log("[FAIL]", "TC-06.2 — session_phase.py chưa pin Camoufox os")
        sys.exit(1)
    _log("[PASS]", "TC-06.2 — session_phase.py pin os=windows")


def main() -> None:
    print("=" * 60, flush=True)
    print(f"User-Agent refactor (Option A) — verify @ {REPO_ROOT}", flush=True)
    print("=" * 60, flush=True)
    step_syntax_parse()
    step_import_modules()
    step_constant_consistency()
    step_curl_cffi_support()
    step_sentinel_payload()
    step_camoufox_os_pinned()
    step_sec_ch_ua_brand_parse()
    step_sentinel_js_userAgentData()
    step_upi_no_legacy_hardcode()
    step_upi_imports_user_agent_profile()
    step_upi_sec_ch_ua_present()
    print("=" * 60, flush=True)
    print("[DONE] Tất cả check PASS — Option A hoàn tất.", flush=True)


if __name__ == "__main__":
    main()
