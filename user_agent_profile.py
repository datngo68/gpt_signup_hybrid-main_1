"""Browser persona — single source of truth cho fingerprint stack mọi flow.

Anti-ban (journal 260625-1224 Task 3.1 + bug H5):
    Trace tay (Camoufox = Firefox 135 Mac) cho thấy server cross-check:
        HTTP UA  ↔  TLS fingerprint (JA3/JA4)  ↔
        Sentinel proof body (decoded chứa UA)  ↔
        navigator.userAgent trong DOM
    Tất cả 4 phải KHỚP nhau, nếu không → cờ đỏ.

    Code cũ ép tất cả về Chrome 145 Windows (kể cả khi Camoufox = Firefox).
    Sentinel proof chứa "Chrome" trong khi HTTP UA Firefox = lệch → ban.

Module này expose:
    - ``BrowserPersona`` dataclass — đóng gói full fingerprint stack.
    - ``CHROME_145_WIN`` — Chrome desktop Windows (cho curl_cffi pure_request login).
    - ``FIREFOX_135_MAC`` — Firefox desktop Mac (cho REG Camoufox + signup browser).
    - ``get_persona(name)`` — lookup theo name (settings ``reg.persona``).
    - Top-level constants — backward compat alias trỏ vào CHROME_145_WIN
      (callers cũ không break).
"""
from __future__ import annotations

from dataclasses import dataclass


# ─────────────────────────────────────────────────────────────────────
# Helper — parse sec-ch-ua header
# ─────────────────────────────────────────────────────────────────────


def _parse_brands_from_sec_ch_ua(value: str) -> list[dict[str, str]]:
    """Parse ``"Brand";v="x", "Brand2";v="y"`` thành list ``[{brand, version}]``.

    Dùng cho navigator.userAgentData.brands trong sdk.js context.
    """
    out: list[dict[str, str]] = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            brand_part, ver_part = chunk.split(";", 1)
            brand = brand_part.strip().strip('"')
            ver = ver_part.split("=", 1)[1].strip().strip('"')
            out.append({"brand": brand, "version": ver})
        except (ValueError, IndexError):
            continue
    return out


# ─────────────────────────────────────────────────────────────────────
# BrowserPersona — fingerprint stack đóng gói
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BrowserPersona:
    """Đóng gói full fingerprint stack của 1 browser persona.

    Mọi field phải nội tại consistent — vd persona Firefox phải có sec_ch_ua=None
    (Firefox KHÔNG gửi Client Hints), accept_language q=0.5 (Firefox style), v.v.

    Public API:
        - ``user_agent`` / ``sec_ch_ua`` / ``camoufox_os`` — top-level fingerprint
        - ``navigator_payload()`` — dict cho ``openai_sentinel_quickjs.js``
        - ``common_headers()`` — dict header tối thiểu cho HTTP request
        - ``curl_impersonate_candidates`` — list impersonate token rotation
    """
    name: str
    user_agent: str

    # Client Hints (Chrome only — Firefox = None)
    sec_ch_ua: str | None
    sec_ch_ua_mobile: str | None
    sec_ch_ua_platform: str | None
    sec_ch_ua_platform_version: str | None

    # Common headers
    accept_language: str   # Chrome: "en-US,en;q=0.9", Firefox: "en-US,en;q=0.5"
    accept_encoding: str   # cả 2 đều "gzip, deflate, br, zstd"

    # Camoufox/Playwright launch
    camoufox_os: tuple[str, ...]   # ("windows",) hoặc ("mac",)

    # curl_cffi TLS fingerprint
    curl_impersonate_primary: str
    curl_impersonate_fallback: tuple[str, ...]

    # Sentinel sdk.js navigator payload
    navigator_language: str
    navigator_languages: tuple[str, ...]
    hardware_concurrency: int
    device_memory: int
    arch: str       # "x86" cho Win, "arm" cho Mac silicon
    bitness: str    # "64"

    @property
    def curl_impersonate_candidates(self) -> tuple[str, ...]:
        """Rotation chain: primary → fallback."""
        return (self.curl_impersonate_primary, *self.curl_impersonate_fallback)

    def common_headers(self, *, referer: str = "https://chatgpt.com/") -> dict[str, str]:
        """Header tối thiểu cho HTTP request từ persona này.

        Chrome: gửi đủ User-Agent + Accept-Language + 3 sec-ch-ua headers.
        Firefox: chỉ User-Agent + Accept-Language (no Client Hints — đặc trưng).
        """
        headers: dict[str, str] = {
            "User-Agent": self.user_agent,
            "Accept-Language": self.accept_language,
            "Accept-Encoding": self.accept_encoding,
            "Referer": referer,
        }
        if self.sec_ch_ua:
            # Chrome family — gửi sec-ch-ua* low-entropy hints
            headers["sec-ch-ua"] = self.sec_ch_ua
            if self.sec_ch_ua_mobile:
                headers["sec-ch-ua-mobile"] = self.sec_ch_ua_mobile
            if self.sec_ch_ua_platform:
                headers["sec-ch-ua-platform"] = self.sec_ch_ua_platform
        return headers

    def navigator_payload(self) -> dict[str, object]:
        """Navigator payload pass cho ``openai_sentinel_quickjs.js`` (sdk.js).

        sdk.js đọc payload để build navigator + navigator.userAgentData. Nếu
        persona = Firefox, navigator.userAgentData = undefined (giống Firefox
        thật) — không inject brands/platform.
        """
        if self.sec_ch_ua:
            brands = _parse_brands_from_sec_ch_ua(self.sec_ch_ua)
            mobile = self.sec_ch_ua_mobile == "?1"
            platform = (self.sec_ch_ua_platform or "").strip('"')
            platform_version = (self.sec_ch_ua_platform_version or "").strip('"')
        else:
            brands = []
            mobile = False
            platform = ""
            platform_version = ""

        return {
            "user_agent": self.user_agent,
            "language": self.navigator_language,
            "languages": list(self.navigator_languages),
            "hardware_concurrency": self.hardware_concurrency,
            "device_memory": self.device_memory,
            # Client Hints — Chrome có, Firefox không (sdk.js sẽ thấy
            # navigator.userAgentData=undefined nếu brands=[]).
            "sec_ch_ua_brands": brands,
            "sec_ch_ua_mobile": mobile,
            "sec_ch_ua_platform": platform,
            "sec_ch_ua_platform_version": platform_version,
            "sec_ch_ua_arch": self.arch,
            "sec_ch_ua_bitness": self.bitness,
            "sec_ch_ua_model": "",
        }


