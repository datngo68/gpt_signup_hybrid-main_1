"""Datadog RUM `_dd_s` session cookie generator.

Anti-ban (journal 260625-1224 Task 3.5 + bug M3):
    Trace tay (HAR `web_record_20260625-120705_manual`) cho thấy browser thật
    luôn có cookie ``_dd_s`` trên chatgpt.com — Datadog RUM SDK set khi page
    load. Format:
        _dd_s=aid=<uuid4>&rum=0&id=<uuid4>&created=<ms>&expire=<ms+15min>

    Pure_request flow (curl_cffi) KHÔNG có Datadog SDK chạy → cookie vắng.
    Server thấy ``traceparent`` header có (request_phase tự gen) nhưng KHÔNG
    có ``_dd_s`` cookie → "session bắt đầu giữa luồng" → bất thường.

Module này:
    - ``gen_dd_s_cookie()``    — gen string cookie value khớp format browser thật.
    - ``inject_dd_s(session)`` — set cookie vào curl_cffi Session jar nếu chưa có.

Thiết kế:
    - aid (anonymous ID) + id (session ID) là UUID4 mới mỗi lần gen.
    - rum=0 (no privileges) hoặc rum=2 (authenticated). Mặc định rum=0; caller
      sau khi login OK có thể refresh với rum=2.
    - created = now ms; expire = created + 15 min (khớp browser thật).
    - URL encoding KHÔNG cần — `aid=...&...` raw, browser gửi nguyên.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 15 phút TTL — khớp Datadog RUM SDK default.
_DD_S_TTL_MS = 15 * 60 * 1000


def gen_dd_s_cookie(*, rum: int = 0) -> str:
    """Gen ``_dd_s`` cookie value khớp format Datadog RUM SDK.

    Args:
        rum: 0 = anonymous (default), 2 = authenticated session. Trace tay cho
             thấy chatgpt.com unauth dùng rum=0, sau login chuyển rum=2.

    Returns:
        String value cho cookie ``_dd_s`` (chưa có name=). Caller set vào jar
        qua ``session.cookies.set("_dd_s", value, domain=...)``.
    """
    if rum not in (0, 2):
        raise ValueError(f"rum must be 0 or 2, got {rum!r}")
    aid = str(uuid.uuid4())
    sid = str(uuid.uuid4())
    now_ms = int(time.time() * 1000)
    expire_ms = now_ms + _DD_S_TTL_MS
    return f"aid={aid}&rum={rum}&id={sid}&created={now_ms}&expire={expire_ms}"


def inject_dd_s(
    session: Any,
    *,
    domain: str = ".chatgpt.com",
    rum: int = 0,
    log: Optional[Any] = None,
    overwrite: bool = False,
) -> bool:
    """Inject ``_dd_s`` vào curl_cffi Session jar (idempotent).

    Args:
        session: curl_cffi Session instance.
        domain: cookie domain (mặc định ``.chatgpt.com`` — match request thật).
        rum: 0 hoặc 2.
        log: optional callable cho diagnostic.
        overwrite: True = replace existing; False = skip nếu jar đã có _dd_s.

    Returns:
        True nếu inject (hoặc replace), False nếu skip (đã có và overwrite=False).
    """
    _log = log or (lambda m: logger.debug(m))
    try:
        existing = session.cookies.get("_dd_s")
    except Exception:  # noqa: BLE001
        existing = None
    if existing and not overwrite:
        _log(f"[_dd_s] cookie đã có (len={len(existing)}) — skip inject")
        return False
    value = gen_dd_s_cookie(rum=rum)
    try:
        # curl_cffi Session.cookies.set(name, value, domain=...) tương thích
        # http.cookiejar interface.
        session.cookies.set("_dd_s", value, domain=domain, path="/")
    except TypeError:
        # Fallback: cookie API không nhận domain kwarg
        try:
            session.cookies.set("_dd_s", value)
        except Exception as exc:  # noqa: BLE001
            _log(f"[_dd_s] inject fail: {exc}")
            return False
    except Exception as exc:  # noqa: BLE001
        _log(f"[_dd_s] inject fail: {exc}")
        return False
    _log(f"[_dd_s] injected (rum={rum}, domain={domain})")
    return True


__all__ = ["gen_dd_s_cookie", "inject_dd_s"]
