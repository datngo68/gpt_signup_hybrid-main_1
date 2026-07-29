"""iCloud Hide My Email REST client (refactor B — cookies-only).

Class duy nhất: ``HmeClient`` — httpx async + ``SessionBundle``. KHÔNG mở
Camoufox / Page; chỉ thuần HTTP với cookies extract sẵn từ
``SessionBundle``.

Endpoints (host hardcode — Apple HME chỉ phục vụ trên 1 host duy nhất,
không dynamic per-account; verified rtunazzz/hidemyemail-generator + nội bộ
``test/check_hme_minimal_call.py``):
  POST /v1/hme/generate             — sinh candidate (chưa lưu, R3.13)
  POST /v1/hme/reserve              — chốt candidate vào account
  GET  /v2/hme/list                 — list HME đã reserve (read-only probe)
  POST /v1/hme/deactivate           — ẩn HME, anonymousId còn (R9)
  POST /v1/hme/reactivate           — kích hoạt lại (R9)
  POST /v1/hme/delete               — xoá hẳn, free slot quota (R9)
  POST /v1/hme/updateMetaData       — đổi label/note, không đổi status (R9)

Auth contract (R11.1-R11.5, refactor B):
- 4 query param trên mọi request: ``clientBuildNumber``, ``clientMasteringNumber``,
  ``clientId`` (rỗng), ``dsid`` (rỗng). Apple webapp gửi ``dsid``/``clientId``
  thực nhưng API không enforce — empty string vẫn 200 + ``success=true``.
- Header cố định: ``Origin: https://www.icloud.com``,
  ``Referer: https://www.icloud.com/``, ``Content-Type: text/plain``,
  ``User-Agent`` Chrome 141 (Apple không enforce UA cụ thể; chuỗi placeholder
  hợp lệ giúp tránh anti-bot heuristic). ``scnt`` / ``X-Apple-ID-Session-Id``
  KHÔNG cần — auth thực qua cookie ``X-APPLE-WEBAUTH-*``.
- Cookies set qua ``client.cookies`` cookiejar (httpx tự gắn header ``Cookie:``
  cho request, KHÔNG serialize thủ công).

Detection lỗi: dùng ``classify_response`` (decision table R11.6, Property 4) để
map ``(status, body, error_message)`` → exception class.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any

import httpx

from .exceptions import (
    HmeAuthError,
    HmeClientError,
    HmeNotFoundError,
    HmeQuotaError,
    HmeReserveTaken,
    HmeTransientError,
)
from .models import GeneratedCandidate, RemoteHme
from .models import ReservedHme as _ReservedHmeModel
from .models import SessionBundle

# Backward-compat alias: legacy generator.py/checker.py import ``HmeApiError``
# từ module này. Giữ alias.
HmeApiError = HmeClientError

__all__ = [
    "HmeApiError",
    "HmeAuthError",
    "HmeClient",
    "HmeNotFoundError",
    "HmeQuotaError",
    "HmeReserveTaken",
    "HmeTransientError",
    "classify_response",
    "DEFAULT_CLIENT_BUILD_NUMBER",
    "DEFAULT_CLIENT_MASTERING_NUMBER",
    "FIXED_HEADERS",
    "BASE_URL",
    "FIXED_USER_AGENT",
]


# ──────────────────────────────────────────────────────────────────────────────
# Decision table classification (task 9.1, R11.6, Property 4)
# ──────────────────────────────────────────────────────────────────────────────

# Markers tìm trong combined `error_message + body.errorMessage` (case-insensitive
# substring). Order matters: quota markers check trước auth check trước reserve
# check để khớp đúng decision table.
_QUOTA_MARKERS: tuple[str, ...] = (
    "rate limit",
    "too many",
    "limit reached",
    "quota",
)
_AUTH_MARKERS: tuple[str, ...] = (
    "unauthorized",
    "not authenticated",
    "session expired",
)
_RESERVE_TAKEN_MARKERS: tuple[str, ...] = (
    "already",
    "taken",
    "unavailable",
    "duplicate",
)

# Lifecycle ops chấp nhận 404 → HmeNotFoundError (R9.6, R9.15). `generate` /
# `reserve` / `list` 404 KHÔNG map sang HmeNotFoundError vì không có "resource
# id" để 404 — sẽ rơi vào default fallback.
_LIFECYCLE_OPS: frozenset[str] = frozenset(
    {"deactivate", "reactivate", "delete", "update_meta"},
)


def _extract_error_text(body: object, error_message: str | None) -> str:
    """Gộp `error_message` + body.errorMessage (nested OK) → lowercase string."""
    parts: list[str] = []
    if error_message:
        parts.append(str(error_message))
    if isinstance(body, dict):
        # Apple shape: {"error": {"errorMessage": "..."}} hoặc top-level
        # `errorMessage` / `reason` / `error` (string fallback).
        err_obj = body.get("error")
        if isinstance(err_obj, dict):
            msg = err_obj.get("errorMessage")
            if isinstance(msg, str):
                parts.append(msg)
        top_msg = body.get("errorMessage")
        if isinstance(top_msg, str):
            parts.append(top_msg)
        reason = body.get("reason")
        if isinstance(reason, str):
            parts.append(reason)
        # `error` có thể là string trong một số response error
        if isinstance(err_obj, str):
            parts.append(err_obj)
    return " ".join(parts).lower()


def classify_response(
    status: int,
    body: object,
    error_message: str | None = None,
    *,
    op: str = "generate_or_reserve",
    is_transient_exception: bool = False,
) -> type[HmeClientError] | None:
    """Classify HME API response → exception class hoặc None (success).

    Trả về **class** (caller responsibility raise instance với message). Trả
    `None` chỉ khi response là success thực sự (status 200 + body.success=True).

    Decision table (R11.6, Property 4):
        1. is_transient_exception=True → HmeTransientError
        2. status == 429 → HmeQuotaError
        3. status ∈ {401, 421, 440} → HmeAuthError
        4. status == 404 + op ∈ lifecycle → HmeNotFoundError
        5. status == 200 + body.success=True → None
        6. status == 200 + body.success=False:
           - quota markers (rate limit/too many/limit reached/quota) → HmeQuotaError
           - auth markers (unauthorized/not authenticated/session expired) → HmeAuthError
           - reserve markers (already/taken/unavailable/duplicate) → HmeReserveTaken
           - fallback → HmeClientError
        7. status >= 500 → HmeTransientError (5xx server retryable)
        8. fallback → HmeClientError

    Args:
        status: HTTP status code (0 nếu chỉ có exception, không có response).
        body: parsed JSON body (dict thông thường, có thể là str/None nếu fail
            parse).
        error_message: raw error message string (từ exception hoặc body summary).
        op: operation name — chỉ ảnh hưởng map 404 sang HmeNotFoundError.
        is_transient_exception: True nếu caller bắt được Timeout/ConnectionError
            kiểu transient — thắng mọi rule khác.

    Returns:
        Exception class (subclass `HmeClientError`) hoặc `None` cho success.
    """
    # 1. Transient (timeout / network) chiếm ưu tiên cao nhất — không quan tâm
    #    status code (có thể status=0 vì request chưa bao giờ hoàn thành).
    if is_transient_exception:
        return HmeTransientError

    # 2-3. Status code thuần — không cần inspect body.
    if status == 429:
        return HmeQuotaError
    if status in (401, 421, 440):
        return HmeAuthError

    # 4. 404 trên lifecycle op (deactivate/reactivate/delete/update_meta).
    if status == 404 and op in _LIFECYCLE_OPS:
        return HmeNotFoundError

    # 5-6. Status 200: phải inspect body.success.
    if status == 200:
        body_dict = body if isinstance(body, dict) else None
        success = body_dict.get("success") if body_dict is not None else None
        if success is True:
            return None
        # success=False (hoặc thiếu) → đọc marker
        text_lc = _extract_error_text(body, error_message)
        if any(m in text_lc for m in _QUOTA_MARKERS):
            return HmeQuotaError
        if any(m in text_lc for m in _AUTH_MARKERS):
            return HmeAuthError
        if any(m in text_lc for m in _RESERVE_TAKEN_MARKERS):
            return HmeReserveTaken
        return HmeClientError

    # 7. 5xx server error — retry transient.
    if status >= 500:
        return HmeTransientError

    # 8. Default fallback (bao gồm 404 không phải lifecycle, 4xx khác).
    return HmeClientError


# ──────────────────────────────────────────────────────────────────────────────
# New HmeClient — httpx async + SessionBundle (task 9, R11)
# ──────────────────────────────────────────────────────────────────────────────

# Apple HME REST host cố định cho mọi account (refactor B, R11.1). KHÔNG
# dynamic theo profile vì Apple không phục vụ HME API trên các partition
# host khác — verified ``test/check_hme_minimal_call.py`` (rtunazzz cùng
# pattern).
BASE_URL = "https://p68-maildomainws.icloud.com"

# 4 query param fixed (R11.1). ``clientBuildNumber`` + ``clientMasteringNumber``
# là client identifier, KHÔNG enforce auth — Apple chấp nhận empty string
# cho ``clientId`` + ``dsid``. Giữ build/mastering version từ webapp hiện
# hành để tránh future-deprecation flag.
DEFAULT_CLIENT_BUILD_NUMBER = "2536Project32"
DEFAULT_CLIENT_MASTERING_NUMBER = "2536B20"

# User-Agent placeholder Chrome 141 macOS — Apple webapp UA tương đương.
# Để hardcode vì:
#   - SessionBundle không còn carry ``user_agent`` (refactor B).
#   - Camoufox UA không khớp 100% Apple webapp UA, dùng UA thực từ Camoufox
#     khiến request lệch fingerprint so với khi gọi từ webapp thật.
#   - Apple HME API không strict UA check.
FIXED_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

# Header cố định cho mọi request (R11.2 — refactor B). KHÔNG còn ``scnt`` /
# ``X-Apple-ID-Session-Id`` vì auth duy nhất qua cookies.
FIXED_HEADERS: dict[str, str] = {
    "Origin": "https://www.icloud.com",
    "Referer": "https://www.icloud.com/",
    "Content-Type": "text/plain",
    "Accept": "*/*",
    "User-Agent": FIXED_USER_AGENT,
}

# Path map cho từng method (R11). Apple webapp hiện tại dùng path đơn giản
# ``/v1/hme/generate`` + ``/v1/hme/reserve`` (verified với rtunazzz +
# ``test/check_hme_minimal_call.py``). Phiên bản cũ ``/generateAddress`` +
# ``/reserveHme`` đã trả 400 trên host p68 → migrate khỏi.
_PATH_GENERATE = "/v1/hme/generate"
_PATH_RESERVE = "/v1/hme/reserve"
_PATH_LIST = "/v2/hme/list"
_PATH_DEACTIVATE = "/v1/hme/deactivate"
_PATH_REACTIVATE = "/v1/hme/reactivate"
_PATH_DELETE = "/v1/hme/delete"
_PATH_UPDATE_META = "/v1/hme/updateMetaData"

# Retry policy (R11.7): max 3 attempts on transient (timeout / 5xx). Backoff
# 1, 2, 4 giây + jitter ±25%.
_RETRY_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0)


def _build_query_params() -> dict[str, str]:
    """4 query param mặc định cho mọi request (R11.1, refactor B).

    ``clientId`` + ``dsid`` cố định empty string — Apple HME API không
    enforce. Tách hàm để testing có thể stub override nếu cần (tuy nhiên
    hiện tại return constant).
    """
    return {
        "clientBuildNumber": DEFAULT_CLIENT_BUILD_NUMBER,
        "clientMasteringNumber": DEFAULT_CLIENT_MASTERING_NUMBER,
        "clientId": "",
        "dsid": "",
    }


def _build_headers() -> dict[str, str]:
    """Header cố định (R11.2, refactor B).

    Trả ``dict(FIXED_HEADERS)`` để httpx có thể mutate copy mà không ảnh
    hưởng module-level constant.
    """
    return dict(FIXED_HEADERS)


class HmeClient:
    """HTTP client gọi Apple HME API qua httpx + ``SessionBundle``.

    KHÔNG phụ thuộc Camoufox / Page. Mỗi instance bind 1 ``SessionBundle`` (1
    Apple_ID, 1 process run). Caller responsibility gọi ``aclose()`` khi done
    để release connection pool (R11).

    Refactor B: ``SessionBundle`` chỉ chứa cookies + apple_id + extracted_at.
    Host + UA + query param dùng module-level constant (BASE_URL,
    FIXED_USER_AGENT, DEFAULT_CLIENT_BUILD_NUMBER...).

    Args:
        bundle: ``SessionBundle`` extract sẵn (R12.4).
        timeout_sec: HTTP timeout per request (R11.7, default 30s).
        log: optional logger callable ``(msg: str) -> None``. Default no-op.
    """

    def __init__(
        self,
        bundle: SessionBundle,
        *,
        timeout_sec: int = 30,
        log: Any = None,
    ) -> None:
        self._bundle = bundle
        self._timeout = timeout_sec
        self._log = log if callable(log) else (lambda *_a, **_k: None)
        # base_url + headers set 1 lần ở constructor (R11.3, R11.4).
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=timeout_sec,
            headers=_build_headers(),
        )
        # Cookies set qua cookiejar (R11.5, Property 15) — KHÔNG paste vào
        # header ``Cookie:`` thủ công. httpx tự tính domain matching và build
        # ``Cookie:`` header lúc send. Domain = ``.icloud.com`` để cookie attach
        # cho mọi subdomain icloud (Apple webapp pattern chuẩn).
        for name, value in bundle.cookies.items():
            self._client.cookies.set(
                name=name, value=value, domain=".icloud.com", path="/",
            )

    async def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        op: str,
    ) -> dict:
        """Gửi 1 request với retry transient. Raise đúng exception class.

        Body luôn JSON-encoded thủ công với ``content=`` (KHÔNG dùng ``json=``)
        để giữ Content-Type: text/plain (R11.2).

        Raises:
            HmeClientError subclass theo decision table R11.6.
        """
        params = _build_query_params()
        encoded_body: bytes | None = None
        if body is not None:
            encoded_body = json.dumps(body, separators=(",", ":")).encode("utf-8")

        last_exc: Exception | None = None
        for attempt_idx in range(_RETRY_MAX_ATTEMPTS):
            try:
                resp = await self._client.request(
                    method,
                    path,
                    params=params,
                    content=encoded_body,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                self._log(
                    f"[hme] {method} {path} transient {type(exc).__name__}: {exc}"
                )
                if attempt_idx == _RETRY_MAX_ATTEMPTS - 1:
                    raise HmeTransientError(
                        f"transient after {attempt_idx + 1} attempts: {exc}"
                    ) from exc
                await self._sleep_backoff(attempt_idx)
                continue
            except httpx.HTTPError as exc:
                # Other httpx errors (vd InvalidURL) — không retry, fail-fast.
                raise HmeClientError(f"http error: {exc}") from exc

            # Parse body (chấp nhận empty body cho lifecycle action).
            parsed_body = self._parse_body(resp)
            err_text = self._summary_error_text(parsed_body)
            cls = classify_response(
                resp.status_code,
                parsed_body,
                err_text,
                op=op,
            )
            # Retry transient on 5xx (R11.7).
            if (
                cls is HmeTransientError
                and attempt_idx < _RETRY_MAX_ATTEMPTS - 1
            ):
                self._log(
                    f"[hme] {method} {path} status={resp.status_code} retrying "
                    f"(attempt {attempt_idx + 1}/{_RETRY_MAX_ATTEMPTS})"
                )
                await self._sleep_backoff(attempt_idx)
                continue
            if cls is None:
                return parsed_body if isinstance(parsed_body, dict) else {}
            raise cls(
                f"{method} {path} status={resp.status_code} body={err_text!r}"
            )

        # Should not reach here, but keep mypy happy.
        if last_exc is not None:
            raise HmeTransientError(str(last_exc)) from last_exc
        raise HmeClientError("unknown failure")

    @staticmethod
    async def _sleep_backoff(attempt_idx: int) -> None:
        """Backoff với jitter ±25% (R11.7)."""
        base = _RETRY_BACKOFF_SECONDS[
            min(attempt_idx, len(_RETRY_BACKOFF_SECONDS) - 1)
        ]
        jitter = random.uniform(0.75, 1.25)
        await asyncio.sleep(base * jitter)

    @staticmethod
    def _parse_body(resp: httpx.Response) -> object:
        """Parse JSON body, fallback raw text/empty dict.

        Return shape (caller must handle với ``isinstance(body, dict)``):
            - ``{}`` nếu body empty (vd lifecycle action OK không có body).
            - ``dict`` parsed từ JSON.
            - ``str`` raw text khi JSON parse fail nhưng decode được.
            - ``{}`` (empty dict) khi cả JSON và text decode đều fail —
              tránh trả None khiến caller `body.get(...)` raise AttributeError
              (A8 fix). Caller nhận `{}` sẽ rơi vào nhánh "no result" → raise
              HmeClientError đúng.
        """
        if not resp.content:
            return {}
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError):
            try:
                return resp.text
            except UnicodeDecodeError:
                return {}

    @staticmethod
    def _summary_error_text(body: object) -> str:
        """Trích error message text để feed vào classify_response."""
        if not isinstance(body, dict):
            return ""
        candidates: list[str] = []
        for key in ("errorMessage", "reason"):
            val = body.get(key)
            if isinstance(val, str):
                candidates.append(val)
        err_obj = body.get("error")
        if isinstance(err_obj, dict):
            val = err_obj.get("errorMessage")
            if isinstance(val, str):
                candidates.append(val)
        elif isinstance(err_obj, str):
            candidates.append(err_obj)
        return " ".join(candidates)

    # ── Public API methods (R11) ──────────────────────────────────────────

    async def generate(self) -> GeneratedCandidate:
        """POST /v1/hme/generate — sinh candidate (R3.13)."""
        body = await self._request(
            "POST", _PATH_GENERATE, {"langCode": "en-us"}, op="generate",
        )
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict):
            raise HmeClientError(f"generate: missing result — {body!r}")
        candidate = result.get("hme")
        if not isinstance(candidate, str) or "@" not in candidate:
            raise HmeClientError(f"generate: invalid candidate — {result!r}")
        return GeneratedCandidate(candidate=candidate, raw=body)

    async def reserve(
        self, candidate: str, label: str, note: str | None,
    ) -> _ReservedHmeModel:
        """POST /v1/hme/reserve — chốt candidate."""
        if not label:
            raise ValueError("label rỗng — Apple require non-empty label")
        body = await self._request(
            "POST",
            _PATH_RESERVE,
            {"hme": candidate, "label": label, "note": note or ""},
            op="reserve",
        )
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict):
            raise HmeClientError(f"reserve: missing result — {body!r}")
        hme_obj = result.get("hme")
        if isinstance(hme_obj, dict):
            anon_id = hme_obj.get("anonymousId") or hme_obj.get("hme")
            saved_email = hme_obj.get("hme") or candidate
            saved_label = hme_obj.get("label")
            saved_note = hme_obj.get("note")
        else:
            anon_id = None
            saved_email = candidate
            saved_label = label
            saved_note = note
        return _ReservedHmeModel(
            email=saved_email,
            hme_id=str(anon_id) if anon_id else "",
            label=saved_label,
            note=saved_note,
            raw=body,
        )

    async def list(self) -> list[RemoteHme]:
        """GET /v2/hme/list — list mọi HME đã reserve."""
        body = await self._request("GET", _PATH_LIST, None, op="list")
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict):
            raise HmeClientError(f"list: missing result — {body!r}")
        items = (
            result.get("hmeEmails")
            or result.get("forwardToEmails")
            or []
        )
        if not isinstance(items, list):
            raise HmeClientError(f"list: items not list — {items!r}")
        return [
            RemoteHme(
                email=str(item.get("hme", "")),
                hme_id=str(item.get("anonymousId") or ""),
                label=item.get("label"),
                note=item.get("note"),
                is_active=bool(item.get("isActive", True)),
                create_timestamp=int(item.get("createTimestamp") or 0),
            )
            for item in items
            if isinstance(item, dict)
        ]

    async def deactivate(self, hme_id: str) -> None:
        """POST /v1/hme/deactivate (R9.1)."""
        await self._request(
            "POST", _PATH_DEACTIVATE, {"anonymousId": hme_id}, op="deactivate",
        )

    async def reactivate(self, hme_id: str) -> None:
        """POST /v1/hme/reactivate (R9.13)."""
        await self._request(
            "POST", _PATH_REACTIVATE, {"anonymousId": hme_id}, op="reactivate",
        )

    async def delete(self, hme_id: str) -> None:
        """POST /v1/hme/delete (R9.14)."""
        await self._request(
            "POST", _PATH_DELETE, {"anonymousId": hme_id}, op="delete",
        )

    async def update_meta(
        self, hme_id: str, label: str, note: str | None,
    ) -> None:
        """POST /v1/hme/updateMetaData (R9.16)."""
        await self._request(
            "POST",
            _PATH_UPDATE_META,
            {"anonymousId": hme_id, "label": label, "note": note or ""},
            op="update_meta",
        )

    async def aclose(self) -> None:
        """Release httpx connection pool."""
        await self._client.aclose()