# ─────────────────────────────────────────────────────────────────────
# Persona instances — KHÔNG hardcode bên ngoài file này
# ─────────────────────────────────────────────────────────────────────

# Chrome 145 Windows desktop — default cho curl_cffi pure_request flow
# (login, get_session). HTTP UA + TLS impersonate đều Chrome → consistent.
CHROME_145_WIN = BrowserPersona(
    name="chrome_win",
    user_agent=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    ),
    sec_ch_ua=(
        '"Chromium";v="145", '
        '"Google Chrome";v="145", '
        '"Not_A Brand";v="24"'
    ),
    sec_ch_ua_mobile="?0",
    sec_ch_ua_platform='"Windows"',
    sec_ch_ua_platform_version='"15.0.0"',
    accept_language="en-US,en;q=0.9",
    accept_encoding="gzip, deflate, br, zstd",
    camoufox_os=("windows",),
    curl_impersonate_primary="chrome145",
    curl_impersonate_fallback=("chrome142", "chrome136"),
    navigator_language="en-US",
    navigator_languages=("en-US", "en"),
    hardware_concurrency=12,
    device_memory=8,
    arch="x86",
    bitness="64",
)


# Firefox 135 Mac desktop — default cho REG signup flow (Camoufox = Firefox).
# HTTP UA Firefox + TLS Firefox (Camoufox tự handle) + Sentinel payload Firefox
# → tất cả KHỚP. Trace tay xác nhận persona này là default thực tế của Camoufox.
FIREFOX_135_MAC = BrowserPersona(
    name="firefox_mac",
    user_agent=(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:135.0) "
        "Gecko/20100101 Firefox/135.0"
    ),
    sec_ch_ua=None,             # Firefox KHÔNG gửi Client Hints — đặc trưng quan trọng
    sec_ch_ua_mobile=None,
    sec_ch_ua_platform=None,
    sec_ch_ua_platform_version=None,
    accept_language="en-US,en;q=0.5",   # Firefox q-value 0.5 (Chrome dùng 0.9)
    accept_encoding="gzip, deflate, br, zstd",
    camoufox_os=("mac",),
    # NOTE: curl_cffi có thể không hỗ trợ firefox impersonate cho version mới.
    # REG flow (Camoufox) KHÔNG dùng curl_impersonate — sentinel iframe trong page
    # tự handle TLS. Chỉ dùng nếu pure_request bridge sang Firefox persona (Phase 6).
    curl_impersonate_primary="firefox135",
    curl_impersonate_fallback=("firefox133", "firefox120"),
    navigator_language="en-US",
    navigator_languages=("en-US", "en"),
    hardware_concurrency=10,    # Mac M1/M2 8c/10c phổ biến
    device_memory=8,
    arch="arm",                  # Mac silicon
    bitness="64",
)


