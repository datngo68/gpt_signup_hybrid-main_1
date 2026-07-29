"""Enable 2FA (TOTP) cho account đã đăng ký — kiến trúc fail-safe.

Flow theo HAR:
    1. POST /backend-api/accounts/mfa/enroll body {"factor_type":"totp"}
       → trả {secret, session_id, factor: {id, factor_type:"totp", ...}}
    2. POST /backend-api/accounts/mfa/user/activate_enrollment
       body {"factor_id":..., "session_id":..., "code":"<6-digit TOTP>"}
       → confirm enrollment, mfa_enabled=true.

Secret base32 tương thích Google Authenticator. Lưu để gen code mỗi lần login.

KIẾN TRÚC FAIL-SAFE
-------------------
Lỗi cũ: enroll OK → activate OK → mfa_info timeout → caller raise → MẤT secret
       → retry-2fa enroll lại → server đã có active factor → fail vô hạn.

Fix bằng 4 cơ chế:

A. ``on_enroll`` callback: persist secret NGAY sau enroll OK (trước activate).
   Activate fail/timeout sau đó vẫn không mất secret.

B. ``pending_enrollment`` argument: caller có thể pass secret/factor_id/session_id
   từ DB → bỏ qua enroll, đi thẳng activate. Idempotent với mọi retry.

C. Idempotent activate: error chứa ``already`` / ``active`` / ``enabled``
   → coi như success. Server-side đã enable rồi.

D. ``MfaError.partial_state``: khi enroll xong nhưng activate fail, exception
   mang theo state để caller persist + retry không cần enroll lại.

E. ``mfa_info`` đổi thành verify nhẹ: 1 attempt × 10s timeout, mọi lỗi → ``{}``,
   tuyệt đối không raise. Activate 200 đã đủ xác nhận 2FA bật.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

from curl_cffi.requests import AsyncSession

from totp_helper import generate_code, normalize_secret


_BASE = "https://chatgpt.com/backend-api"

# Account mới create_account → server-side cần thời gian để propagate sang
# /backend-api. Retry với backoff cho cả enroll + activate.
_HTTP_TIMEOUT = 60.0
_MAX_ATTEMPTS = 4
_BACKOFF_SECONDS = (3.0, 6.0, 10.0)  # delay sau attempt 1, 2, 3

# Outer retry backoff khi gặp CF challenge / 403. CF rate-limit thường giữ
# 30s–5min, backoff ngắn (3s) sẽ cháy hết retry budget mà vẫn bị block.
_BACKOFF_CF_SECONDS = (15.0, 30.0, 60.0)

# mfa_info: optional verify — KHÔNG ảnh hưởng tới quyết định activated=True.
_MFA_INFO_TIMEOUT = 10.0

# Markers cho idempotent activate — server đã enable factor rồi
_ACTIVATE_IDEMPOTENT_MARKERS = (
    "already",
    "active",
    "enabled",
    "duplicate",
    "exists",
)

# Markers cho enroll khi server đã có active factor — ưu tiên dùng pending state
_ENROLL_CONFLICT_MARKERS = (
    "already",
    "exists",
    "active",
    "enrolled",
    "duplicate",
)

# Markers cho Cloudflare challenge / WAF block — thường HTTP 403/503 + body HTML
# có chứa các pattern này. Khi match → refresh CF cookies + retry với backoff dài.
_CF_CHALLENGE_MARKERS = (
    "<html",
    "cloudflare",
    "cf-mitigated",
    "cf-ray",
    "challenge-platform",
    "just a moment",
    "attention required",
    "enable javascript and cookies",
)

# Cookies bắt buộc inject vào curl_cffi session để bypass CF khi gọi /backend-api.
# Khớp đúng tên CF cookies + session cookies chatgpt.com (xem browser_phase.py).
_BACKEND_DOMAINS = ("chatgpt.com", "openai.com")


# Kiểu callback persist khi đã enroll xong (chưa activate)
EnrollPersistCallback = Callable[[dict[str, Any]], Awaitable[None]]


class MfaError(Exception):
    """MFA enable fail.

    ``partial_state`` chứa ``{secret, factor_id, session_id}`` khi enroll đã
    thành công nhưng activate fail — caller có thể persist + retry với
    ``pending_enrollment`` mà không phải enroll lại.
    """

    def __init__(self, message: str, *, partial_state: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.partial_state = partial_state


def _inject_session_cookies(
    session: AsyncSession,
    cookies: list[dict[str, Any]] | None,
    *,
    log,
) -> int:
    """Inject session cookies cho chatgpt.com + openai.com vào curl_cffi session.

    Cookies này gồm CF cookies (``cf_clearance``, ``__cf_bm``) đã pass CF
    challenge từ browser context. Không inject → POST /mfa/enroll lần đầu sẽ
    bị CF block 403 vì curl_cffi session không có CF cookies.

    Returns:
        Số cookie đã inject thành công.
    """
    if not cookies:
        return 0
    count = 0
    cf_count = 0
    for c in cookies:
        try:
            domain_raw = c.get("domain") or ""
            domain = domain_raw.lstrip(".").lower()
            if not domain:
                continue
            if not any(d in domain for d in _BACKEND_DOMAINS):
                continue
            session.cookies.set(
                c["name"], c["value"],
                domain=domain_raw or domain,
                path=c.get("path") or "/",
            )
            count += 1
            if c["name"] in ("cf_clearance", "__cf_bm"):
                cf_count += 1
        except Exception as exc:
            log(f"[mfa] inject cookie {c.get('name')!r} failed: {exc}")
    log(f"[mfa] injected {count} cookies (cf_clearance/__cf_bm: {cf_count})")
    return count


def _is_cf_challenge(status_code: int, body_text: str, headers) -> bool:
    """Detect Cloudflare challenge / WAF block.

    HTTP 403/503 với body HTML chứa CF markers → CF block. Cần refresh CF
    cookies (qua /api/auth/session) trước khi retry, không retry naive.
    """
    if status_code not in (403, 429, 503):
        return False
    body_lower = (body_text or "").lower()
    if any(m in body_lower for m in _CF_CHALLENGE_MARKERS):
        return True
    # Header CF-Mitigated được set khi CF action = challenge/block
    try:
        cf_mitigated = (headers.get("cf-mitigated") or "").lower() if headers else ""
        if cf_mitigated:
            return True
    except Exception:
        pass
    return False


async def _refresh_access_token(
    session: AsyncSession, *, cookies: list[dict[str, Any]] | None, log,
) -> str | None:
    """Gọi /api/auth/session với session cookies để lấy access_token mới.

    Side effect: response Set-Cookie từ chatgpt.com (gồm ``__cf_bm`` mới) sẽ
    được curl_cffi tự động lưu vào session jar — refresh CF cookies song hành
    với việc lấy access_token mới.
    """
    # Re-inject cookies trong trường hợp jar bị clear hoặc caller pass cookies
    # mới — idempotent, không hại nếu đã inject từ trước.
    if cookies:
        _inject_session_cookies(session, cookies, log=log)
    url = "https://chatgpt.com/api/auth/session"
    try:
        r = await session.get(url, timeout=30)
        if r.status_code != 200:
            log(f"[mfa] refresh token: HTTP {r.status_code}")
            return None
        data = r.json()
        token = data.get("accessToken")
        if token:
            log("[mfa] access_token + CF cookies refreshed OK")
        else:
            log("[mfa] refresh response missing accessToken")
        return token
    except Exception as exc:
        log(f"[mfa] refresh token error: {exc}")
        return None


async def _post_with_retry(
    session: AsyncSession, *, url: str, headers: dict, body: dict, log, label: str,
):
    """POST với retry exponential backoff khi timeout/5xx/connection error."""
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            r = await session.post(
                url, headers=headers, data=json.dumps(body), timeout=_HTTP_TIMEOUT,
            )
            if r.status_code in (502, 503, 504):
                log(f"[mfa] {label} HTTP {r.status_code} attempt {attempt} — retry")
                last_exc = MfaError(f"{label} HTTP {r.status_code}")
            else:
                return r
        except Exception as exc:
            last_exc = exc
            log(f"[mfa] {label} attempt {attempt} error: {exc}")

        if attempt < _MAX_ATTEMPTS:
            backoff = _BACKOFF_SECONDS[min(attempt - 1, len(_BACKOFF_SECONDS) - 1)]
            log(f"[mfa] retry in {backoff:.0f}s...")
            await asyncio.sleep(backoff)

    raise MfaError(f"{label} failed sau {_MAX_ATTEMPTS} attempts: {last_exc}")


async def _enroll_totp(
    session: AsyncSession, *, access_token: str, cookies: list[dict[str, Any]] | None, log,
) -> tuple[dict[str, Any], str]:
    """POST /mfa/enroll → trả (enroll_data, access_token_used).

    Xử lý các response error:
      - 401 token_revoked + có cookies → refresh access_token rồi retry.
      - 403/429/503 + body HTML (CF challenge) → refresh CF cookies + token
        qua /api/auth/session rồi retry. Sau retry vẫn fail → raise để outer
        retry áp dụng backoff CF dài (15/30/60s).
    """
    url = f"{_BASE}/accounts/mfa/enroll"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    log("[mfa] POST mfa/enroll factor_type=totp")
    r = await _post_with_retry(
        session, url=url, headers=headers, body={"factor_type": "totp"},
        log=log, label="enroll",
    )

    # 401 token_revoked → refresh access_token rồi retry 1 lần
    if r.status_code == 401 and cookies:
        body_text = r.text[:500]
        if "token_revoked" in body_text or "invalidated" in body_text:
            log("[mfa] token revoked — refreshing access_token...")
            new_token = await _refresh_access_token(session, cookies=cookies, log=log)
            if new_token:
                access_token = new_token
                headers["Authorization"] = f"Bearer {access_token}"
                await asyncio.sleep(2.0)  # small delay cho server propagate
                r = await _post_with_retry(
                    session, url=url, headers=headers, body={"factor_type": "totp"},
                    log=log, label="enroll-retry",
                )
            else:
                raise MfaError(f"enroll failed HTTP 401 + refresh failed: {body_text}")

    # 403/429/503 + CF challenge → refresh CF cookies + token rồi retry 1 lần.
    # Đây là root cause hay gặp: account vừa create, /backend-api lần đầu bị
    # CF intercept vì JWT chưa propagate. Refresh /api/auth/session để CF set
    # __cf_bm mới và lấy access_token đã propagate.
    body_text_initial = r.text[:1500] if hasattr(r, "text") else ""
    if _is_cf_challenge(r.status_code, body_text_initial, getattr(r, "headers", None)):
        if not cookies:
            # Fail-fast: không có cookies để refresh CF → raise NGAY, không in
            # log gây hiểu lầm "refreshing" rồi mới phát hiện thiếu cookies.
            raise MfaError(
                f"enroll CF challenge HTTP {r.status_code} but no cookies passed "
                f"để refresh — caller phải truyền cookies (body: {body_text_initial[:200]})"
            )
        log(
            f"[mfa] CF challenge HTTP {r.status_code} (body_len={len(body_text_initial)}) "
            f"— refreshing token + CF cookies"
        )
        new_token = await _refresh_access_token(session, cookies=cookies, log=log)
        if new_token:
            access_token = new_token
            headers["Authorization"] = f"Bearer {access_token}"
            await asyncio.sleep(3.0)  # cho CF propagate cookie mới
            r = await _post_with_retry(
                session, url=url, headers=headers, body={"factor_type": "totp"},
                log=log, label="enroll-cf-retry",
            )
        else:
            raise MfaError(
                f"enroll CF challenge HTTP {r.status_code} + refresh token failed "
                f"(body: {body_text_initial[:200]})"
            )

    if r.status_code != 200:
        body_text = r.text[:300] if hasattr(r, "text") else ""
        raise MfaError(f"enroll failed HTTP {r.status_code}: {body_text}")
    data = r.json()
    if "secret" not in data:
        raise MfaError(f"enroll response missing secret: {data}")
    log(f"[mfa] enroll OK factor_id={data.get('factor', {}).get('id', '?')[:20]} secret_len={len(data['secret'])}")
    return data, access_token


async def _enroll_totp_with_retry(
    session: AsyncSession,
    *,
    access_token: str,
    cookies: list[dict[str, Any]] | None,
    log,
    max_attempts: int = 3,
) -> tuple[dict[str, Any], str]:
    """Wrap ``_enroll_totp`` với outer retry — toàn bộ enroll fail → retry lại.

    Phân biệt 3 loại lỗi:
      - **Conflict** (server đã có active factor) → KHÔNG retry, propagate ngay
        để caller dùng pending_enrollment / Get Session flow.
      - **CF challenge** (HTTP 403/429/503 + body HTML) → retry với backoff dài
        (15/30/60s) vì CF rate-limit thường giữ ≥30s. Backoff ngắn (3s) sẽ
        cháy retry budget mà vẫn bị block.
      - **Lỗi khác** (HTTP 4xx ngoài conflict, 5xx, network, timeout) → retry
        backoff thường (3/6/10s).

    Khác với ``_post_with_retry`` (chỉ retry 5xx + transient ở mức HTTP), wrapper
    này retry ở mức **logical operation** — bao gồm cả refresh-token, response
    parse, secret-validation. Mỗi attempt là một enroll hoàn chỉnh độc lập.
    """
    last_exc: MfaError | None = None
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            log(f"[mfa] enroll retry {attempt}/{max_attempts}")
        try:
            return await _enroll_totp(
                session, access_token=access_token, cookies=cookies, log=log,
            )
        except MfaError as exc:
            msg = str(exc).lower()
            # Conflict: server đã có active factor → fail-fast, không retry
            if any(m in msg for m in _ENROLL_CONFLICT_MARKERS):
                raise
            last_exc = exc
            log(f"[mfa] enroll attempt {attempt}/{max_attempts} failed: {exc}")
            if attempt < max_attempts:
                # CF challenge (403/429/503 với body HTML) → backoff dài hơn.
                # Detect qua message: chứa "HTTP 403/429/503" + CF marker.
                is_cf = (
                    ("http 403" in msg or "http 429" in msg or "http 503" in msg
                     or "cf challenge" in msg)
                    and any(m in msg for m in _CF_CHALLENGE_MARKERS)
                )
                backoff_table = _BACKOFF_CF_SECONDS if is_cf else _BACKOFF_SECONDS
                backoff = backoff_table[
                    min(attempt - 1, len(backoff_table) - 1)
                ]
                tag = "CF cooldown" if is_cf else "retry"
                log(f"[mfa] enroll {tag} trong {backoff:.0f}s...")
                await asyncio.sleep(backoff)

    # Hết số lần retry — propagate lỗi cuối cùng
    raise MfaError(
        f"enroll failed sau {max_attempts} lần retry: {last_exc}"
    ) from last_exc


def _is_activate_idempotent_response(status_code: int, body_text: str) -> bool:
    """True nếu activate response cho biết factor đã ở trạng thái active.

    Cases:
      - HTTP 200 mà body chứa "already" / "active" → idempotent OK.
      - HTTP 4xx (400/409/422) mà body chứa marker idempotent → coi như đã active.
    """
    text_lower = (body_text or "").lower()
    has_marker = any(m in text_lower for m in _ACTIVATE_IDEMPOTENT_MARKERS)
    if not has_marker:
        return False
    # Chỉ nhận idempotent với status có ý nghĩa (200 OK hoặc 4xx conflict).
    # 5xx + marker = noise, không tin được.
    return status_code == 200 or 400 <= status_code < 500


async def _activate_enrollment(
    session: AsyncSession,
    *,
    access_token: str,
    factor_id: str,
    session_id: str,
    code: str,
    log,
) -> tuple[dict[str, Any], bool]:
    """POST /mfa/user/activate_enrollment để confirm 6-digit TOTP.

    Returns: (response_body, idempotent_flag).
        idempotent_flag=True khi server cho biết factor đã active (skip activate
        nhưng coi như success).
    """
    url = f"{_BASE}/accounts/mfa/user/activate_enrollment"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    body = {
        "factor_id": factor_id,
        "factor_type": "totp",
        "session_id": session_id,
        "code": code,
    }
    log(f"[mfa] POST activate_enrollment factor_id={factor_id[:20]} code={code}")
    r = await _post_with_retry(
        session, url=url, headers=headers, body=body, log=log, label="activate",
    )
    body_text = r.text[:500] if hasattr(r, "text") else ""

    if r.status_code == 200:
        try:
            data = r.json()
        except Exception:
            data = {}
        log("[mfa] activate OK")
        return data, False

    # Idempotent: factor đã active từ trước (vd: retry sau activate đã OK ngầm)
    if _is_activate_idempotent_response(r.status_code, body_text):
        log(f"[mfa] activate HTTP {r.status_code} idempotent — factor đã active: {body_text[:120]}")
        return {}, True

    raise MfaError(
        f"activate failed HTTP {r.status_code}: {body_text[:300]}",
        partial_state={
            "secret": None,  # secret được fill bởi caller (đã có sẵn)
            "factor_id": factor_id,
            "session_id": session_id,
        },
    )


async def _check_mfa_info(session: AsyncSession, *, access_token: str, log) -> dict[str, Any]:
    """GET /mfa_info — verify-only, fire-and-forget.

    Activate 200 = đã enable server-side. mfa_info chỉ để log info bổ sung.
    1 attempt × 10s timeout. Mọi exception/non-200 → ``{}``, không raise.
    """
    url = f"{_BASE}/accounts/mfa_info"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        r = await session.get(url, headers=headers, timeout=_MFA_INFO_TIMEOUT)
        if r.status_code != 200:
            log(f"[mfa] mfa_info HTTP {r.status_code} — ignored (activate đã OK)")
            return {}
        return r.json()
    except Exception as exc:
        log(f"[mfa] mfa_info skipped ({type(exc).__name__}: {exc}) — activate đã OK")
        return {}


async def enable_2fa(
    *,
    access_token: str,
    cookies: list[dict[str, Any]] | None = None,
    user_agent: str | None = None,
    impersonate: str | None = None,
    proxy: str | None = None,
    activate: bool = True,
    pending_enrollment: dict[str, Any] | None = None,
    on_enroll: EnrollPersistCallback | None = None,
    log=print,
) -> dict[str, Any]:
    """Enable 2FA TOTP cho account hiện tại.

    Args:
        access_token: Bearer JWT của account (lấy từ SignupResult).
        cookies: Session cookies chatgpt.com — dùng để refresh token nếu bị revoke.
        user_agent: UA cho curl_cffi. None → dùng ``WINDOWS_USER_AGENT`` từ
            user_agent_profile (đồng bộ với reg flow).
        impersonate: TLS fingerprint preset của curl_cffi. None → dùng
            ``CURL_IMPERSONATE_PRIMARY`` (chrome145) — KHÔNG dùng firefox135 nữa
            vì làm UA(Chrome) ↔ TLS(Firefox) mismatch.
        proxy: HTTP/HTTPS proxy.
        activate: True = gọi activate_enrollment với code TOTP đầu tiên (bật 2FA luôn).
                  False = chỉ enroll, lưu secret để mày confirm sau.
        pending_enrollment: Dict ``{secret, factor_id, session_id}`` từ lần enroll
            trước (đã persist). Nếu có → BỎ QUA enroll, đi thẳng activate.
            Tránh enroll loop khi server đã có active factor.
        on_enroll: Async callback nhận ``{secret, factor_id, session_id, status}``
            sau khi enroll OK (TRƯỚC activate). Caller persist vào DB tại đây để
            activate fail không mất secret. Best-effort: callback raise → log
            warning nhưng vẫn tiếp tục activate.
        log: callable.

    Returns:
        {
            "secret": "B2P3OQCCXINLHGPUDIS55DHQDW5MENK5",
            "factor_id": "6a0beb...",
            "session_id": "6a0beb...",
            "provisioning_uri": "otpauth://totp/...",
            "first_code": "763657",  # code TOTP gen từ secret tại t=now
            "activated": True / False,
            "mfa_info": {...}  # nếu activate=True, response /mfa_info sau khi enable
        }

    Raises:
        MfaError: Nếu enroll/activate fail. ``partial_state`` (nếu có) chứa
            secret/factor_id/session_id để caller persist + retry sau.
    """
    from user_agent_profile import (
        CURL_IMPERSONATE_PRIMARY,
        SEC_CH_UA,
        SEC_CH_UA_MOBILE,
        SEC_CH_UA_PLATFORM,
        WINDOWS_USER_AGENT,
    )

    if user_agent is None:
        user_agent = WINDOWS_USER_AGENT
    if impersonate is None:
        impersonate = CURL_IMPERSONATE_PRIMARY

    proxies = {"http": proxy, "https": proxy} if proxy else None
    base_headers = {
        "User-Agent": user_agent,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": SEC_CH_UA_PLATFORM,
        # Sec-Fetch hints — browser thật luôn gửi 3 header này khi POST
        # /backend-api từ origin chatgpt.com. Thiếu → CF/WAF coi là bot.
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    async with AsyncSession(impersonate=impersonate, proxies=proxies, headers=base_headers) as session:
        # ── BẮT BUỘC: inject CF cookies + session cookies vào curl_cffi session
        # NGAY khi tạo, trước khi POST /mfa/enroll. Lý do: browser context đã
        # pass CF challenge và có ``cf_clearance`` + ``__cf_bm``. Nếu curl_cffi
        # session khởi tạo trắng thì lần POST đầu tiên sẽ bị CF block 403 với
        # body HTML. Đây là root cause của "enroll failed HTTP 403: <html...".
        if cookies:
            _inject_session_cookies(session, cookies, log=log)

        # ── Phase 1: secure secret ──
        # Ưu tiên pending_enrollment từ caller (DB) → bỏ qua enroll.
        # Nếu enroll lần này conflict (server đã có active factor) → đẩy lỗi
        # để caller phát hiện account đã 2FA enabled từ trước.
        active_token = access_token
        if pending_enrollment and pending_enrollment.get("secret"):
            secret = normalize_secret(pending_enrollment["secret"])
            factor_id = pending_enrollment["factor_id"]
            enroll_session_id = pending_enrollment["session_id"]
            log(
                f"[mfa] reuse pending enrollment factor_id={factor_id[:20]} "
                f"secret_len={len(secret)} (skip enroll)"
            )
        else:
            try:
                enroll, active_token = await _enroll_totp_with_retry(
                    session, access_token=access_token, cookies=cookies, log=log,
                    max_attempts=3,
                )
            except MfaError as exc:
                # Detect enroll conflict — server đã có active factor.
                # Không có pending để fallback → fail-fast, caller phải dùng
                # luồng "Get Session" với secret đã biết.
                msg = str(exc).lower()
                if any(m in msg for m in _ENROLL_CONFLICT_MARKERS):
                    raise MfaError(
                        f"enroll conflict — account đã có 2FA active server-side. "
                        f"Caller phải dùng pending_enrollment (DB) hoặc Get Session "
                        f"flow với secret cũ. Original: {exc}"
                    ) from exc
                raise

            secret = normalize_secret(enroll["secret"])
            factor_id = enroll["factor"]["id"]
            enroll_session_id = enroll["session_id"]

            # ── Persist NGAY (callback) — activate fail vẫn không mất secret ──
            if on_enroll is not None:
                try:
                    await on_enroll({
                        "secret": secret,
                        "factor_id": factor_id,
                        "session_id": enroll_session_id,
                        "status": "enrolled",
                    })
                except Exception as exc_persist:
                    # Best-effort: log warning, vẫn tiếp tục activate.
                    # Caller nên đảm bảo on_enroll an toàn (write atomic).
                    log(f"[mfa] WARN on_enroll callback raised: {exc_persist}")

        first_code = generate_code(secret)
        result: dict[str, Any] = {
            "secret": secret,
            "factor_id": factor_id,
            "session_id": enroll_session_id,
            "provisioning_uri": f"otpauth://totp/ChatGPT?secret={secret}&issuer=ChatGPT",
            "first_code": first_code,
            "activated": False,
            "mfa_info": None,
        }

        if not activate:
            return result

        # ── Phase 2: activate ──
        try:
            _data, idempotent = await _activate_enrollment(
                session,
                access_token=active_token,
                factor_id=factor_id,
                session_id=enroll_session_id,
                code=first_code,
                log=log,
            )
        except MfaError as exc:
            # Bổ sung secret vào partial_state để caller persist đầy đủ
            if exc.partial_state is not None:
                exc.partial_state["secret"] = secret
            else:
                exc.partial_state = {
                    "secret": secret,
                    "factor_id": factor_id,
                    "session_id": enroll_session_id,
                }
            raise

        result["activated"] = True
        if idempotent:
            log("[mfa] activate idempotent — factor đã active từ trước, skip mfa_info")
            result["mfa_info"] = {}
        else:
            result["mfa_info"] = await _check_mfa_info(session, access_token=active_token, log=log)
        return result


# ═══════════════════════════════════════════════════════════════════════
# INLINE ENROLL — enroll 2FA NGAY trong context vừa tạo account (CF-clean)
# ═══════════════════════════════════════════════════════════════════════
#
# Root cause của bug "mất acc": enable_2fa cũ luôn mở curl_cffi session MỚI
# SAU khi browser/session gốc đã chết → mất CF clearance, sai IP, fingerprint
# xấp xỉ → Cloudflare 403 → enroll fail vĩnh viễn → account đã create nhưng
# không bật được 2FA và không retry signup được nữa.
#
# Giải pháp: enroll bằng CHÍNH context vừa pass CF:
#   - browser mode  → ``enable_2fa_in_page``  (fetch same-origin trong page).
#   - pure_request  → ``enable_2fa_in_session`` (tái dùng curl session sống).
#
# Cả 2 chia sẻ logic phân loại response + build result + idempotent/conflict
# markers với fallback ``enable_2fa``. Khác biệt duy nhất là transport HTTP.


def _classify_enroll_response(status_code: int, body_text: str) -> tuple[str, Any]:
    """Phân loại response của POST mfa/enroll.

    Returns ``(kind, payload)``:
      - ``("ok", enroll_dict)``    — 200 + có ``secret``.
      - ``("conflict", msg)``      — server đã có active factor (không retry).
      - ``("error", msg)``         — lỗi khác (retry được).
    """
    if status_code == 200:
        try:
            data = json.loads(body_text)
        except Exception:
            return ("error", f"200 nhưng body không phải JSON: {body_text[:200]}")
        if isinstance(data, dict) and "secret" in data:
            return ("ok", data)
        return ("error", f"200 thiếu secret: {body_text[:200]}")
    low = (body_text or "").lower()
    if any(m in low for m in _ENROLL_CONFLICT_MARKERS):
        return ("conflict", body_text[:200])
    return ("error", f"HTTP {status_code}: {body_text[:200]}")


def _build_mfa_result(
    *, secret: str, factor_id: str, session_id: str, first_code: str,
    activated: bool, mfa_info: dict[str, Any] | None,
) -> dict[str, Any]:
    """Result dict thống nhất với ``enable_2fa`` (cùng schema downstream)."""
    return {
        "secret": secret,
        "factor_id": factor_id,
        "session_id": session_id,
        "provisioning_uri": f"otpauth://totp/ChatGPT?secret={secret}&issuer=ChatGPT",
        "first_code": first_code,
        "activated": activated,
        "mfa_info": mfa_info,
    }


# ── Browser transport (page.evaluate fetch, same-origin chatgpt.com) ──

_PAGE_FETCH_JS = """
async ({method, path, body, token}) => {
  const headers = {'Content-Type': 'application/json'};
  if (token) headers['Authorization'] = 'Bearer ' + token;
  const opt = {method: method, headers: headers, credentials: 'include'};
  if (body !== null && body !== undefined) opt.body = JSON.stringify(body);
  const resp = await fetch(path, opt);
  const text = await resp.text();
  return {status: resp.status, text: text};
}
"""


async def _page_request(
    page, *, method: str, path: str, body: dict | None, token: str | None,
) -> tuple[int, str]:
    """Gọi fetch() trong page context → (status, text)."""
    res = await page.evaluate(
        _PAGE_FETCH_JS,
        {"method": method, "path": path, "body": body, "token": token},
    )
    return int(res["status"]), str(res.get("text") or "")


async def _page_access_token(page, *, log) -> str | None:
    """Lấy accessToken qua /api/auth/session trong page (CF-clean)."""
    try:
        status, text = await _page_request(
            page, method="GET", path="/api/auth/session", body=None, token=None,
        )
        if status != 200:
            log(f"[mfa-inline] /api/auth/session HTTP {status}")
            return None
        data = json.loads(text)
        token = data.get("accessToken")
        if not token:
            log("[mfa-inline] /api/auth/session thiếu accessToken")
        return token
    except Exception as exc:
        log(f"[mfa-inline] lấy accessToken lỗi: {exc}")
        return None


async def enable_2fa_in_page(
    page,
    *,
    activate: bool = True,
    log=print,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Enroll + activate 2FA bằng chính ``page`` browser đang login.

    Page đã pass Cloudflare challenge, giữ ``cf_clearance``/``__cf_bm`` hợp lệ,
    TLS/JA3 fingerprint thật và đúng IP → fetch same-origin tới /backend-api
    gần như không thể bị CF 403 (khác hẳn curl_cffi spawn sau).

    Raises:
        MfaError: enroll/activate fail. ``partial_state`` mang secret/factor_id/
            session_id khi enroll OK nhưng activate fail (caller persist + retry
            activate-only, không enroll lại).
    """
    access_token = await _page_access_token(page, log=log)
    if not access_token:
        raise MfaError("enroll-in-page: không lấy được accessToken từ page")

    # ── enroll (retry nhẹ — CF hiếm khi block trong page) ──
    enroll_data: dict[str, Any] | None = None
    last_err = ""
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            log(f"[mfa-inline] enroll retry {attempt}/{max_attempts}")
        try:
            status, text = await _page_request(
                page, method="POST", path="/backend-api/accounts/mfa/enroll",
                body={"factor_type": "totp"}, token=access_token,
            )
        except Exception as exc:
            last_err = f"page.evaluate lỗi: {exc}"
            log(f"[mfa-inline] enroll attempt {attempt}/{max_attempts} {last_err}")
            if attempt < max_attempts:
                await asyncio.sleep(_BACKOFF_SECONDS[min(attempt - 1, len(_BACKOFF_SECONDS) - 1)])
            continue

        kind, payload = _classify_enroll_response(status, text)
        if kind == "ok":
            enroll_data = payload
            break
        if kind == "conflict":
            raise MfaError(
                f"enroll-in-page conflict — account đã có 2FA active server-side: {payload}"
            )
        last_err = str(payload)
        log(f"[mfa-inline] enroll attempt {attempt}/{max_attempts} failed: {last_err}")
        if attempt < max_attempts:
            await asyncio.sleep(_BACKOFF_SECONDS[min(attempt - 1, len(_BACKOFF_SECONDS) - 1)])

    if enroll_data is None:
        raise MfaError(f"enroll-in-page failed sau {max_attempts} lần: {last_err}")

    secret = normalize_secret(enroll_data["secret"])
    factor_id = enroll_data["factor"]["id"]
    session_id = enroll_data["session_id"]
    first_code = generate_code(secret)
    log(f"[mfa-inline] enroll OK factor_id={factor_id[:20]} secret_len={len(secret)}")

    if not activate:
        return _build_mfa_result(
            secret=secret, factor_id=factor_id, session_id=session_id,
            first_code=first_code, activated=False, mfa_info=None,
        )

    # ── activate ──
    try:
        status, text = await _page_request(
            page, method="POST",
            path="/backend-api/accounts/mfa/user/activate_enrollment",
            body={
                "factor_id": factor_id, "factor_type": "totp",
                "session_id": session_id, "code": first_code,
            },
            token=access_token,
        )
    except Exception as exc:
        raise MfaError(
            f"activate-in-page transport lỗi: {exc}",
            partial_state={"secret": secret, "factor_id": factor_id, "session_id": session_id},
        )

    if status == 200 or _is_activate_idempotent_response(status, text):
        log(f"[mfa-inline] activate OK (HTTP {status})")
        return _build_mfa_result(
            secret=secret, factor_id=factor_id, session_id=session_id,
            first_code=first_code, activated=True, mfa_info={},
        )

    raise MfaError(
        f"activate-in-page failed HTTP {status}: {text[:300]}",
        partial_state={"secret": secret, "factor_id": factor_id, "session_id": session_id},
    )


