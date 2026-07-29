"""Factory build ``FirefoxProfile`` + ``CamoufoxTokenGenerator`` từ ``SignupRequest``.

Camoufox bản chất là Firefox hardened. Mode ``hybrid`` cố tình ép toàn bộ stack về
Firefox 135 trên macOS (giống capture HAR Camoufox gốc — golden record của package
chatgpt_camoufox). Persona "chrome_win" của repo (CHROME_145_WIN) không khả dụng
cho hybrid vì curl_cffi impersonate Chrome + sdk.js trong Camoufox Firefox sẽ tự
mâu thuẫn nhau (UA/sec-ch-ua mismatch sentinel).

Logic chọn locale:
  - request.locale set explicit → dùng nguyên (vd "en-IN", "vi-VN").
  - request.locale None → fallback "en-US" (an toàn — không lộ Vietnamese
    Accept-Language khi proxy ở US).

Captcha: YesCaptcha key đọc từ env ``YESCAPTCHA_KEY``. Không hardcode trong
Settings Store (anti-secret-leak); chỉ kích hoạt khi authorize trả 403.
"""
from __future__ import annotations

import os
import random
from typing import Any

from models import SignupRequest


# Firefox major mặc định cho Camoufox (giữ đồng bộ với
# ``chatgpt_camoufox.fingerprint.profile_for_locale`` default = 135).
_DEFAULT_FIREFOX_MAJOR = 135

# Platform default — KHỚP CHÍNH XÁC golden record của ``chatgpt_camoufox``
# (xem chatgpt_camoufox/__main__.py argparse `--platform` default="Windows" +
# fingerprint.FirefoxProfile.platform default="Windows"). Camoufox sẽ launch
# với `os="windows"`, UA HTTP gửi "Windows NT 10.0; Win64; x64", sentinel sdk.js
# navigator.platform = "Win32" — tất cả khớp nhau.
#
# KHÔNG đổi sang macOS (dù repo có persona FIREFOX_135_MAC riêng) — hybrid mode
# cố tình bám sát chatgpt_camoufox golden để hưởng nguyên test coverage của
# package đó. Muốn macOS → dùng mode ``browser`` với persona ``firefox_mac``.
_DEFAULT_PLATFORM = "Windows"

# Locale default — **en-US** (neutral, đa số người dùng OpenAI). Lưu ý: chatgpt_camoufox
# CLI default ``vi-VN`` (preference của dev gốc), nhưng đó KHÔNG phải golden
# anti-ban — locale phải khớp proxy country, mà repo này dùng pool proxy đa quốc gia
# (US/IN/VN). en-US là fallback an toàn nhất vì:
#   - Generic (proxy IP US/EU/IN đều không bị flag)
#   - Accept-Language `en-US,en;q=0.5` là default Firefox vanilla
#   - timezone UTC-7 (PDT) phổ biến cho Firefox Windows ở Mỹ
# Caller muốn locale khác → set ``request.locale`` (vd "en-IN" khi proxy India,
# "vi-VN" khi proxy VN). Khi ``reg.locale_auto_geo`` bật, browser_phase tự pick
# locale theo proxy country — hybrid hiện tại CHƯA auto-detect (TODO Phase 2).
_DEFAULT_LOCALE = "en-US"


def build_firefox_profile(request: SignupRequest) -> Any:
    """Build ``chatgpt_camoufox.FirefoxProfile`` từ request.

    Args:
        request: SignupRequest đã được runtime resolve (proxy/locale/...).

    Returns:
        FirefoxProfile instance (hoãn import để smoke test có thể import module
        ngay cả khi chưa cài curl_cffi/camoufox).
    """
    from chatgpt_camoufox.chatgpt_camoufox.fingerprint import profile_for_locale

    locale = (request.locale or _DEFAULT_LOCALE).strip() or _DEFAULT_LOCALE
    return profile_for_locale(
        locale=locale,
        firefox_major=_DEFAULT_FIREFOX_MAJOR,
        platform=_DEFAULT_PLATFORM,
        rng=random.Random(),
    )