_PERSONA_REGISTRY: dict[str, BrowserPersona] = {
    p.name: p for p in (CHROME_145_WIN, FIREFOX_135_MAC)
}


def get_persona(name: str) -> BrowserPersona:
    """Lookup persona theo name (vd "firefox_mac"). Raise nếu unknown.

    Caller pattern:
        persona = get_persona(settings.get("reg.persona") or "firefox_mac")
    """
    p = _PERSONA_REGISTRY.get(name)
    if p is None:
        raise ValueError(
            f"unknown persona: {name!r}. Available: {sorted(_PERSONA_REGISTRY)}"
        )
    return p


# ─────────────────────────────────────────────────────────────────────
# Backward-compatible top-level constants (default = CHROME_145_WIN)
#
# Caller cũ (request_phase.py, sentinel_*.py, session_phase.py) import constant
# trực tiếp. Giữ alias để KHÔNG break — mặc định Chrome (vì pure_request +
# get_session dùng curl_cffi Chrome impersonate). REG flow nên dùng
# ``get_persona("firefox_mac")`` trực tiếp.
# ─────────────────────────────────────────────────────────────────────

CHROME_MAJOR = "145"
CHROME_FULL = f"{CHROME_MAJOR}.0.0.0"

WINDOWS_USER_AGENT = CHROME_145_WIN.user_agent
SEC_CH_UA = CHROME_145_WIN.sec_ch_ua or ""
SEC_CH_UA_MOBILE = CHROME_145_WIN.sec_ch_ua_mobile or ""
SEC_CH_UA_PLATFORM = CHROME_145_WIN.sec_ch_ua_platform or ""
SEC_CH_UA_PLATFORM_VERSION = CHROME_145_WIN.sec_ch_ua_platform_version or ""

CURL_IMPERSONATE_PRIMARY = CHROME_145_WIN.curl_impersonate_primary
CURL_IMPERSONATE_FALLBACK = CHROME_145_WIN.curl_impersonate_fallback
CURL_IMPERSONATE_CANDIDATES = CHROME_145_WIN.curl_impersonate_candidates

CAMOUFOX_OS = CHROME_145_WIN.camoufox_os

NAVIGATOR_LANGUAGE = CHROME_145_WIN.navigator_language
NAVIGATOR_LANGUAGES = CHROME_145_WIN.navigator_languages
HARDWARE_CONCURRENCY = CHROME_145_WIN.hardware_concurrency
DEVICE_MEMORY_GB = CHROME_145_WIN.device_memory


# ─────────────────────────────────────────────────────────────────────
# Helper functions — backward compat
# ─────────────────────────────────────────────────────────────────────


def common_chrome_headers(*, referer: str = "https://chatgpt.com/") -> dict[str, str]:
    """Chrome desktop common headers (alias cho ``CHROME_145_WIN.common_headers``)."""
    return CHROME_145_WIN.common_headers(referer=referer)


def sentinel_navigator_payload(
    persona: BrowserPersona | None = None,
) -> dict[str, object]:
    """Sentinel sdk.js navigator payload.

    Args:
        persona: BrowserPersona instance. None = backward compat = CHROME_145_WIN.

    Caller mới nên pass persona explicit:
        from user_agent_profile import get_persona, sentinel_navigator_payload
        payload = sentinel_navigator_payload(get_persona("firefox_mac"))
    """
    p = persona or CHROME_145_WIN
    return p.navigator_payload()


__all__ = [
    # New public API
    "BrowserPersona",
    "CHROME_145_WIN",
    "FIREFOX_135_MAC",
    "get_persona",
    "sentinel_navigator_payload",
    "common_chrome_headers",
    # Backward compat constants
    "CHROME_MAJOR",
    "CHROME_FULL",
    "WINDOWS_USER_AGENT",
    "SEC_CH_UA",
    "SEC_CH_UA_MOBILE",
    "SEC_CH_UA_PLATFORM",
    "SEC_CH_UA_PLATFORM_VERSION",
    "CURL_IMPERSONATE_PRIMARY",
    "CURL_IMPERSONATE_FALLBACK",
    "CURL_IMPERSONATE_CANDIDATES",
    "CAMOUFOX_OS",
    "NAVIGATOR_LANGUAGE",
    "NAVIGATOR_LANGUAGES",
    "HARDWARE_CONCURRENCY",
    "DEVICE_MEMORY_GB",
]
