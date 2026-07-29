"""Shared NextAuth bootstrap helpers for chatgpt.com auth flows."""
from __future__ import annotations

from typing import Any


# ─────────────────────────────────────────────────────────────────────
# Cookie helpers — read auth_session_logging_id từ jar
# ─────────────────────────────────────────────────────────────────────
#
# Trace tay (HAR `web_record_20260625-120705_manual`) cho thấy:
#   query param `auth_session_logging_id` của POST `/api/auth/signin/openai`
#   ↔ giá trị cookie `oai-asli` đã có sẵn trong jar TỪ TRƯỚC (sentinel SDK
#   chatgpt.com set khi page load).
#
# Code cũ gen `str(uuid.uuid4())` mỗi lần → query param ≠ cookie → server
# cross-check fail → flag (anti-bot). Helper này sửa: đọc cookie trước, gen
# UUID mới chỉ khi chưa có cookie (lần load đầu, jar trống).
#
# 2 phiên bản:
#   - async: cho Camoufox/Playwright BrowserContext (`ctx.cookies(url)`)
#   - sync : cho curl_cffi Session (`session.cookies.get(name)`)


async def read_oai_asli_from_ctx(ctx: Any, *, url: str = "https://chatgpt.com/") -> str | None:
    """Async helper: đọc cookie ``oai-asli`` từ Camoufox/Playwright context.

    Trả về giá trị cookie (str) hoặc None nếu chưa có. Không raise — mọi
    exception (cookie API thiếu, ctx đã đóng, ...) → None để caller fallback.
    """
    try:
        cookies = await ctx.cookies(url)
    except Exception:  # noqa: BLE001 — best-effort
        return None
    for c in cookies or ():
        if c.get("name") == "oai-asli" and c.get("value"):
            return c["value"]
    return None


def read_oai_asli_from_session(session: Any) -> str | None:
    """Sync helper: đọc cookie ``oai-asli`` từ curl_cffi Session jar.

    Trả về giá trị cookie hoặc None. KHÔNG raise.
    """
    try:
        # curl_cffi Session.cookies có .get(name, default) tương thích cookies.lib
        val = session.cookies.get("oai-asli")
        return val if val else None
    except Exception:  # noqa: BLE001
        return None


BOOTSTRAP_JS = r"""
async ({email, deviceId, loggingId, callbackUrl}) => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));

    const buildParams = () => {
        const params = new URLSearchParams({
            'prompt': 'login',
            'ext-oai-did': deviceId,
            'ext-passkey-client-capabilities': '01001',
            'screen_hint': 'login_or_signup',
        });
        if (loggingId) params.set('auth_session_logging_id', loggingId);
        if (email) params.set('login_hint', email);
        return params;
    };

    let lastErr = '';
    // Retry up to 4 times — signin/openai 500 is often a transient server error
    // or stale CSRF; re-fetch CSRF each attempt.
    for (let attempt = 1; attempt <= 4; attempt++) {
        try {
            const csrfRes = await fetch('/api/auth/csrf', {credentials: 'include'});
            if (!csrfRes.ok) throw new Error('csrf HTTP ' + csrfRes.status);
            const csrfData = await csrfRes.json();
            const csrfToken = csrfData.csrfToken;
            if (!csrfToken) throw new Error('csrf token missing');

            const body = new URLSearchParams({
                callbackUrl: callbackUrl || 'https://chatgpt.com/',
                csrfToken,
                json: 'true',
            }).toString();
            const signRes = await fetch('/api/auth/signin/openai?' + buildParams().toString(), {
                method: 'POST',
                credentials: 'include',
                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body,
            });
            if (signRes.status >= 500) {
                lastErr = 'signin HTTP ' + signRes.status;
                await sleep(attempt * 1500);
                continue;
            }
            if (!signRes.ok) throw new Error('signin HTTP ' + signRes.status);
            const signData = await signRes.json();
            if (!signData.url) {
                throw new Error('signin missing url: ' + JSON.stringify(signData));
            }
            return signData.url;
        } catch (e) {
            lastErr = String(e && e.message ? e.message : e);
            // Network/transient → retry; hard errors (missing token/url) → rethrow
            if (attempt < 4 && /HTTP 5\d\d|Failed to fetch|NetworkError/.test(lastErr)) {
                await sleep(attempt * 1500);
                continue;
            }
            throw e;
        }
    }
    throw new Error('signin failed after retries: ' + lastErr);
}
"""


async def bootstrap_authorize_url(
    page: Any,
    *,
    device_id: str,
    email: str | None = None,
    logging_id: str | None = None,
    callback_url: str = "https://chatgpt.com/",
) -> str:
    """Return the auth.openai.com authorize URL bootstrapped from chatgpt.com."""
    url = await page.evaluate(
        BOOTSTRAP_JS,
        {
            "email": email or "",
            "deviceId": device_id,
            "loggingId": logging_id or "",
            "callbackUrl": callback_url,
        },
    )
    if not isinstance(url, str) or "auth.openai.com" not in url:
        raise ValueError(f"bootstrap returned bad URL: {url!r}")
    return url
