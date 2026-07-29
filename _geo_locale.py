"""Map proxy IP country → locale + timezone + geolocation cho Camoufox.

Anti-ban (journal 260625-1224 Task 1.4 + bug C-prev):
    Trace tay xác nhận server OpenAI Sentinel cross-check IP country (proxy
    exit) ↔ navigator.language ↔ timezone ↔ geolocation. Hardcode
    `locale="en-US"` trong khi proxy là IP India = mismatch = cờ đỏ.

Module này:
    1. ``lookup_proxy_country(proxy)`` — probe ipinfo.io qua proxy → ISO 3166-1
       alpha-2 country code (vd "IN", "US"). Cache theo proxy URL.
    2. ``locale_for_country(cc)`` — map country code → (locale, timezone, geo).
    3. ``resolve_proxy_locale(proxy)`` — high-level helper trả tuple đầy đủ.

Strategy "fail-safe":
    - proxy=None → default US (KHÔNG raise).
    - lookup fail (network/timeout) → log warning + default US (KHÔNG raise).
    - country chưa map → default US.
    Caller có thể override qua ``request.locale`` (CLI explicit > auto-detect).
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

# (country_code → (locale, timezone, (lat, lon))) — top countries cho REG.
# Mở rộng khi có proxy mới. Locale theo BCP-47 + IANA timezone + geo center
# (capital/largest city, dùng làm Camoufox geolocation).
_COUNTRY_LOCALE_MAP: dict[str, tuple[str, str, tuple[float, float]]] = {
    "IN": ("en-IN", "Asia/Kolkata", (28.6139, 77.2090)),         # New Delhi
    "US": ("en-US", "America/New_York", (40.7128, -74.0060)),    # NYC
    "GB": ("en-GB", "Europe/London", (51.5074, -0.1278)),        # London
    "AU": ("en-AU", "Australia/Sydney", (-33.8688, 151.2093)),
    "CA": ("en-CA", "America/Toronto", (43.6532, -79.3832)),
    "DE": ("de-DE", "Europe/Berlin", (52.5200, 13.4050)),
    "FR": ("fr-FR", "Europe/Paris", (48.8566, 2.3522)),
    "JP": ("ja-JP", "Asia/Tokyo", (35.6762, 139.6503)),
    "BR": ("pt-BR", "America/Sao_Paulo", (-23.5505, -46.6333)),
    "ID": ("id-ID", "Asia/Jakarta", (-6.2088, 106.8456)),
    "SG": ("en-SG", "Asia/Singapore", (1.3521, 103.8198)),
    "PH": ("en-PH", "Asia/Manila", (14.5995, 120.9842)),
    "VN": ("vi-VN", "Asia/Ho_Chi_Minh", (10.8231, 106.6297)),
    "TH": ("th-TH", "Asia/Bangkok", (13.7563, 100.5018)),
    "MY": ("en-MY", "Asia/Kuala_Lumpur", (3.1390, 101.6869)),
}

_DEFAULT_LOCALE = "en-US"
_DEFAULT_TZ = "America/New_York"
_DEFAULT_GEO = (40.7128, -74.0060)
_DEFAULT_TUPLE: tuple[str, str, tuple[float, float]] = (
    _DEFAULT_LOCALE, _DEFAULT_TZ, _DEFAULT_GEO,
)

# Cache: proxy URL → country_code (hoặc None khi lookup fail). TTL = process
# lifetime (REG runtime ngắn, không cần TTL phức tạp; restart process nếu
# muốn re-lookup).
_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, str | None] = {}


def lookup_proxy_country(
    proxy: str | None,
    *,
    timeout: float = 10.0,
    log: Any = None,
) -> str | None:
    """Probe IP country của proxy bằng GET https://ipinfo.io/json.

    Args:
        proxy: HTTP/HTTPS/SOCKS proxy URL. None → return None.
        timeout: timeout HTTP request (seconds).
        log: optional callable(msg) cho diagnostic; None = dùng logger.

    Returns:
        ISO 3166-1 alpha-2 country code (vd "IN") hoặc None.
    """
    if not proxy:
        return None
    with _CACHE_LOCK:
        if proxy in _CACHE:
            return _CACHE[proxy]

    _log = log or (lambda m: logger.info(m))
    cc: str | None = None
    try:
        from curl_cffi import requests as _curl
        sess = _curl.Session(impersonate="chrome145")
        sess.trust_env = False
        # Normalize socks5 → socks5h (resolve DNS qua proxy, tránh leak DNS).
        normalized = proxy
        if proxy.startswith("socks5://"):
            normalized = "socks5h://" + proxy[len("socks5://"):]
        sess.proxies = {"https": normalized, "http": normalized}
        try:
            resp = sess.get("https://ipinfo.io/json", timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                cc = (data.get("country") or "").upper().strip() or None
                ip = data.get("ip", "?")
                _log(f"[geo-locale] proxy IP={ip} country={cc or '?'}")
            else:
                _log(f"[geo-locale] ipinfo.io HTTP {resp.status_code}")
        finally:
            try:
                sess.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001 — fail-safe: lookup không được phá flow
        _log(f"[geo-locale] proxy country lookup failed: {type(exc).__name__}: {exc}")
        cc = None

    with _CACHE_LOCK:
        _CACHE[proxy] = cc
    return cc


def locale_for_country(cc: str | None) -> tuple[str, str, tuple[float, float]]:
    """Map ISO 3166-1 alpha-2 country code → (locale, timezone, (lat, lon)).

    Country chưa có trong map (hoặc cc=None) → default US.
    """
    if not cc:
        return _DEFAULT_TUPLE
    return _COUNTRY_LOCALE_MAP.get(cc.upper(), _DEFAULT_TUPLE)


def resolve_proxy_locale(
    proxy: str | None,
    *,
    timeout: float = 10.0,
    log: Any = None,
) -> tuple[str, str, tuple[float, float], str | None]:
    """High-level: proxy URL → (locale, timezone, (lat, lon), country_code).

    proxy=None hoặc lookup fail → defaults (en-US/America/New_York/NYC).
    country_code có thể None khi default được dùng — caller log để debug.
    """
    cc = lookup_proxy_country(proxy, timeout=timeout, log=log)
    locale, tz, geo = locale_for_country(cc)
    return locale, tz, geo, cc


def clear_cache() -> None:
    """Clear in-process cache. Test/admin chỉ — production không cần."""
    with _CACHE_LOCK:
        _CACHE.clear()


__all__ = [
    "lookup_proxy_country",
    "locale_for_country",
    "resolve_proxy_locale",
    "clear_cache",
]
