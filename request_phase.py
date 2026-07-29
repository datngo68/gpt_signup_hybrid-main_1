"""Pure-request registration phase — no browser required.

Implements the full OpenAI signup state machine via HTTP requests (curl_cffi):
  1. chatgpt.com CSRF + signin/openai → authorize URL
  2. OAuth init → device_id
  3. Sentinel token (QuickJS primary, Python PoW fallback)
  4. authorize/continue (email submission)
  5. register password
  6. OTP send → poll via existing mail providers → verify
  7. create_account (name + birthdate)
  8. Follow redirect chain → callback URL
  9. Consume callback → session_token + access_token

Adapts the protocol from github.com/Regert888/gpt-outlook-register to work
with the existing gpt_signup_hybrid mail providers and SignupRequest/SignupResult.

Public API:
    run_request_phase(request, mail_provider, log) -> SignupResult
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from curl_cffi import requests as curl_requests

from mail_providers import MailProvider
from models import SignupRequest, SignupResult
from user_agent_profile import (
    BrowserPersona as _BrowserPersona,
    CHROME_145_WIN as _DEFAULT_PERSONA,
    CURL_IMPERSONATE_CANDIDATES as _UA_IMPERSONATE_CANDIDATES,
    CURL_IMPERSONATE_PRIMARY as _UA_IMPERSONATE_PRIMARY,
    SEC_CH_UA,
    SEC_CH_UA_MOBILE,
    SEC_CH_UA_PLATFORM,
    WINDOWS_USER_AGENT,
)

logger = logging.getLogger(__name__)


class RequestPhaseError(Exception):
    """Pure-request registration failed."""


# ─── Constants ────────────────────────────────────────────────────────

# Re-export cho backward compatibility (session_phase + module khác đã import).
USER_AGENT = WINDOWS_USER_AGENT

_FIRST_NAMES = [
    "James", "John", "Robert", "Michael", "William", "David", "Richard",
    "Joseph", "Thomas", "Charles", "Mary", "Patricia", "Jennifer", "Linda",
    "Elizabeth", "Barbara", "Susan", "Jessica", "Sarah", "Karen",
]
_LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Wilson", "Anderson", "Taylor", "Thomas",
]


# ─── Datadog trace headers (critical for OTP delivery) ────────────────


def _datadog_trace_headers() -> dict[str, str]:
    """Generate Datadog APM trace headers.

    OpenAI frontend uses Datadog RUM — all real browser requests carry these.
    Missing headers cause silent OTP drop (200 OK but no email sent).
    """
    trace_id = str(random.getrandbits(64))
    parent_id = str(random.getrandbits(64))
    trace_hex = format(int(trace_id), "016x")
    parent_hex = format(int(parent_id), "016x")
    return {
        "traceparent": f"00-0000000000000000{trace_hex}-{parent_hex}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


# Cookies cần nhập từ sidecar Camoufox vào curl_cffi session để khớp browser.
# Ngoài __cf_bm/__cflb (Camoufox-Cloudflare set sẵn khi load chatgpt.com),
# 3 cookie sau chỉ tồn tại khi JS chạy:
#   - oai-sc      : Sentinel SDK init token  (anti-bot signal cốt lõi)
#   - _dd_s       : Datadog RUM session ID
#   - oai-asli    : auth_session_logging_id từ NextAuth
# Thêm các cookie session khác mà server cấp khi navigate /email-verification.
#
# Phase A.2 (chuẩn chatgpt_camoufox): THU HẸP allowlist — bỏ ``__cf_bm`` /
# ``__cflb`` / ``_cfuvid`` (Cloudflare bot-management bound to TLS session).
# Camoufox và curl_cffi có 2 TLS session khác nhau (JA3/JA4 khác), copy 3 cookie
# này từ Camoufox sang curl = anti-bot signal (server thấy cookie issued cho
# JA3_A nhưng request đến từ JA3_B → mismatch). Để curl_cffi TỰ NHẬN
# ``__cf_bm`` etc. từ response của chính nó (tự nhiên, khớp JA3 của curl).
# ``cf_clearance`` GIỮ vì đây là challenge response (browser-only, curl không
# tự kiếm được) — IP-bound nên đã có skip-nếu-proxy-khác bên dưới.
_SIDECAR_COOKIE_ALLOWLIST = frozenset({
    "oai-sc", "_dd_s", "oai-asli", "oai-did",
    "oaicom-stable-id",
    # Cloudflare clearance — challenge JS response, browser-only, curl không
    # tự kiếm được. IP-bound nên skip khi proxy khác (xem _CF_IP_BOUND_COOKIES).
    "cf_clearance",
})

# Cookies that are IP-bound (Cloudflare bot management). If sidecar's
# upstream proxy differs from caller's proxy, syncing these would send
# CF cookies issued for IP_A from IP_B → server detects mismatch and
# re-issues a challenge. When the proxies match (or both ``None``),
# syncing is fine and saves a round-trip.
#
# Phase A.2: chỉ còn ``cf_clearance`` (3 cookie ``__cf_bm``/``__cflb``/
# ``_cfuvid`` đã bị loại khỏi allowlist trên — luôn không sync, không cần
# check IP-bound nữa).
_CF_IP_BOUND_COOKIES = frozenset({
    "cf_clearance",
})


def _import_cookies_from_sidecar(
    session, sidecar, log: Callable, caller_proxy: str | None = None,
) -> int:
    """Dump cookies từ Camoufox sidecar → inject vào curl_cffi session jar.

    Returns: số cookie thực sự import. 0 = sidecar không trả gì.

    Chỉ import cookies trong allowlist để tránh nhập rác (perf-cookie,
    consent, ...) làm header Cookie phình to bất thường.

    IP-bound cookies (Cloudflare) are skipped when ``caller_proxy`` differs
    from the sidecar's proxy (``SIDECAR_SHARED_PROXY`` mode). Caller's
    curl session will fetch its own CF cookies via the prime GET to
    ``/auth/login`` instead.
    """
    cookies = sidecar.dump_cookies()
    if not cookies:
        return 0
    # Resolve the IP boundary: sidecar's proxy vs caller's proxy.
    sidecar_proxy = getattr(sidecar, "proxy", None)
    skip_cf = (sidecar_proxy or None) != (caller_proxy or None)
    if skip_cf:
        log(
            f"[request] sidecar proxy={sidecar_proxy or 'direct'} ≠ caller "
            f"proxy={caller_proxy or 'direct'} → skipping IP-bound CF cookies"
        )
    imported = 0
    skipped_cf = 0
    for c in cookies:
        name = (c.get("name") or "").strip()
        if name not in _SIDECAR_COOKIE_ALLOWLIST:
            continue
        if skip_cf and name in _CF_IP_BOUND_COOKIES:
            skipped_cf += 1
            continue
        value = c.get("value") or ""
        if not value:
            continue
        domain = (c.get("domain") or "").strip() or ".chatgpt.com"
        path = c.get("path") or "/"
        try:
            session.cookies.set(name, value, domain=domain, path=path)
            imported += 1
        except Exception as exc:  # noqa: BLE001
            log(f"[request] cookie {name} inject failed: {exc}")
    if imported or skipped_cf:
        log(
            f"[request] imported {imported} cookies from sidecar"
            + (f" (skipped {skipped_cf} CF cookies)" if skipped_cf else "")
        )
    return imported


# ─── Session factory ─────────────────────────────────────────────────


def _create_session(proxy: str | None, impersonate: str = _UA_IMPERSONATE_PRIMARY) -> curl_requests.Session:
    session = curl_requests.Session(impersonate=impersonate)
    session.trust_env = False
    if proxy:
        normalized = proxy
        if proxy.startswith("socks5://"):
            normalized = "socks5h://" + proxy[len("socks5://"):]
        session.proxies = {"https": normalized, "http": normalized}
    else:
        session.proxies = {"https": "", "http": ""}
    return session


# TLS fingerprint candidates — rotate on TLS handshake failure (from gpt-outlook-register).
# Đồng bộ với UA: cùng Chrome family, version giảm dần. Defined in user_agent_profile
# để khớp với WINDOWS_USER_AGENT (CHROME_MAJOR).
_IMPERSONATE_CANDIDATES = list(_UA_IMPERSONATE_CANDIDATES)


def _is_tls_error(exc: BaseException) -> bool:
    """Detect curl_cffi TLS handshake errors → worth rotating fingerprint."""
    msg = str(exc).lower()
    markers = [
        "curl: (35)", "tls connect error", "openssl_internal", "sslerror",
        "curl: (56)", "curl: (7)", "ssl_error", "handshake",
    ]
    return any(m in msg for m in markers)


def _is_cloudflare_block_error(exc: BaseException) -> bool:
    """Detect HTTP 403 từ chatgpt.com prime/csrf → Cloudflare bot-management
    flag JA3/JA4 fingerprint cụ thể.

    CF rate-limit / fingerprint-flag thường target 1 impersonate. Rotate sang
    Chrome 142 / 136 (chain ``_IMPERSONATE_CANDIDATES``) thường bypass được.
    Pattern này paralle với ``_is_tls_error`` — cùng trigger rotation chain
    trong ``_bootstrap_with_tls_rotation`` / ``session_phase._do_bootstrap``.
    """
    msg = str(exc).lower()
    if "http 403" not in msg:
        return False
    # Restrict marker để 403 từ endpoint khác (vd Stripe, OAuth) KHÔNG nhầm
    # trigger rotation — chỉ chatgpt.com prime/csrf cần fingerprint rotation.
    markers = ("prime chatgpt session", "csrf fetch")
    return any(m in msg for m in markers)


def _is_rotatable_error(exc: BaseException) -> bool:
    """Bao trùm error đáng rotate impersonate fingerprint:
    TLS handshake (curl_cffi) OR Cloudflare 403 fingerprint flag."""
    return _is_tls_error(exc) or _is_cloudflare_block_error(exc)


# ─── Sentinel ─────────────────────────────────────────────────────────


def _get_sentinel_token(
    session,
    device_id: str,
    flow: str,
    log: Callable,
    worker=None,
    *,
    persona: _BrowserPersona | None = None,
) -> str:
    """Get sentinel token: QuickJS primary → Python PoW fallback.

    Args:
        persona: BrowserPersona để inject vào sdk.js navigator + HTTP headers
            (Task 7.2 + Phase 3.2). None = default CHROME_145_WIN — phù hợp
            cho pure_request flow (curl_cffi impersonate Chrome).
        worker: SentinelNodeWorker (persistent Node), None = one-shot Node spawn.

    SECURITY NOTE (deferred-ban root cause):
        QuickJS / Node KHÔNG có canvas, WebGL, AudioContext, navigator.plugins
        thật → sdk.js đọc giá trị empty/undefined → so-token "zero-fingerprint"
        → server flag account → ban async 1-24h sau. Path ưu tiên thực sự là
        ``get_sentinel_token_async`` với ``browser_oracle`` từ Camoufox page
        sống. Function sync này chỉ dùng khi không có page (vd pure_request
        flow), chấp nhận risk fingerprint yếu.

    Phase B (chuẩn chatgpt_camoufox): env ``OPENAI_SENTINEL_REQUIRE_SIDECAR=1``
    → raise RequestPhaseError thay vì fallback QuickJS. Dùng cho production
    account creation — chấp nhận fail sớm hơn là tạo account với fingerprint
    yếu rồi bị deferred ban (uổng combo + proxy + OTP).
    """
    require_sidecar = os.getenv("OPENAI_SENTINEL_REQUIRE_SIDECAR", "0").lower() in (
        "1", "true", "yes",
    )
    if require_sidecar:
        raise RequestPhaseError(
            f"sentinel sync path bị block (OPENAI_SENTINEL_REQUIRE_SIDECAR=1) cho "
            f"flow={flow!r}. Sidecar Camoufox phải mint token — không có fallback "
            f"QuickJS/PoW vì fingerprint yếu = deferred ban. Cân nhắc chuyển "
            f"reg_mode='hybrid' (chatgpt_camoufox pipeline, sdk.js LIVE only)."
        )

    log(
        f"[sentinel] WARN: fallback path QuickJS/PoW cho flow={flow!r} — "
        f"fingerprint yếu, risk deferred ban. Set "
        f"OPENAI_SENTINEL_REQUIRE_SIDECAR=1 để fail sớm thay vì burn combo."
    )

    disable_quickjs = os.getenv("OPENAI_SENTINEL_DISABLE_QUICKJS", "0").lower() in (
        "1", "true", "yes",
    )

    if not disable_quickjs:
        try:
            from sentinel_quickjs import get_sentinel_token_via_quickjs
            token = get_sentinel_token_via_quickjs(
                session,
                device_id,
                flow=flow,
                log=log,
                worker=worker,
                persona=persona,
            )
            if token:
                return token
            log("[sentinel] QuickJS failed, falling back to Python PoW")
        except Exception as e:
            log(f"[sentinel] QuickJS import/call error, fallback: {e}")

    from sentinel_pow import get_sentinel_token as _pow_token
    return _pow_token(session, device_id, flow=flow, persona=persona)


async def _get_sentinel_token_async(
    session,
    device_id: str,
    flow: str,
    log: Callable,
    worker=None,
    *,
    persona: _BrowserPersona | None = None,
    browser_oracle=None,
) -> str:
    """Async-aware sentinel-token gen with page-native priority.

    Path order:
        1. ``browser_oracle`` page-native (REAL canvas/WebGL/audio from
           Camoufox Firefox) — anti-ban path. Token quality matches manual
           user; server không flag.
        2. QuickJS fallback — only if oracle missing OR returns None.
           Caller MUST log this as degraded path (so-token weak).
        3. Python PoW fallback — only if QuickJS also fails.

    ``browser_oracle``: instance of ``sentinel_browser.SentinelBrowserOracle``
    (already constructed by session_phase with a live Camoufox page). None
    means caller is sync (vd request_phase pure_request flow) — degrade to
    QuickJS with explicit warning log so operator knows the risk.
    """
    if browser_oracle is not None:
        try:
            token = await browser_oracle.get_token(
                device_id=device_id, flow=flow,
            )
            if token:
                log(f"[sentinel] page-native OK (flow={flow})")
                return token
            log(
                f"[sentinel] page-native returned None — fallback QuickJS "
                f"(token will have weak fingerprint; risk deferred ban)"
            )
        except Exception as exc:
            log(
                f"[sentinel] page-native error {type(exc).__name__}: {exc} "
                f"— fallback QuickJS (risk deferred ban)"
            )
    else:
        log(
            f"[sentinel] no browser_oracle — using QuickJS "
            f"(flow={flow}; risk deferred ban for new accounts)"
        )

    # Sync fallback. asyncio.to_thread to avoid blocking event loop on
    # subprocess.Popen + readline loop inside SentinelNodeWorker.
    import asyncio
    return await asyncio.to_thread(
        _get_sentinel_token,
        session, device_id, flow, log, worker,
        persona=persona,
    )


# ─── Common headers ──────────────────────────────────────────────────


def _common_headers(
    referer: str = "https://chatgpt.com/",
    *,
    persona: _BrowserPersona | None = None,
) -> dict[str, str]:
    """Common HTTP headers cho persona này. Default = CHROME_145_WIN.

    Anti-ban (journal 260625-1224 Task 4.4 + bug H4 + M1):
        Chrome gửi sec-ch-ua* (3 headers Client Hints), Firefox KHÔNG gửi.
        Hardcode 3 headers Chrome = mismatch nếu caller sau dùng Firefox persona.
        Now persona-aware: ``persona.common_headers()`` builds đúng theo browser
        family, KHÔNG gửi sec-ch-ua khi Firefox.

    Datadog headers (traceparent + x-datadog-*) luôn add — server expect cho
    sentinel cross-check.
    """
    p = persona or _DEFAULT_PERSONA
    origin = "https://chatgpt.com"
    try:
        parsed = urlparse(referer)
        if parsed.scheme and parsed.netloc:
            origin = f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        pass

    # Persona-aware base headers (Chrome có sec-ch-ua, Firefox không)
    headers = p.common_headers(referer=referer)
    # Override / add specific headers for API request
    headers["Accept"] = "application/json"
    headers["Origin"] = origin
    # Datadog RUM trace — luôn có trên request browser thật
    headers.update(_datadog_trace_headers())
    return headers


# ─── Auth state machine steps ─────────────────────────────────────────


def _prime_chatgpt_session(session, log: Callable) -> None:
    """Prime chatgpt.com session bằng GET /auth/login (HTML SSR page).

    Bug observed (verified qua test/diag_login_bootstrap.py):
        Khi GET trực tiếp `/api/auth/csrf` với jar trống, NextAuth chatgpt.com
        TRẢ về ``csrfToken`` JSON nhưng KHÔNG set cookie
        ``__Host-next-auth.csrf-token`` đi kèm. Hệ quả: POST tiếp theo tới
        ``/api/auth/signin/openai`` bị NextAuth reject với
        ``{ url: "/api/auth/signin?csrf=true" }`` (CSRF mismatch — body có
        token nhưng cookie thiếu/khác). Cascade thành HTTP 409 ``invalid_state``
        khi state machine đi tiếp tới ``authorize/continue``.

    Root cause: chatgpt.com chỉ set cookie csrf-token qua `/api/auth/csrf` KHI
        jar đã có Cloudflare bot-management cookies (``__cf_bm``, ``_cfuvid``,
        ``__cflb``). Browser thật luôn navigate qua trang HTML trước khi gọi
        API → CF cookies tự có. Pure-request hit thẳng API → thiếu CF cookies
        → server degrade response (giữ token nhưng bỏ Set-Cookie).

    Fix: GET ``/auth/login`` HTML page TRƯỚC ``/api/auth/csrf``. CF middleware
        sẽ set ``__cf_bm`` + ``__cflb`` + ``_cfuvid`` ở response này. Lần GET
        ``/api/auth/csrf`` kế tiếp sẽ set csrf-token cookie chuẩn.

    Idempotent: skip nếu jar đã có ``__cf_bm`` (đã prime trước đó trong cùng
        session). An toàn gọi nhiều lần.
    """
    try:
        if any(c.name == "__cf_bm" for c in session.cookies.jar):
            return
    except Exception:
        # Cookie jar API không expose .jar → fall through, prime lại không hại.
        pass

    log("[request] [0/9] Priming chatgpt.com session (GET /auth/login)...")

    # Inject `_dd_s` Datadog RUM cookie TRƯỚC khi prime (Task 3.5 + bug M3).
    # Browser thật luôn có cookie này khi load chatgpt.com — Datadog RUM SDK set.
    # Pure_request không có SDK → cookie vắng → server thấy "session bắt đầu
    # giữa luồng" = bất thường. Inject với rum=0 (anonymous) trước login.
    try:
        from _datadog_session import inject_dd_s as _inject_dd_s
        _inject_dd_s(session, domain=".chatgpt.com", rum=0, log=log)
    except Exception as exc:  # noqa: BLE001 — best-effort
        log(f"[request] _dd_s inject failed (continue): {exc}")

    # ── Truy cập ban đầu: promo landing (gắn campaign plus-1-month-free) ──
    # Mọi mode reg vào link promo TRƯỚC (khớp user thật click từ ad promo).
    # GET top-level no-referer (Sec-Fetch-Site=none) — cũng warm __cf_bm như
    # /auth/login. Best-effort: lỗi KHÔNG chặn flow (/auth/login vẫn prime CF).
    from config import PROMO_LANDING_URL
    promo_headers = _navigate_headers("https://chatgpt.com/")
    promo_headers.pop("Referer", None)  # đến từ ngoài → không có referer
    promo_headers["Sec-Fetch-Site"] = "none"
    promo_headers["Connection"] = "keep-alive"
    try:
        session.get(
            PROMO_LANDING_URL, headers=promo_headers, timeout=30,
            allow_redirects=True,
        )
        log("[request] [0/9] promo landing visited (plus-1-month-free)")
    except Exception as exc:  # noqa: BLE001 — best-effort
        log(f"[request] promo landing visit failed (continue): {exc}")

    # Headers persona-aware (Task 7.5) — page navigate kèm Connection.
    headers = _navigate_headers("https://chatgpt.com/")
    headers["Connection"] = "keep-alive"
    # Retry up to 3x on Cloudflare 403 (transient bot-management challenge),
    # backoff 5s/10s — đồng bộ pattern `_step_csrf`. Cùng host (chatgpt.com)
    # → cùng kiểu lỗi: CF middleware đôi khi trả 403 trước khi cấp __cf_bm,
    # retry với jar warm sau 5-10s thường pass.
    resp = None
    for attempt in range(3):
        resp = session.get(
            "https://chatgpt.com/auth/login",
            headers=headers,
            timeout=30,
            allow_redirects=True,
        )
        if resp.status_code == 403 and attempt < 2:
            wait = (attempt + 1) * 5
            log(f"[request] prime 403, retrying in {wait}s ({attempt + 1}/3)...")
            time.sleep(wait)
            continue
        break
    if resp is None or resp.status_code >= 400:
        raise RequestPhaseError(
            f"prime chatgpt session failed: HTTP {resp.status_code if resp else '?'}"
        )


def _step_providers(session, log: Callable) -> None:
    """Step 0.5: GET /api/auth/providers (NextAuth providers list).

    Anti-ban (Phase 8 audit gap): trace tay xác nhận browser thật GET
    ``/api/auth/providers`` TRƯỚC ``/api/auth/csrf`` (~337ms gap). NextAuth
    client SDK luôn fetch providers list khi load auth page rồi mới fetch
    csrf trước khi signin click. Pure_request gọi thẳng csrf → server
    thấy "missing providers fetch" = pattern bot.

    Best-effort: response body không cần parse — chỉ cần server thấy GET.
    """
    log("[request] [1a/9] Fetching providers list...")
    headers = _common_headers("https://chatgpt.com/auth/login")
    try:
        resp = session.get(
            "https://chatgpt.com/api/auth/providers",
            headers=headers,
            timeout=15,
        )
        if resp.status_code != 200:
            log(f"[request] providers HTTP {resp.status_code} (continue, non-fatal)")
    except Exception as exc:  # noqa: BLE001 — best-effort
        log(f"[request] providers fetch failed (continue): {exc}")


def _step_csrf(session, log: Callable) -> str:
    """Step 1: GET chatgpt.com/api/auth/csrf → csrfToken.

    Tự động prime session qua ``_prime_chatgpt_session`` trước để đảm bảo
    NextAuth set cookie ``__Host-next-auth.csrf-token`` cho lần POST
    ``/api/auth/signin/openai`` kế tiếp (xem docstring _prime_chatgpt_session).

    Bao gồm step ``_step_providers`` (Phase 8 audit gap) — browser thật luôn
    fetch providers TRƯỚC csrf.

    Retry up to 3x on Cloudflare 403 (transient rate-limit), backoff 5s/10s.
    """
    _prime_chatgpt_session(session, log)
    _step_providers(session, log)
    log("[request] [1/9] Fetching CSRF token...")
    headers = _common_headers("https://chatgpt.com/auth/login")
    resp = None
    for attempt in range(3):
        resp = session.get(
            "https://chatgpt.com/api/auth/csrf",
            headers=headers,
            timeout=30,
        )
        if resp.status_code == 403 and attempt < 2:
            wait = (attempt + 1) * 5
            log(f"[request] Cloudflare 403, retrying in {wait}s ({attempt + 1}/3)...")
            time.sleep(wait)
            continue
        break
    if resp is None or resp.status_code != 200:
        raise RequestPhaseError(f"CSRF fetch failed: HTTP {resp.status_code if resp else '?'}")
    csrf = resp.json().get("csrfToken", "")
    if not csrf:
        raise RequestPhaseError("CSRF token missing from response")
    log(f"[request] CSRF: {csrf[:20]}...")
    return csrf


def _bootstrap_with_tls_rotation(
    proxy: str | None,
    log: Callable,
    *,
    login_hint: str = "",
) -> tuple[Any, str, str]:
    """Bootstrap CSRF + auth_url + OAuth init with TLS fingerprint rotation.

    On TLS handshake error, rotates curl_cffi impersonate fingerprint
    qua các candidate trong ``_IMPERSONATE_CANDIDATES`` (chain Chrome desktop
    Windows: chrome145 → chrome142 → chrome136 — đồng bộ với
    ``user_agent_profile.WINDOWS_USER_AGENT``).
    Bootstrap steps carry no critical session state yet, so restarting is safe.

    Returns: (session, device_id, auth_url)
    """
    last_exc: BaseException | None = None
    for idx, impersonate in enumerate(_IMPERSONATE_CANDIDATES):
        session = _create_session(proxy=proxy, impersonate=impersonate)
        try:
            if idx > 0:
                log(f"[request] fingerprint rotation: retrying with impersonate={impersonate}")
            device_id = str(uuid.uuid4())
            csrf = _step_csrf(session, log)
            auth_url = _step_auth_url(session, csrf, log, device_id=device_id, login_hint=login_hint)
            oauth_did = _step_oauth_init(session, auth_url, log)
            if oauth_did:
                device_id = oauth_did
            return session, device_id, auth_url
        except Exception as e:
            last_exc = e
            try:
                session.close()
            except Exception:
                pass
            # Rotate impersonate khi: TLS handshake fail HOẶC CF 403 flag JA3
            # (cùng impersonate retry vô ích — phải đổi fingerprint Chrome).
            if _is_rotatable_error(e) and idx < len(_IMPERSONATE_CANDIDATES) - 1:
                continue
            raise
    # Exhausted all fingerprints
    if last_exc and _is_rotatable_error(last_exc):
        raise RequestPhaseError(
            f"Bootstrap failed với mọi impersonate fingerprint "
            f"({len(_IMPERSONATE_CANDIDATES)}× tried) — Cloudflare flag IP "
            f"hoặc network không reach chatgpt.com. Last error: {last_exc}"
        ) from last_exc
    if last_exc:
        raise last_exc
    raise RequestPhaseError("bootstrap failed unexpectedly")


def _step_auth_url(session, csrf_token: str, log: Callable, device_id: str = "", login_hint: str = "") -> str:
    """Step 2: POST chatgpt.com/api/auth/signin/openai → authorize URL.

    Must include query params matching browser:
    prompt=login, ext-oai-did, ext-passkey-client-capabilities, screen_hint=login_or_signup
    login_hint={email} for login flow (lets server route to password/verify directly).

    Anti-ban (Task 4.6 + bug C2): ``auth_session_logging_id`` đọc từ cookie
    ``oai-asli`` (sentinel SDK chatgpt.com đã set khi prime). Khớp với cookie
    → server cross-check pass. Code cũ KHÔNG có param này → server thấy lạ.
    """
    log("[request] [2/8] Getting authorize URL...")
    headers = _common_headers("https://chatgpt.com/auth/login")
    headers["Content-Type"] = "application/x-www-form-urlencoded"

    # Query params matching browser (_nextauth_bootstrap.py)
    params = {
        "prompt": "login",
        "ext-passkey-client-capabilities": "01001",
        "screen_hint": "login_or_signup",
    }
    if device_id:
        params["ext-oai-did"] = device_id
    if login_hint:
        params["login_hint"] = login_hint

    # Đọc cookie oai-asli → query param auth_session_logging_id (Task 4.6).
    try:
        from _nextauth_bootstrap import read_oai_asli_from_session as _read_asli_sync
        asli = _read_asli_sync(session)
        if asli:
            params["auth_session_logging_id"] = asli
            log(f"[request] auth_session_logging_id={asli} (from oai-asli cookie)")
    except Exception as exc:  # noqa: BLE001 — best-effort
        log(f"[request] read oai-asli failed (continue): {exc}")

    from urllib.parse import urlencode as _urlencode
    url = "https://chatgpt.com/api/auth/signin/openai?" + _urlencode(params)

    resp = session.post(
        url,
        headers=headers,
        data={
            "csrfToken": csrf_token,
            "callbackUrl": "https://chatgpt.com/",
            "json": "true",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RequestPhaseError(f"signin/openai failed: HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except Exception as exc:
        body_preview = (resp.text or "")[:400]
        raise RequestPhaseError(
            f"signin/openai: non-JSON response (HTTP {resp.status_code}): {body_preview}"
        ) from exc
    auth_url = payload.get("url", "") if isinstance(payload, dict) else ""
    if not auth_url:
        raise RequestPhaseError(
            f"signin/openai: no URL in response — payload={str(payload)[:300]}"
        )

    # Fail-fast: validate URL trỏ về auth.openai.com (OAuth provider thật).
    # NextAuth trả URL dạng `<chatgpt.com>/api/auth/signin?csrf=true&...` khi
    # CSRF validation fail (cookie `__Host-next-auth.csrf-token` không match
    # body `csrfToken`). Trước đây code accept URL này → đem GET → landing
    # trên chatgpt.com signin page → fallback gọi `authorize/continue` mà
    # OpenAI chưa có OAuth session → cascade thành HTTP 409 `invalid_state`.
    # Validate sớm để báo lỗi đúng chỗ + dump bằng chứng để debug.
    if "auth.openai.com" not in auth_url:
        # Dump cookies hiện có (mask value) để diagnose CSRF mismatch.
        try:
            cookie_summary = ", ".join(
                sorted(
                    f"{c.name}={'<set>' if c.value else '<empty>'}"
                    for c in session.cookies.jar
                )
            )
        except Exception:
            cookie_summary = "<unavailable>"
        raise RequestPhaseError(
            "signin/openai trả URL không phải auth.openai.com — "
            "NextAuth từ chối (CSRF/origin/anti-bot). "
            f"Got: {auth_url[:200]}. Cookies: {cookie_summary[:300]}"
        )

    log(f"[request] Auth URL: {auth_url[:80]}...")
    return auth_url


def _step_oauth_init(session, auth_url: str, log: Callable) -> str:
    """Step 3: Follow authorize URL → extract device_id from oai-did cookie."""
    log("[request] [3/9] OAuth init...")
    # Page navigate (Task 7.5) — persona-aware
    headers = _navigate_headers("https://chatgpt.com/auth/login")
    session.get(auth_url, headers=headers, timeout=30, allow_redirects=True)

    # Extract device_id from cookies
    device_id = ""
    try:
        device_id = session.cookies.get("oai-did", "")
    except Exception:
        pass
    if not device_id:
        device_id = str(uuid.uuid4())
        log(f"[request] Generated device_id: {device_id}")
    else:
        log(f"[request] Device ID: {device_id}")
    return device_id


def _step_authorize_continue(
    session,
    email: str,
    sentinel_token: str,
    screen_hint: str,
    referer: str,
    device_id: str,
    log: Callable,
) -> dict:
    """POST authorize/continue — submit email to auth state machine."""
    headers = _common_headers(referer)
    headers["Content-Type"] = "application/json"
    if sentinel_token:
        headers["openai-sentinel-token"] = sentinel_token
    if device_id:
        headers["oai-device-id"] = device_id

    payload = {
        "username": {"value": email, "kind": "email"},
        "screen_hint": screen_hint,
    }
    resp = session.post(
        "https://auth.openai.com/api/accounts/authorize/continue",
        headers=headers,
        json=payload,
        timeout=30,
    )
    if resp.status_code != 200:
        body = (resp.text or "")[:300]
        raise RequestPhaseError(
            f"authorize/continue failed: HTTP {resp.status_code} - {body}"
        )
    try:
        return resp.json()
    except Exception:
        return {}


# REMOVED 2026-06-25 (anti-ban Phase 7 Task 7.1):
#   - ``_step_signup`` — gọi ``/authorize/continue`` mà browser thật KHÔNG gọi
#     trong signup flow. ``_run_request_phase_sync`` không dùng (detect new vs
#     existing qua HTTP status của ``/register``).
#   - ``_step_register_password`` — visit ``/create-account/password`` XHR mà
#     browser thật KHÔNG visit. Sync flow đã inline POST register với header
#     persona-aware (Phase 4 Task 4.2 wire ``/email-verification`` HTML thay).
#
# Caller cũ nếu còn import sẽ raise AttributeError → bug rõ ràng dễ fix. Hàm
# ``_step_authorize_continue`` GIỮ vì ``session_phase.py`` (login flow) còn dùng.


def _navigate_headers(
    referer: str,
    *,
    persona: _BrowserPersona | None = None,
) -> dict[str, str]:
    """Page navigate request headers (vd ``GET /email-verification`` HTML).

    Anti-ban (journal 260625-1224 Task 4.3 + bug H2):
        Trace tay xác nhận browser gửi GET ``/email-otp/send`` (HTTP 302) như
        page navigate, KHÔNG phải XHR/fetch:
            Sec-Fetch-Dest: document
            Sec-Fetch-Mode: navigate
            Sec-Fetch-Site: same-origin
            Sec-Fetch-User: ?1
            Upgrade-Insecure-Requests: 1
            Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8

        Code cũ dùng ``_common_headers`` = XHR mode (Sec-Fetch-Mode: cors,
        Accept: application/json). Server thấy unusual XHR → flag.

    KHÔNG có Datadog headers (page navigate KHÔNG có Datadog RUM trace).
    """
    p = persona or _DEFAULT_PERSONA
    headers = p.common_headers(referer=referer)
    headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Connection": "keep-alive",
    })
    return headers


def _step_send_otp(session, device_id: str, log: Callable) -> None:
    """Step 6a: Trigger OTP email delivery.

    Trace tay (HAR `web_record_20260625-120705_manual`): GET ``/email-otp/send``
    là page navigate → 302 redirect tới ``/email-verification`` HTML. Browser
    follow redirect tự nhiên. KHÔNG phải XHR.

    Code cũ gửi XHR với Accept: application/json → server flag bot. Fix:
    dùng ``_navigate_headers`` (Task 4.3).

    Phase 7 Task 7.1: Bỏ fallback ``passwordless/send-otp`` — endpoint không
    có trong record tay → chỉ-bot-mới-gọi. Nếu primary fail thì raise.
    """
    log("[request] [6/9] Sending OTP (page navigate)...")
    headers = _navigate_headers("https://auth.openai.com/create-account/password")
    if device_id:
        headers["oai-device-id"] = device_id

    resp = session.get(
        "https://auth.openai.com/api/accounts/email-otp/send",
        headers=headers,
        timeout=30,
        allow_redirects=True,   # follow 302 → /email-verification
    )
    if resp.status_code not in (200, 302):
        raise RequestPhaseError(
            f"OTP send failed: HTTP {resp.status_code}: {(resp.text or '')[:200]}"
        )
    log("[request] OTP sent")


def _step_resend_otp(session, device_id: str, log: Callable) -> bool:
    """Resend OTP (for existing account flow)."""
    headers = _common_headers("https://auth.openai.com/email-verification")
    headers["Content-Type"] = "application/json"
    if device_id:
        headers["oai-device-id"] = device_id
    resp = session.post(
        "https://auth.openai.com/api/accounts/email-otp/resend",
        headers=headers,
        timeout=30,
    )
    if resp.status_code == 200:
        log("[request] OTP resent")
        return True
    log(f"[request] OTP resend failed: {resp.status_code}")
    return False


def _step_verify_otp(
    session, otp_code: str, device_id: str, log: Callable,
    *, raise_on_fail: bool = True,
) -> dict:
    """Step 7: Verify OTP code.

    raise_on_fail=True (mặc định, cho session_phase): raise RequestPhaseError nếu
    HTTP != 200. raise_on_fail=False (cho retry ở request_phase): trả dict kèm
    metadata ``_ok`` / ``_status`` / ``_body`` để caller tự quyết định retry.
    """
    log("[request] [7/9] Verifying OTP...")
    headers = _common_headers("https://auth.openai.com/email-verification")
    headers["Content-Type"] = "application/json"
    if device_id:
        headers["oai-device-id"] = device_id

    resp = session.post(
        "https://auth.openai.com/api/accounts/email-otp/validate",
        headers=headers,
        json={"code": otp_code},
        timeout=30,
    )
    if resp.status_code != 200:
        body = resp.text or ""
        if raise_on_fail:
            raise RequestPhaseError(
                f"OTP verify failed: HTTP {resp.status_code} - {body[:200]}"
            )
        log(f"[request] OTP verify HTTP {resp.status_code}: {body[:120]}")
        return {"_ok": False, "_status": resp.status_code, "_body": body}
    log("[request] OTP verified")
    try:
        data = resp.json()
    except Exception:
        data = {}
    if isinstance(data, dict):
        data["_ok"] = True
        data["_status"] = 200
    return data


def _step_create_account(
    session, name: str, birthdate: str, device_id: str, log: Callable,
    sentinel_token: str | None = None, worker=None,
    so_token: str | None = None,
) -> str:
    """Step 8: Create account (fill name + birthdate) → continue_url.

    ``sentinel_token``: nếu đã pre-compute sẵn (song song lúc poll OTP) thì dùng
    luôn, bỏ qua bước tính sentinel tại đây. None → tính mới.
    ``so_token``: ``openai-sentinel-so-token`` header (Session Observer JSON).
    None → bỏ header (chỉ pure_request không có sidecar mới None).
    """
    log("[request] [8/9] Creating account...")

    # Refresh sentinel for create_account flow (dùng token pre-computed nếu có)
    sentinel = sentinel_token or _get_sentinel_token(
        session, device_id, "create_account", log, worker=worker,
    )

    headers = _common_headers("https://auth.openai.com/about-you")
    headers["Content-Type"] = "application/json"
    if sentinel:
        headers["openai-sentinel-token"] = sentinel
    # so-token = Session Observer JSON {so, c}. Server REQUIRES this on
    # /create_account khi sentinel SDK đã observe DOM events. Bỏ header này
    # = signal "Sentinel Observer chưa init" = bot. Sidecar gen so-token
    # bằng cách simulate form interaction trên Camoufox page trước khi
    # gọi sdk.js.token(). None → caller (pure_request không có sidecar)
    # đã log warning ở chỗ khác.
    if so_token:
        headers["openai-sentinel-so-token"] = so_token
    if device_id:
        headers["oai-device-id"] = device_id

    resp = session.post(
        "https://auth.openai.com/api/accounts/create_account",
        headers=headers,
        json={"name": name, "birthdate": birthdate},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RequestPhaseError(
            f"create_account failed: HTTP {resp.status_code} - {(resp.text or '')[:300]}"
        )
    data = resp.json()
    continue_url = (data.get("continue_url") or "").strip()
    if not continue_url:
        raise RequestPhaseError("create_account: no continue_url in response")
    log("[request] Account created")
    return continue_url


def _step_follow_redirects(session, start_url: str, log: Callable) -> tuple[str, str]:
    """Step 9: Follow redirect chain → (callback_url, final_url)."""
    log("[request] [9/9] Following redirect chain...")
    current = start_url
    callback_url = ""

    for i in range(12):
        if "/api/auth/callback/openai" in current and "code=" in current:
            callback_url = current
            break

        # Page navigate (Task 7.5) — persona-aware redirect follow
        headers = _navigate_headers("https://chatgpt.com/")
        resp = session.get(current, headers=headers, timeout=30, allow_redirects=False)

        if resp.status_code in (301, 302, 303, 307, 308):
            location = (resp.headers.get("Location") or "").strip()
            if not location:
                break
            if location.startswith("/"):
                parsed = urlparse(current)
                location = f"{parsed.scheme}://{parsed.netloc}{location}"
            if "/api/auth/callback/openai" in location and "code=" in location:
                callback_url = location
                current = location
                break
            current = location
        else:
            break

    log(f"[request] Redirect chain done, callback={'found' if callback_url else 'missing'}")
    return callback_url, current


def _consume_callback(session, callback_url: str, log: Callable) -> bool:
    """Follow callback redirect chain hop-by-hop để NextAuth set + capture
    session-token cookie vào Python cookie jar.

    QUAN TRỌNG — KHÔNG dùng ``allow_redirects=True``: khi libcurl tự follow
    redirect, cookie ``__Secure-next-auth.session-token`` được set ở response
    TRUNG GIAN (302 của ``/api/auth/callback/openai``) chỉ nằm trong cookie
    store nội bộ của libcurl. curl_cffi chỉ sync Set-Cookie của response CUỐI
    về ``session.cookies`` phía Python → cookie session-token bị mất khỏi jar
    (dù request kế tiếp tới ``/api/auth/session`` vẫn gửi được nên accessToken
    vẫn lấy được). Hệ quả: ``session_token`` rỗng dù account đã đăng nhập.

    Fix: follow từng hop với ``allow_redirects=False`` để curl_cffi sync
    Set-Cookie của MỖI response về jar — giống ``_step_follow_redirects``.

    NextAuth có thể chunk cookie thành ``.0`` / ``.1`` khi JWT > 4KB; dừng sớm
    ngay khi ``_read_session_token_cookie`` ghép được token từ jar.
    """
    if not callback_url or "code=" not in callback_url:
        return False

    # Page navigate (Task 7.5) — persona-aware callback follow
    headers = _navigate_headers("https://auth.openai.com/")

    current = callback_url
    try:
        for _ in range(12):
            resp = session.get(
                current, headers=headers, timeout=30, allow_redirects=False,
            )

            # Cookie có thể được set ở bất kỳ hop nào → check ngay sau mỗi hop.
            if _read_session_token_cookie(session):
                return True

            if resp.status_code in (301, 302, 303, 307, 308):
                location = (resp.headers.get("Location") or "").strip()
                if not location:
                    break
                if location.startswith("/"):
                    parsed = urlparse(current)
                    location = f"{parsed.scheme}://{parsed.netloc}{location}"
                current = location
                continue
            break

        return bool(_read_session_token_cookie(session))
    except Exception as e:
        log(f"[request] Consume callback error: {e}")
        return False


def _read_session_token_cookie(session) -> str:
    """Đọc cookie ``__Secure-next-auth.session-token`` (kèm reassembly chunk).

    NextAuth chunk session-token thành ``.0`` / ``.1`` / ... khi JWT > 4KB
    (rất thường gặp với account ChatGPT vì payload lớn). curl_cffi
    ``cookies.get("...session-token")`` chỉ trả cookie tên gốc → rỗng khi bị
    chunk → ``session_token`` mất trắng dù account đã đăng nhập thật.

    Mirror logic ``http_phase._extract_session_from_handoff``: ưu tiên cookie
    tên gốc; nếu không có thì ghép các chunk ``.N`` theo thứ tự index tăng dần.
    """
    base = session.cookies.get("__Secure-next-auth.session-token", "") or ""
    if base:
        return base

    chunks: dict[int, str] = {}
    prefix = "__Secure-next-auth.session-token."
    try:
        for cookie in session.cookies:
            name = getattr(cookie, "name", "") or ""
            value = getattr(cookie, "value", "") or ""
            if name.startswith(prefix) and value:
                suffix = name[len(prefix):]
                try:
                    chunks[int(suffix)] = value
                except ValueError:
                    continue
    except Exception:
        return ""

    if not chunks:
        return ""
    return "".join(chunks[k] for k in sorted(chunks))


def _get_session_tokens(session, log: Callable) -> tuple[str, str, str]:
    """GET /api/auth/session → (session_token, access_token, user_id)."""
    headers = _common_headers("https://chatgpt.com/")
    resp = session.get(
        "https://chatgpt.com/api/auth/session",
        headers=headers,
        timeout=30,
    )
    if resp.status_code != 200:
        log(f"[request] /api/auth/session HTTP {resp.status_code}")
        return "", "", ""

    data = resp.json() if resp is not None else {}
    access_token = data.get("accessToken", "") or ""
    user = data.get("user", {}) or {}
    user_id = user.get("id", "") or ""

    # Session token from cookie (reassembly chunk .0/.1 nếu NextAuth split JWT).
    session_token = _read_session_token_cookie(session)
    return session_token, access_token, user_id


# ─── OTP polling bridge (async mail provider → sync wait) ─────────────


async def _poll_otp_async(
    provider: MailProvider,
    *,
    recipient: str,
    started_at: datetime,
    timeout_seconds: float,
    poll_interval_seconds: float,
    log: Callable,
) -> str:
    """Async wrapper for mail provider OTP polling."""
    return await provider.poll_otp(
        recipient=recipient,
        started_at=started_at,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        log=log,
    )


# ─── Main orchestrator ────────────────────────────────────────────────


def _acquire_fresh_otp(
    *,
    session,
    device_id: str,
    mail_provider: MailProvider,
    request: SignupRequest,
    log: Callable,
    loop,
    started_at: datetime,
    tried_codes: set[str],
    pending: list[str],
    max_resends: int,
    prefer_second_code: bool = False,
) -> tuple[str, int]:
    """Lấy 1 OTP code chưa nằm trong ``tried_codes`` — mirror vòng poll đầu của
    ``browser_phase._run_signup_flow``.

    Hành vi (theo thứ tự ưu tiên mỗi vòng):
      1. Pop ``pending`` (code dư từ ``poll_all_codes`` lần trước) chưa thử → trả ngay.
      2. Poll 1 chunk ngắn (15s) để kiểm tra ngưỡng resend kịp thời.
      3. Nhận code MỚI → ``poll_all_codes`` để bắt mail delay (iCloud HME hay gửi
         trễ/nhiều mail OTP); nạp code dư vào ``pending``, trả code đầu.
      4. RESEND khi đã chờ quá ngưỡng (random ~[base*0.5, base], base =
         otp_resend_after_seconds) mà CHƯA có code mới — bất kể mailbox trả code cũ
         (stale) hay rỗng. Khác browser ở chỗ này: account MỚI mailbox chỉ có đúng
         1 code; nếu code đó sai thì KHÔNG bao giờ có code mới nếu không resend.
         Resend reset mốc thời gian + chỉ nhận code về SAU resend (``cur_started``).
      5. Hết quota ``max_resends`` hoặc chưa tới ngưỡng → chờ tiếp tới hết
         ``otp_timeout_seconds`` rồi raise.

    Mutates ``pending`` in-place. Trả ``(code, resends_used)``. Raise
    ``RequestPhaseError`` khi hết ``otp_timeout_seconds`` mà không có code mới.

    ``prefer_second_code``: khi mailbox có ≥2 mã ở lần fetch đầu, submit mã THỨ 2
        (mã "sau") trước, mã đầu giữ lại trong ``pending`` làm fallback. Dùng cho
        lần poll đầu — iCloud worker đôi khi thiếu ``date`` nên thứ tự không chắc
        mới→cũ; thực tế mã thứ 2 thường là mã hợp lệ.
    """
    recipient = request.source_email or request.email
    poll_interval = max(5.0, request.otp_poll_interval_seconds)
    # Poll theo chunk ngắn để check ngưỡng resend kịp thời ngay cả khi mailbox
    # chỉ trả code cũ (stale) hoặc rỗng liên tục.
    poll_chunk = 15.0

    def _resend_threshold() -> float:
        # Random hoá thời điểm resend (human-like) trong [base*0.5, base];
        # base = otp_resend_after_seconds (config). base=120s → ~60-120s ("1-2 phút").
        base = max(10.0, float(request.otp_resend_after_seconds))
        return random.uniform(base * 0.5, base)

    resend_count = 0
    stale_count = 0
    cur_started = started_at
    deadline = time.monotonic() + request.otp_timeout_seconds
    # Mốc đo thời gian chờ code mới — reset sau mỗi resend.
    resend_window_start = time.monotonic()
    resend_threshold = _resend_threshold()

    while True:
        # 1. Pop pending chưa thử trước khi đụng mạng.
        while pending:
            candidate = pending.pop(0)
            if candidate not in tried_codes:
                return candidate, resend_count

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RequestPhaseError(
                f"OTP timeout {request.otp_timeout_seconds:.0f}s — không nhận được "
                f"code mới (đã resend {resend_count} lần)"
            )

        # 2. Poll 1 chunk ngắn.
        chunk = min(poll_chunk, remaining)
        try:
            candidate = loop.run_until_complete(
                mail_provider.poll_otp(
                    recipient=recipient,
                    started_at=cur_started,
                    timeout_seconds=chunk,
                    poll_interval_seconds=poll_interval,
                    log=log,
                )
            )
        except TimeoutError:
            candidate = ""
        except Exception as exc:
            log(f"[request] poll OTP lỗi (tiếp tục): {type(exc).__name__}: {exc}")
            candidate = ""

        # 3. Code MỚI → fetch all để bắt mail delay, trả code đầu.
        if candidate and candidate not in tried_codes:
            time.sleep(2.0)
            all_codes: list[str] = []
            if hasattr(mail_provider, "poll_all_codes"):
                try:
                    all_codes = loop.run_until_complete(
                        mail_provider.poll_all_codes(
                            recipient=recipient,
                            started_at=cur_started,
                            log=log,
                        )
                    )
                except Exception:
                    all_codes = []
            fresh = [c for c in all_codes if c not in tried_codes]
            if not fresh:
                fresh = [candidate]
            elif candidate not in fresh:
                fresh.insert(0, candidate)
            if len(fresh) > 1:
                log(f"[request] nhận {len(fresh)} OTP codes mới: {', '.join(fresh)}")
            # prefer_second_code: có ≥2 mã ở lần fetch đầu → lấy mã THỨ 2 ("mã sau")
            # trước; mã đầu giữ lại pending làm fallback. Thứ tự worker không chắc
            # mới→cũ (thiếu date) nên mã thứ 2 thường mới là mã hợp lệ.
            if prefer_second_code and len(fresh) >= 2:
                first = fresh.pop(1)
                log(f"[request] ưu tiên submit mã thứ 2 ({first}), giữ {fresh[0]} fallback")
            else:
                first = fresh.pop(0)
            pending[:] = fresh
            return first, resend_count

        # 4. Code cũ (đã thử) lặp lại — log theo dõi.
        if candidate and candidate in tried_codes:
            stale_count += 1
            log(
                f"[request] poll trả code đã thử ({candidate}) → chờ code mới "
                f"(lần {stale_count})"
            )

        # 5. Chưa có code mới (stale HOẶC rỗng). Resend khi đã chờ quá ngưỡng + còn
        #    quota. Account MỚI mailbox chỉ có 1 code: code sai → phải resend mới có
        #    code mới, KHÔNG thể chờ suông.
        waited = time.monotonic() - resend_window_start
        if resend_count < max_resends and waited >= resend_threshold:
            resend_count += 1
            log(
                f"[request] chờ {waited:.0f}s chưa có code mới — resend OTP "
                f"({resend_count}/{max_resends})"
            )
            try:
                if not _step_resend_otp(session, device_id, log):
                    _step_send_otp(session, device_id, log)
            except Exception as exc:
                log(f"[request] resend OTP lỗi (vẫn poll tiếp): {exc}")
            time.sleep(2.0)
            # Chỉ nhận code về SAU resend; reset cửa sổ chờ + random ngưỡng mới.
            cur_started = datetime.now(timezone.utc)
            resend_window_start = time.monotonic()
            resend_threshold = _resend_threshold()
            continue

        # Chưa tới ngưỡng resend (hoặc hết quota) → chờ rồi poll lại.
        time.sleep(poll_interval)


def _prefer_newest_untried_otp(
    *,
    current: str,
    mail_provider: MailProvider,
    loop,
    recipient: str,
    started_at: datetime,
    tried_codes: set[str],
    pending: list[str],
    log: Callable,
) -> str:
    """Refresh mailbox 1 lần (non-blocking) ngay trước khi verify, trả code MỚI
    NHẤT chưa thử.

    Lý do: ``_acquire_fresh_otp`` trả code newest tại thời điểm gọi, nhưng có nhịp
    human-delay (2-4s) trước khi submit. Code mới hơn (OpenAI gửi lại / mail
    in-flight) có thể về đúng trong khoảng này. OpenAI vô hiệu code cũ khi phát code
    mới → verify code cũ trước sẽ ăn 401 dư thừa rồi mới retry sang code mới.

    An toàn với lệch giờ HME: nếu có code mới hơn, code hiện tại KHÔNG bị bỏ — nó
    được đẩy lên đầu ``pending`` để retry vẫn thử lại nếu code mới sai. Vì vậy hành
    vi luôn ``>=`` logic cũ (không bao giờ mất code, chỉ tiết kiệm 1 lần 401 khi
    đoán đúng).

    Chỉ áp dụng cho provider có ``poll_all_codes`` (worker/iCloud). Provider khác
    trả ``current`` nguyên trạng.
    """
    if not hasattr(mail_provider, "poll_all_codes"):
        return current
    try:
        codes = loop.run_until_complete(
            mail_provider.poll_all_codes(
                recipient=recipient, started_at=started_at, log=log,
            )
        )
    except Exception:
        return current

    # poll_all_codes trả mới→cũ. Chọn code mới nhất chưa thử.
    untried = [c for c in codes if c not in tried_codes]
    if not untried:
        return current
    newest = untried[0]
    if newest == current:
        return current

    # Có code mới hơn current → verify code này. Đẩy current + các untried còn lại
    # vào đầu pending (giữ thứ tự mới→cũ) để retry không mất code nào.
    fallback = [current] + [c for c in untried[1:] if c != current]
    for code in reversed(fallback):
        if code not in tried_codes and code not in pending:
            pending.insert(0, code)
    log(
        f"[request] code mới hơn ({newest}) vừa về trong lúc chờ → "
        f"verify code này thay cho {current} (giữ {current} làm fallback)"
    )
    return newest


def _run_request_phase_sync(
    request: SignupRequest,
    mail_provider: MailProvider,
    log: Callable,
    on_checkpoint: Callable | None = None,
    sidecar: Any = None,
) -> dict[str, Any]:
    """Synchronous core — runs in thread via asyncio.to_thread.

    ``sidecar``: optional ``SentinelSidecar`` (from ``sentinel_sidecar.py``).
    When provided, sentinel-token + so-token + key cookies (oai-sc, _dd_s,
    oai-asli) are sourced from a real Camoufox page running in a daemon
    thread — eliminates the QuickJS zero-fingerprint path that triggers
    deferred account ban. None → fall back to QuickJS (legacy, risky).

    Flow (matching browser HAR):
      1. CSRF + signin/openai (login_hint) → auth_url
      2. GET auth_url (OAuth init) → device_id
      3. Sentinel token  (sidecar.get_sentinel_token if available)
      4. POST /api/accounts/user/register (email + password) — DIRECT, no authorize/continue
      5. GET /api/accounts/email-otp/send
      6. Poll OTP (started_at = exact send time) → POST email-otp/validate
      7. POST /api/accounts/create_account  (+openai-sentinel-so-token via sidecar)
      8. Follow redirect chain → callback → session
    """
    worker = None
    try:
        # Persistent Node worker cho sentinel (warm — tránh cold-start V8 mỗi action).
        # Dùng chung cho cả sentinel #1 (register) và #2 (create_account, pre-computed).
        from sentinel_quickjs import create_worker as _create_sentinel_worker
        try:
            worker = _create_sentinel_worker(log)
        except Exception as _e:
            log(f"[request] sentinel worker init failed, dùng one-shot: {_e}")
            worker = None

        # Step 1-5: Bootstrap + Register, có retry cho HTTP 409 invalid_state.
        #
        # Khi server trả 409 ``invalid_state`` ("Your sign-in session is no
        # longer valid"), state machine OAuth đã desync (CSRF/auth_url/sentinel
        # cũ không còn hợp lệ). Cách fix duy nhất là RE-BOOTSTRAP toàn bộ:
        # session mới, CSRF mới, device_id mới, sentinel mới — KHÔNG được tái
        # dùng artifact cũ. Retry tối đa 3 lần để tránh loop vô tận khi server
        # đang gặp vấn đề thực sự.
        max_register_attempts = 3
        session = None
        device_id = ""
        password = request.password or _default_password(request.email)
        reg_continue = ""
        reg_page_type = ""

        for register_attempt in range(1, max_register_attempts + 1):
            # Đóng session cũ trước khi re-bootstrap (nếu có)
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
                session = None

            if register_attempt > 1:
                log(
                    f"[request] Re-bootstrap mới "
                    f"(lần {register_attempt}/{max_register_attempts}) "
                    f"sau HTTP 409 invalid_state"
                )

            # Step 1-3: Bootstrap with TLS fingerprint rotation on handshake failure.
            # Pass login_hint=email so authorize routes to the correct account context.
            session, device_id, _auth_url = _bootstrap_with_tls_rotation(
                request.proxy, log, login_hint=request.email,
            )

            # Step 4: GET /email-verification HTML (page navigate) để seed
            # server signup state + cookie ``oai-login-csrf_dev_*`` (Task 4.2).
            #
            # Trace tay (HAR `web_record_20260625-120705_manual`): browser
            # navigate /email-verification rồi SPA tự chuyển /create-account/password
            # client-side (KHÔNG có request HTTP /create-account/password).
            #
            # Code cũ GET /create-account/password = browser thật KHÔNG gửi →
            # bất thường. Hơn nữa /email-verification response set 8+ session
            # cookies (oai-login-csrf_dev_*, rg_context, login_session, ...)
            # mà /create-account/password KHÔNG set → cookie chain mismatch.
            try:
                ev_headers = _navigate_headers("https://chatgpt.com/")
                session.get(
                    "https://auth.openai.com/email-verification",
                    headers=ev_headers,
                    timeout=15,
                    allow_redirects=True,
                )
            except Exception as exc:
                log(f"[request] /email-verification visit failed (continue): {exc}")

            # Step 5: Sentinel (flow=username_password_create) + user/register
            #
            # Prefer the page-native path via ``sidecar`` (Phase 10/11):
            # real Firefox canvas/WebGL/audio → token fingerprint matches
            # browser users. QuickJS fallback ONLY when sidecar missing or
            # returns None (degrade with warning — operator sees the risk).
            #
            # Order (best → worst fingerprint quality):
            #   1. K2 intercept_register_token — sidecar drives a real form
            #      submission, captures ``openai-sentinel-token`` from the
            #      live /register POST headers, aborts. sdk.js fires the
            #      CORRECT path (no Xray buildGenerateFailMessage trap)
            #      because submission is user-initiated. Body is not
            #      hashed in sentinel-token, so caller reuses it for the
            #      REAL /register POST with the user's password.
            #   2. get_sentinel_token (page.evaluate(sdk)) — degrades on
            #      Firefox Xray when sdk's fail path runs; works for many
            #      flows but not all sdk.js versions.
            #   3. QuickJS — no canvas/WebGL/audio → weak fingerprint.
            #   4. Python PoW — no fingerprint at all.
            sentinel = None
            if sidecar is not None:
                # K2's bootstrap_authorize_url needs ``auth_session_logging_id``
                # matching the page's ``oai-asli`` cookie. In pure_request mode
                # the curl jar has NO ``oai-asli`` yet (sentinel SDK that sets
                # it never ran on chatgpt.com — there's no JS engine). Read it
                # from the sidecar's BrowserContext instead, where it was set
                # during ``acquire_context``'s chatgpt.com load.
                k2_logging_id = ""
                try:
                    sc_cookies = sidecar.dump_cookies()
                    sc_cookie_names = sorted({
                        (c.get("name") or "").strip()
                        for c in (sc_cookies or ())
                        if c.get("name")
                    })
                    log(
                        f"[sentinel] K2 sidecar cookies "
                        f"(n={len(sc_cookies or ())}): {sc_cookie_names!r}"
                    )
                    for c in sc_cookies or ():
                        if (c.get("name") or "").strip() == "oai-asli":
                            k2_logging_id = (c.get("value") or "").strip()
                            if k2_logging_id:
                                break
                except Exception as exc:  # noqa: BLE001
                    log(f"[sentinel] K2 read oai-asli from sidecar failed: {exc}")
                # Curl jar fallback (browser mode imported it earlier).
                if not k2_logging_id:
                    try:
                        from _nextauth_bootstrap import (
                            read_oai_asli_from_session as _read_asli_for_k2,
                        )
                        k2_logging_id = _read_asli_for_k2(session) or ""
                    except Exception as exc:  # noqa: BLE001
                        log(f"[sentinel] K2 read oai-asli from session failed: {exc}")
                # Last resort: synthesize a UUID. Sentinel SDK doesn't seem to
                # set ``oai-asli`` on chatgpt.com in headless Camoufox (probably
                # because Sentinel's first action is /sentinel/req which fails
                # under Xray in our patched build). The server treats
                # ``auth_session_logging_id`` as a CLIENT-generated correlation
                # ID — it accepts whatever we send and echoes via cookie.
                # So generating one here is fine; the sidecar's signin/openai
                # response will then set ``oai-asli`` matching this value.
                if not k2_logging_id:
                    import uuid as _uuid
                    k2_logging_id = str(_uuid.uuid4())
                    log(
                        f"[sentinel] K2 synthesized auth_session_logging_id="
                        f"{k2_logging_id} (sidecar+jar both missing)"
                    )

                disable_k2 = os.getenv(
                    "OPENAI_SENTINEL_DISABLE_K2", "0",
                ).lower() in ("1", "true", "yes")

                if not disable_k2 and k2_logging_id:
                    try:
                        captured = sidecar.intercept_register_token(
                            email=request.email,
                            device_id=device_id,
                            logging_id=k2_logging_id,
                        )
                        if captured and captured.get("sentinel_token"):
                            # sentinel-token is bound to whatever device_id
                            # sdk.js SAW inside the sidecar's page (which is
                            # the sidecar's own ``oai-did`` cookie, not the
                            # caller's). After K2, we ALSO sync sidecar
                            # cookies (oai-did + oai-asli + oai-sc + ...)
                            # into the curl jar via ``_import_cookies_from_sidecar``,
                            # so the whole session shifts to sidecar's
                            # identity. The header ``oai-device-id`` must
                            # follow suit — ADOPT the captured device_id.
                            captured_did = (captured.get("device_id") or "").strip()
                            if captured_did and captured_did != device_id:
                                log(
                                    f"[sentinel] K2 adopting sidecar device_id="
                                    f"{captured_did} (was {device_id}) — session "
                                    f"shifts to sidecar identity"
                                )
                                device_id = captured_did
                            sentinel = captured["sentinel_token"]
                            log(
                                f"[sentinel] K2 page-form intercept OK "
                                f"(len={len(sentinel)} "
                                f"so_token={'yes' if captured.get('so_token') else 'no'} "
                                f"flow=username_password_create)"
                            )
                    except Exception as exc:  # noqa: BLE001
                        log(
                            f"[sentinel] K2 intercept_register_token error "
                            f"{type(exc).__name__}: {exc} — fallback get_token"
                        )
                elif disable_k2:
                    log("[sentinel] K2 skipped (OPENAI_SENTINEL_DISABLE_K2=1)")
                else:
                    log(
                        "[sentinel] K2 skipped — auth_session_logging_id "
                        "missing in jar (bootstrap may have failed)"
                    )

                # K2 failed or skipped → page.evaluate(sdk) fallback.
                if not sentinel:
                    try:
                        sentinel = sidecar.get_sentinel_token(
                            device_id=device_id, flow="username_password_create",
                        )
                        if sentinel:
                            log(
                                "[sentinel] page-native get_token OK "
                                "(flow=username_password_create)"
                            )
                    except Exception as exc:  # noqa: BLE001
                        log(
                            f"[sentinel] sidecar.get_sentinel_token error "
                            f"{type(exc).__name__}: {exc} — fallback QuickJS"
                        )
            if not sentinel:
                if sidecar is not None:
                    log(
                        "[sentinel] sidecar returned None — fallback QuickJS "
                        "(weak fingerprint; risk deferred ban)"
                    )
                sentinel = _get_sentinel_token(
                    session, device_id, "username_password_create", log, worker=worker,
                )

            # Inject sidecar cookies (oai-sc, _dd_s, oai-asli, oai-did, ...)
            # vào curl_cffi session jar TRƯỚC khi POST register. Browser thật
            # luôn có những cookie này khi request /register; pure HTTP không
            # tự gen được mà không chạy JS → import từ sidecar.
            if sidecar is not None:
                try:
                    _import_cookies_from_sidecar(
                        session, sidecar, log, caller_proxy=request.proxy,
                    )
                except Exception as exc:
                    log(f"[request] cookie import from sidecar failed (continue): {exc}")

            log("[request] [4/8] Registering account (password)...")
            reg_headers = _common_headers("https://auth.openai.com/create-account/password")
            reg_headers["Content-Type"] = "application/json"
            if sentinel:
                reg_headers["openai-sentinel-token"] = sentinel
            if device_id:
                reg_headers["oai-device-id"] = device_id

            resp = session.post(
                "https://auth.openai.com/api/accounts/user/register",
                headers=reg_headers,
                json={"password": password, "username": request.email},
                timeout=30,
            )

            if resp.status_code == 200:
                reg_data = resp.json() if resp is not None else {}
                reg_continue = (reg_data.get("continue_url") or "").strip()
                reg_page_type = ((reg_data.get("page") or {}).get("type") or "").strip()
                log(f"[request] Register OK → page_type={reg_page_type!r} continue_url={reg_continue[:80]!r}")
                # LƯU Ý: page_type=email_otp_verification sau user/register LÀ HỢP LỆ cho
                # account MỚI (server yêu cầu verify email vừa nhập). KHÔNG được coi là
                # "đã tồn tại". Signal duy nhất cho email đã đăng ký là HTTP 400 invalid_auth_step.
                break  # success → exit retry loop

            # 400 invalid_auth_step = email đã đăng ký rồi → fail-fast, KHÔNG retry
            if resp.status_code == 400 and "invalid_auth_step" in (resp.text or ""):
                raise RequestPhaseError(
                    f"email {request.email} đã được đăng ký (invalid_auth_step) "
                    f"— cần email mới để reg"
                )

            body = (resp.text or "")[:300]

            # 409 invalid_state = state machine desync → re-bootstrap fresh và retry
            if resp.status_code == 409 and "invalid_state" in body:
                log(
                    f"[request] user/register HTTP 409 invalid_state "
                    f"(lần {register_attempt}/{max_register_attempts}): {body[:200]}"
                )
                if register_attempt >= max_register_attempts:
                    raise RequestPhaseError(
                        f"user/register failed sau {max_register_attempts} lần retry "
                        f"với HTTP 409 invalid_state - {body}"
                    )
                # backoff ngắn để server clear state cũ trước khi bootstrap lại
                time.sleep(1.5)
                continue

            # Lỗi khác (5xx, 401, 422...) → fail-fast, không retry mù quáng
            raise RequestPhaseError(f"user/register failed: HTTP {resp.status_code} - {body}")

        # Random 2-4s sau khi register OK rồi mới send OTP (human-like — tránh
        # register → send OTP ngay trong cùng giây, fingerprint bot).
        _send_delay = random.uniform(2.0, 4.0)
        log(f"[request] chờ {_send_delay:.1f}s trước khi send OTP (human-like)")
        time.sleep(_send_delay)

        # Step 6: Send OTP
        log("[request] [5/8] Sending OTP...")
        otp_started_at = datetime.now(timezone.utc)

        if reg_continue and "/email-otp/send" in reg_continue:
            # Page navigate (Task 4.3) — server trả 302 → /email-verification HTML.
            otp_headers = _navigate_headers("https://auth.openai.com/email-verification")
            if device_id:
                otp_headers["oai-device-id"] = device_id
            resp = session.get(
                reg_continue, headers=otp_headers, timeout=30, allow_redirects=True,
            )
            if resp.status_code not in (200, 302):
                log(f"[request] OTP send via continue_url returned {resp.status_code}")
                _step_send_otp(session, device_id, log)
        else:
            _step_send_otp(session, device_id, log)
        log("[request] OTP sent")

        # ── Phase E (perf): pre-mint sentinel oauth_create_account ──
        # Khi sidecar khả dụng, spawn 1 thread nền mint sentinel + so cho
        # flow ``create_account`` song song với poll OTP (block ~15-30s).
        # Khi K2c fail sau OTP, fallback path dùng cache pre-mint thay vì
        # gọi sidecar.get_sentinel_token lại → tiết kiệm 3-6s/signup.
        #
        # Caveat: device_id có thể bị K2c "adopt sidecar's" sau OTP → cache
        # stale. Check device_id_unchanged khi dùng cache.
        # Env knob: HYBRID_PREMINT_DISABLED=1 để tắt (debug perf overhead).
        _premint_ca_cache: dict = {"token": None, "so": None}
        _premint_ca_thread = None
        _premint_ca_device_id = device_id
        _premint_disabled = os.getenv(
            "HYBRID_PREMINT_DISABLED", "0",
        ).lower() in ("1", "true", "yes")
        if sidecar is not None and not _premint_disabled:
            def _premint_create_account_sentinel():
                try:
                    log(
                        "[premint] start sentinel oauth_create_account "
                        "(background, song song poll OTP)"
                    )
                    t_premint = time.monotonic()
                    try:
                        tok = sidecar.get_sentinel_token(
                            device_id=_premint_ca_device_id,
                            flow="create_account",
                        )
                        if tok:
                            _premint_ca_cache["token"] = tok
                    except Exception as exc_t:
                        log(f"[premint] sentinel-token error: {exc_t}")
                    try:
                        so = sidecar.get_so_token(
                            device_id=_premint_ca_device_id,
                            flow="create_account",
                        )
                        if so:
                            _premint_ca_cache["so"] = so
                    except Exception as exc_so:
                        log(f"[premint] so-token error (non-fatal): {exc_so}")
                    log(
                        f"[premint] done in {time.monotonic() - t_premint:.2f}s "
                        f"(token={'OK' if _premint_ca_cache['token'] else 'MISS'} "
                        f"so={'OK' if _premint_ca_cache['so'] else 'MISS'})"
                    )
                except Exception as exc:  # noqa: BLE001 — best-effort
                    log(f"[premint] error (will fallback): {exc}")

            import threading as _threading
            _premint_ca_thread = _threading.Thread(
                target=_premint_create_account_sentinel,
                name="pure-request-premint-ca",
                daemon=True,
            )
            _premint_ca_thread.start()

        # Step 6: Poll OTP với mini-timeout + resend có giới hạn (mirror vòng poll
        # đầu của browser_phase). started_at = send time → chỉ nhận code về SAU thời
        # điểm này, loại code cũ cùng inbox. Dùng 1 event loop xuyên suốt vòng poll
        # đầu + các vòng retry verify. tried_codes/pending_codes chia sẻ toàn phase:
        # thử hết code đã biết trước khi resend.
        log("[request] [6/8] Waiting for OTP...")
        import asyncio as _asyncio
        _loop = _asyncio.new_event_loop()
        tried_codes: set[str] = set()
        pending_codes: list[str] = []
        # Tổng quota resend cho cả OTP phase. Account mới mailbox chỉ có 1 code:
        # code sai → phải resend mới có code mới. Cho tối đa 3 lần resend.
        total_resend_budget = 3
        resends_used = 0
        try:
            otp_code, used = _acquire_fresh_otp(
                session=session, device_id=device_id, mail_provider=mail_provider,
                request=request, log=log, loop=_loop, started_at=otp_started_at,
                tried_codes=tried_codes, pending=pending_codes,
                max_resends=total_resend_budget - resends_used,
                prefer_second_code=True,
            )
            resends_used += used

            # Đã LẤY ĐƯỢC OTP → báo watchdog gia hạn deadline (tránh kill ngay sau khi có OTP).
            if on_checkpoint is not None:
                try:
                    on_checkpoint("otp")
                    log("[request] OTP secured — watchdog gia hạn để hoàn tất")
                except Exception:
                    pass

            # Step 7: Verify OTP với retry. Wrong code → lấy code mới (pop pending dư
            # hoặc poll/resend qua _acquire_fresh_otp) rồi verify lại. Chỉ raise khi
            # hết lượt hoặc gặp lỗi không phải wrong-code. 1 code đầu + tối đa 3 code
            # mới từ 3 lần resend = 4 lần verify.
            max_verify_attempts = 1 + total_resend_budget
            verified = False
            _otp_recipient = request.source_email or request.email
            for v_attempt in range(1, max_verify_attempts + 1):
                # Human-like delay trước khi submit OTP — tránh verify ngay trong
                # cùng giây nhận code (fingerprint bot). 2-4s random mỗi lần thử.
                _verify_delay = random.uniform(2.0, 4.0)
                log(f"[request] chờ {_verify_delay:.1f}s trước khi submit OTP (human-like)")
                time.sleep(_verify_delay)

                # Trong lúc chờ, code mới hơn có thể vừa về (OpenAI gửi lại / mail
                # in-flight). Ưu tiên verify code mới nhất — tránh ăn 401 dư rồi mới
                # retry. Code hiện tại được giữ làm fallback trong pending.
                otp_code = _prefer_newest_untried_otp(
                    current=otp_code, mail_provider=mail_provider, loop=_loop,
                    recipient=_otp_recipient, started_at=otp_started_at,
                    tried_codes=tried_codes, pending=pending_codes, log=log,
                )
                tried_codes.add(otp_code)
                otp_resp = _step_verify_otp(
                    session, otp_code, device_id, log, raise_on_fail=False,
                )
                if otp_resp.get("_ok"):
                    verified = True
                    break

                status = otp_resp.get("_status")
                body = str(otp_resp.get("_body") or "")
                is_wrong_code = (
                    status == 401
                    or "wrong_email_otp_code" in body
                    or "wrong code" in body.lower()
                )
                if not is_wrong_code:
                    raise RequestPhaseError(
                        f"OTP verify failed: HTTP {status} - {body[:200]}"
                    )
                if v_attempt >= max_verify_attempts:
                    raise RequestPhaseError(
                        f"OTP verify vẫn sai sau {max_verify_attempts} lần "
                        f"(HTTP {status}) — code stale/không hợp lệ"
                    )

                # Wrong code → lấy code mới. _acquire_fresh_otp ưu tiên pop pending
                # (code dư đã fetch) trước, chỉ resend khi cạn code + còn quota.
                log(f"[request] OTP sai (lần {v_attempt}/{max_verify_attempts}) → lấy code mới")
                otp_code, used = _acquire_fresh_otp(
                    session=session, device_id=device_id, mail_provider=mail_provider,
                    request=request, log=log, loop=_loop, started_at=otp_started_at,
                    tried_codes=tried_codes, pending=pending_codes,
                    max_resends=total_resend_budget - resends_used,
                )
                resends_used += used
                if on_checkpoint is not None:
                    try:
                        on_checkpoint("otp")
                    except Exception:
                        pass

            if not verified:
                raise RequestPhaseError("OTP verify thất bại")
        finally:
            _loop.close()

        # Step 8: Create account (sentinel create_account + so-token if sidecar)
        #
        # Order (best → worst fingerprint quality), mirroring Step 5 (K2):
        #   1. K2c ``intercept_create_account_token`` — sidecar drives a
        #      fake /about-you submit, captures ``openai-sentinel-token``
        #      AND ``openai-sentinel-so-token`` from the live POST headers,
        #      aborts. Real Firefox canvas/audio + Observer activity from
        #      human-like typing → ``so`` field non-null, fingerprint
        #      matches a legitimate user.
        #   2. sidecar.get_sentinel_token / get_so_token via page.evaluate
        #      — degrades on Firefox Xray (``buildGenerateFailMessage``
        #      hits TypedArray cross-realm). Often returns None.
        #   3. QuickJS — no canvas/audio/Observer → weak fingerprint;
        #      so-token stays None entirely.
        ca_sentinel = None
        ca_so_token = None
        if sidecar is not None:
            disable_k2c = os.getenv(
                "OPENAI_SENTINEL_DISABLE_K2C", "0",
            ).lower() in ("1", "true", "yes")

            if not disable_k2c:
                # Dump caller's full curl jar — sidecar needs the
                # post-OTP auth state to access /about-you.
                curl_cookies: list[dict] = []
                try:
                    for c in session.cookies.jar:
                        curl_cookies.append({
                            "name": c.name,
                            "value": c.value,
                            "domain": getattr(c, "domain", "") or "",
                            "path": getattr(c, "path", "/") or "/",
                        })
                except Exception as exc:  # noqa: BLE001
                    log(f"[sentinel] K2c cookie jar dump failed: {exc}")

                if curl_cookies:
                    log(
                        f"[sentinel] K2c attempt — dumped {len(curl_cookies)} "
                        f"caller cookies, calling intercept_create_account_token..."
                    )
                    try:
                        captured = sidecar.intercept_create_account_token(
                            device_id=device_id,
                            name=request.name,
                            birthdate=request.birthdate,
                            caller_cookies=curl_cookies,
                        )
                        if captured and captured.get("sentinel_token"):
                            ca_sentinel = captured["sentinel_token"]
                            ca_so_token = captured.get("so_token")
                            # Adopt device_id if sidecar's flow saw a
                            # different one (mirror Step 5 logic).
                            captured_did = (
                                captured.get("device_id") or ""
                            ).strip()
                            if (
                                captured_did
                                and captured_did != device_id
                            ):
                                log(
                                    f"[sentinel] K2c adopting sidecar "
                                    f"device_id={captured_did} "
                                    f"(was {device_id})"
                                )
                                device_id = captured_did
                            log(
                                f"[sentinel] K2c create_account "
                                f"intercept OK "
                                f"(sentinel-len={len(ca_sentinel)} "
                                f"so_token="
                                f"{'yes' if ca_so_token else 'no'})"
                            )
                        else:
                            # K2c returned None (or no sentinel_token in
                            # the captured dict). Logging happens inside
                            # the method (e.g. "[sidecar.K2c] ..."); add a
                            # caller-side breadcrumb so the log shows that
                            # K2c WAS attempted but failed.
                            log(
                                "[sentinel] K2c returned None — see "
                                "[sidecar.K2c] logs above for reason; "
                                "fallback page.evaluate/QuickJS"
                            )
                    except Exception as exc:  # noqa: BLE001
                        log(
                            f"[sentinel] K2c intercept_create_account_"
                            f"token error {type(exc).__name__}: {exc} "
                            f"— fallback get_sentinel_token"
                        )
                else:
                    log("[sentinel] K2c skipped — curl jar empty (cannot sync)")
            else:
                log("[sentinel] K2c skipped (OPENAI_SENTINEL_DISABLE_K2C=1)")

            # K2c failed or skipped → page.evaluate(sdk) fallback for
            # sentinel-token (likely Xray-degraded but try anyway).
            # Phase E: ưu tiên cache pre-mint (thread đã chạy song song poll
            # OTP) khi device_id KHÔNG đổi giữa pre-mint và bây giờ. Tiết
            # kiệm 1 vòng page.evaluate (~3-6s).
            _device_id_unchanged = (device_id == _premint_ca_device_id)
            if not ca_sentinel:
                if (
                    _premint_ca_thread is not None
                    and _device_id_unchanged
                ):
                    _premint_ca_thread.join(timeout=5.0)
                    if _premint_ca_cache["token"]:
                        ca_sentinel = _premint_ca_cache["token"]
                        log(
                            "[sentinel] page-native cache HIT "
                            "(pre-minted oauth_create_account)"
                        )
                if not ca_sentinel:
                    try:
                        ca_sentinel = sidecar.get_sentinel_token(
                            device_id=device_id, flow="create_account",
                        )
                        if ca_sentinel:
                            log("[sentinel] page-native OK (flow=create_account)")
                    except Exception as exc:
                        log(
                            f"[sentinel] sidecar create_account error "
                            f"{type(exc).__name__}: {exc}"
                        )
            # If K2c didn't give us so-token, try cache then page.evaluate fallback
            # (also likely Xray-degraded but no harm trying).
            if not ca_so_token:
                if (
                    _premint_ca_thread is not None
                    and _device_id_unchanged
                    and _premint_ca_cache["so"]
                ):
                    ca_so_token = _premint_ca_cache["so"]
                    log(
                        "[sentinel] so-token cache HIT "
                        "(pre-minted oauth_create_account)"
                    )
                if not ca_so_token:
                    try:
                        ca_so_token = sidecar.get_so_token(
                            device_id=device_id, flow="create_account",
                        )
                        if ca_so_token:
                            log("[sentinel] so-token from sidecar OK (page.evaluate)")
                        else:
                            log(
                                "[sentinel] so-token NULL from sidecar "
                                "(Observer chưa đủ events?) — gửi /create_account "
                                "không có so-token (rủi ro flag)"
                            )
                    except Exception as exc:
                        log(
                            f"[sentinel] sidecar.get_so_token error "
                            f"{type(exc).__name__}: {exc}"
                        )

            # Refresh cookies từ sidecar (sentinel SDK có thể đã rotate oai-sc,
            # _dd_s sau khi gen token mới). Đảm bảo POST /create_account dùng
            # cookies state cuối cùng — khớp browser thật flow.
            try:
                _import_cookies_from_sidecar(
                    session, sidecar, log, caller_proxy=request.proxy,
                )
            except Exception as exc:
                log(f"[request] cookie refresh from sidecar failed: {exc}")

        continue_url = _step_create_account(
            session, request.name, request.birthdate, device_id, log,
            sentinel_token=ca_sentinel,
            worker=worker,
            so_token=ca_so_token,
        )

        # Step 9: Follow redirects + get session
        if not continue_url:
            raise RequestPhaseError("No continue_url after create_account")

        callback_url, final_url = _step_follow_redirects(session, continue_url, log)

        if callback_url:
            _consume_callback(session, callback_url, log)

        session_token, access_token, user_id = _get_session_tokens(session, log)

        if not session_token and not access_token:
            raise RequestPhaseError(
                "Registration completed but no session_token/access_token obtained"
            )

        # Extract all cookies for result
        cookies = []
        try:
            for cookie in session.cookies:
                name = getattr(cookie, "name", "") or ""
                value = getattr(cookie, "value", "") or ""
                domain = getattr(cookie, "domain", "") or ""
                if name and value:
                    cookies.append({
                        "name": name, "value": value,
                        "domain": domain, "path": "/", "secure": True,
                    })
        except Exception:
            pass

        # ── Inline 2FA enroll (CF-clean) — tái dùng session vừa pass CF ──
        # Session này vừa create_account thành công nên còn cf_clearance fresh
        # + đúng proxy/IP. Enroll ngay tại đây an toàn hơn spawn session mới.
        # NEVER fail registration vì account đã được tạo: lỗi enroll → để
        # caller fallback enable_2fa Phase 2.
        two_factor = None
        two_factor_partial = None
        if getattr(request, "mfa_inline", False) and access_token:
            from mfa_phase import MfaError, enable_2fa_in_session
            try:
                two_factor = enable_2fa_in_session(
                    session,
                    access_token=access_token,
                    user_agent=request.user_agent,
                    log=log,
                )
                log("[request] 2FA enrolled inline OK (CF-clean)")
            except MfaError as exc:
                partial = getattr(exc, "partial_state", None)
                if partial and partial.get("secret"):
                    two_factor_partial = partial
                    log(f"[request] 2FA inline: enroll OK nhưng activate fail → partial saved: {exc}")
                else:
                    log(f"[request] 2FA inline fail (fallback Phase 2): {exc}")
            except Exception as exc:
                log(f"[request] 2FA inline lỗi bất ngờ (fallback Phase 2): {exc}")

        return {
            "session_token": session_token,
            "access_token": access_token,
            "user_id": user_id,
            "password": password,
            "cookies": cookies,
            "device_id": device_id,
            "two_factor": two_factor,
            "two_factor_partial": two_factor_partial,
        }
    finally:
        try:
            session.close()
        except Exception:
            pass
        if worker is not None:
            try:
                worker.close()
            except Exception:
                pass


def _default_password(email: str) -> str:
    pwd = email.replace("@", "")
    if len(pwd) < 8:
        pwd = f"{pwd}2026OpenAI"
    return pwd


async def run_request_phase(
    *,
    request: SignupRequest,
    mail_provider: MailProvider,
    log: Callable = print,
    on_checkpoint: Callable | None = None,
) -> SignupResult:
    """Run pure-request registration. Returns SignupResult.

    on_checkpoint: callback(stage:str) — gọi khi đã lấy được OTP để watchdog
        bên ngoài gia hạn deadline (tránh kill job ngay sau khi có OTP).

    The sync core runs in a worker thread (asyncio.to_thread) and polls OTP
    inline via a fresh event loop, with started_at = exact OTP send time so
    stale codes from previous attempts are never picked up.

    Sidecar (Phase 10 hardening, opt-in via env):
        ``REG_SIDECAR_DISABLED=1`` → keep legacy QuickJS path (debug only).
        Default ON: spawn ``SentinelSidecar`` (headless Camoufox in daemon
        thread) for real sentinel-token + so-token + JS cookies. Eliminates
        the zero-fingerprint deferred-ban path for the pure-HTTP flow.
    """
    result = SignupResult(success=False, email=request.email)
    t_start = time.monotonic()

    sidecar = None
    sidecar_disabled = os.getenv("REG_SIDECAR_DISABLED", "0").lower() in (
        "1", "true", "yes",
    )
    if not sidecar_disabled:
        try:
            from sentinel_sidecar import SentinelSidecar
            # RAM optimization: ``SentinelSidecarPool`` shares a single
            # Camoufox process per (proxy, headless, os) key. If every
            # signup uses a UNIQUE upstream proxy, that's 1 Camoufox
            # per signup (~150MB each). ``SIDECAR_SHARED_PROXY`` env
            # overrides the per-signup proxy with a SHARED value so all
            # concurrent signups pool to one Camoufox process. Trade-off:
            # all sentinel-token traffic comes from one IP, but the
            # ACTUAL signup HTTP requests (via curl_cffi) still use
            # ``request.proxy`` so server still sees N different IPs
            # for N signups. Set to empty string ``""`` for direct
            # (no proxy on sidecar) — recommended for local dev.
            shared_proxy = os.getenv("SIDECAR_SHARED_PROXY")
            if shared_proxy is not None:
                # Explicit override (empty string = direct).
                sidecar_proxy = shared_proxy or None
                log(
                    f"[request] sidecar SHARED proxy="
                    f"{sidecar_proxy or 'direct'} (SIDECAR_SHARED_PROXY env "
                    f"override; pool keys all signups to one Camoufox)"
                )
            else:
                # Legacy behavior: sidecar uses caller's proxy →
                # one Camoufox per unique proxy.
                sidecar_proxy = request.proxy
            log("[request] starting sentinel sidecar (Camoufox headless)...")
            sidecar = SentinelSidecar(
                proxy=sidecar_proxy,
                headless=True,
                log=log,
            )
            sidecar.start(timeout=60.0)
            log("[request] sentinel sidecar ready")
        except Exception as exc:
            log(
                f"[request] sentinel sidecar FAILED to start "
                f"({type(exc).__name__}: {exc}) — fallback QuickJS "
                f"(zero-fingerprint risk; expect deferred ban)"
            )
            if sidecar is not None:
                try:
                    sidecar.close()
                except Exception:
                    pass
            sidecar = None
    else:
        log(
            "[request] REG_SIDECAR_DISABLED=1 — sidecar bypass "
            "(legacy QuickJS path; ZERO-FINGERPRINT bot risk)"
        )

    try:
        phase_result = await asyncio.to_thread(
            _run_request_phase_sync, request, mail_provider, log, on_checkpoint, sidecar,
        )

        result.success = True
        result.session_token = phase_result.get("session_token")
        result.access_token = phase_result.get("access_token")
        result.user_id = phase_result.get("user_id")
        result.password = phase_result.get("password") or request.password
        result.name = request.name
        result.cookies = phase_result.get("cookies", [])
        result.phase1_seconds = time.monotonic() - t_start
        result.phase2_seconds = 0.0  # No separate phase 2 in pure-request mode
        result.two_factor = phase_result.get("two_factor")
        result.two_factor_partial = phase_result.get("two_factor_partial")

        # Compute age
        try:
            y, m, d = request.birthdate.split("-")
            today = datetime.utcnow()
            result.age = today.year - int(y) - ((today.month, today.day) < (int(m), int(d)))
        except Exception:
            pass

        log(f"[request] Registration complete! session_token={'yes' if result.session_token else 'no'} "
            f"access_token={'yes' if result.access_token else 'no'}")

    except RequestPhaseError as e:
        result.error = f"RequestPhaseError: {e}"
        log(f"[request] FAILED: {result.error}")
    except TimeoutError as e:
        result.error = f"TimeoutError: {e}"
        log(f"[request] TIMEOUT: {result.error}")
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        log(f"[request] ERROR: {result.error}")
    finally:
        total = time.monotonic() - t_start
        log(f"[request] Total time: {total:.2f}s")
        if sidecar is not None:
            try:
                sidecar.close()
                log("[request] sentinel sidecar closed")
            except Exception as exc:
                log(f"[request] sidecar.close exception: {exc}")

    return result