def build_token_generator(
    request: SignupRequest, *, profile: Any,
) -> Any:
    """Build token generator — pool reuse Camoufox nếu enabled.

    Args:
        profile: FirefoxProfile từ ``build_firefox_profile`` (chỉ dùng khi
            fallback no-pool path — pool tự handle launch options).

    Returns:
        CamoufoxTokenGenerator golden bọc ``_NoPoolThreadAffinityWrapper``
        (**default** — no-pool, mỗi signup browser riêng) hoặc
        HybridContextHandle (khi pool được OPT-IN qua Settings Store
        ``reg.hybrid_pool_enabled=True``). Cả 2 implement cùng interface, caller
        không cần phân biệt.

    Pool tradeoff (OPT-IN, KHÔNG còn là default):
        - **No-pool (default)**: mỗi signup launch ``CamoufoxTokenGenerator``
          golden riêng + close trong finally — KHỚP lifecycle golden
          (``__main__.run_line``), tránh cluster fingerprint giữa account và
          tránh hang do single-thread serialization của pool. Đánh đổi: cold
          launch ~5-10s/signup.
        - **Pool (opt-in)**: launch Camoufox 1 lần xuyên N signup cùng config,
          mỗi signup chỉ trả phí new_context() + page.goto(frame.html) + sdk.js
          load ~2-3s. CHỈ bật khi đã xác nhận fingerprint per-context đủ khác
          và pool ổn định (không hang). Bật qua Settings Store key
          ``reg.hybrid_pool_enabled``; env ``HYBRID_POOL_DISABLED=1`` override
          cứng về no-pool.
    """
    from .browser_pool import (
        _NoPoolThreadAffinityWrapper, get_pool, pool_enabled,
    )

    if not pool_enabled():
        # DEFAULT no-pool: launch Camoufox riêng mỗi signup (khớp golden). Bọc
        # qua ``_NoPoolThreadAffinityWrapper`` để route mọi sync Playwright op
        # qua 1 dedicated thread — bắt buộc vì caller (run_hybrid_signup) là
        # async coroutine + pre-mint thread chạy song song (cả 2 phải share
        # cùng 1 thread Camoufox, không thì Playwright sync API fail).
        from chatgpt_camoufox.chatgpt_camoufox.camoufox_vm import CamoufoxTokenGenerator
        inner = CamoufoxTokenGenerator(
            profile=profile,
            proxy=request.proxy,
            headless=bool(request.headless),
            insecure=bool(request.tls_insecure),
        )
        return _NoPoolThreadAffinityWrapper(inner)

    # Pool path (OPT-IN): acquire BrowserContext isolated từ shared Camoufox.
    pool = get_pool()
    return pool.acquire(
        proxy=request.proxy,
        headless=bool(request.headless),
        insecure=bool(request.tls_insecure),
        log=lambda msg: None,  # Pool logs riêng; runner sẽ log timing.
    )


def build_curl_session(request: SignupRequest, *, profile: Any) -> Any:
    """Build curl_cffi Session impersonate Firefox + apply proxy + TLS verify.

    Args:
        profile: FirefoxProfile (lấy impersonate token = "firefox{N}").

    Returns:
        curl_cffi.requests.Session instance đã set proxy + TLS verify + impersonate.
    """
    from chatgpt_camoufox.chatgpt_camoufox.client import make_session

    session = make_session(profile, verify=not request.tls_insecure)
    if request.proxy:
        session.proxies = {"http": request.proxy, "https": request.proxy}
    return session


def build_captcha(log) -> Any | None:
    """Build YesCaptcha client nếu env ``YESCAPTCHA_KEY`` được set, else None.

    Anti-secret-leak: KHÔNG đọc từ Settings Store DB (secret chỉ ở env). Khi
    không set → trả None, Relay sẽ propagate Cloudflare 403 thành lỗi luôn
    (KHÔNG bypass im lặng).
    """
    key = os.environ.get("YESCAPTCHA_KEY", "").strip()
    if not key:
        return None
    from chatgpt_camoufox.chatgpt_camoufox.captcha import YesCaptchaClient

    log("[hybrid] YESCAPTCHA_KEY detected — Cloudflare authorize 403 sẽ được auto-solve")
    return YesCaptchaClient(client_key=key)