# ── Sync curl transport (tái dùng session pure_request còn sống) ──

def _backend_headers_sync(access_token: str, user_agent: str) -> dict[str, str]:
    """Headers cho POST /backend-api từ curl session (đồng bộ browser thật)."""
    from user_agent_profile import SEC_CH_UA, SEC_CH_UA_MOBILE, SEC_CH_UA_PLATFORM
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": user_agent,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": SEC_CH_UA_PLATFORM,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


def enable_2fa_in_session(
    session,
    *,
    access_token: str,
    user_agent: str,
    activate: bool = True,
    log=print,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Enroll + activate 2FA bằng CHÍNH ``session`` curl_cffi (sync) đang sống.

    Dùng cho mode pure_request: session vừa pass CF để ``create_account`` thành
    công nên còn ``cf_clearance``/``__cf_bm`` fresh + đúng proxy/IP. Enroll trong
    cùng session an toàn hơn hẳn so với spawn session mới sau đó.

    Chạy SYNC (trong worker thread của request_phase). Raises ``MfaError`` với
    ``partial_state`` khi enroll OK nhưng activate fail.
    """
    import time as _time

    headers = _backend_headers_sync(access_token, user_agent)
    enroll_url = f"{_BASE}/accounts/mfa/enroll"

    enroll_data: dict[str, Any] | None = None
    last_err = ""
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            log(f"[mfa-inline] enroll retry {attempt}/{max_attempts}")
        try:
            r = session.post(
                enroll_url, headers=headers,
                data=json.dumps({"factor_type": "totp"}), timeout=_HTTP_TIMEOUT,
            )
            status = r.status_code
            text = r.text if hasattr(r, "text") else ""
        except Exception as exc:
            last_err = f"curl lỗi: {exc}"
            log(f"[mfa-inline] enroll attempt {attempt}/{max_attempts} {last_err}")
            if attempt < max_attempts:
                _time.sleep(_BACKOFF_SECONDS[min(attempt - 1, len(_BACKOFF_SECONDS) - 1)])
            continue

        kind, payload = _classify_enroll_response(status, text)
        if kind == "ok":
            enroll_data = payload
            break
        if kind == "conflict":
            raise MfaError(
                f"enroll-in-session conflict — account đã có 2FA active server-side: {payload}"
            )
        last_err = str(payload)
        log(f"[mfa-inline] enroll attempt {attempt}/{max_attempts} failed: {last_err}")
        if attempt < max_attempts:
            _time.sleep(_BACKOFF_SECONDS[min(attempt - 1, len(_BACKOFF_SECONDS) - 1)])

    if enroll_data is None:
        raise MfaError(f"enroll-in-session failed sau {max_attempts} lần: {last_err}")

    secret = normalize_secret(enroll_data["secret"])
    factor_id = enroll_data["factor"]["id"]
    session_id = enroll_data["session_id"]
    first_code = generate_code(secret)
    log(f"[mfa-inline] enroll OK factor_id={factor_id[:20]} secret_len={len(secret)}")

    if not activate:
        return _build_mfa_result(
            secret=secret, factor_id=factor_id, session_id=session_id,
            first_code=first_code, activated=False, mfa_info=None,
        )

    try:
        r = session.post(
            f"{_BASE}/accounts/mfa/user/activate_enrollment",
            headers=headers,
            data=json.dumps({
                "factor_id": factor_id, "factor_type": "totp",
                "session_id": session_id, "code": first_code,
            }),
            timeout=_HTTP_TIMEOUT,
        )
        status = r.status_code
        text = r.text if hasattr(r, "text") else ""
    except Exception as exc:
        raise MfaError(
            f"activate-in-session transport lỗi: {exc}",
            partial_state={"secret": secret, "factor_id": factor_id, "session_id": session_id},
        )

    if status == 200 or _is_activate_idempotent_response(status, text):
        log(f"[mfa-inline] activate OK (HTTP {status})")
        return _build_mfa_result(
            secret=secret, factor_id=factor_id, session_id=session_id,
            first_code=first_code, activated=True, mfa_info={},
        )

    raise MfaError(
        f"activate-in-session failed HTTP {status}: {text[:300]}",
        partial_state={"secret": secret, "factor_id": factor_id, "session_id": session_id},
    )
