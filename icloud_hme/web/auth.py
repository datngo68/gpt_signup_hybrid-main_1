"""[DEPRECATED] Bearer token auth dependency cho factory ``build_router``.

⚠️  RUNTIME KHÔNG DÙNG FILE NÀY ⚠️

Module này chỉ còn import bởi ``icloud_hme/web/router.py`` (đã deprecated)
và test cũ. Runtime auth đã chuyển sang ``web/auth.py:require_token``
middleware (env ``GPT_SIGNUP_WEB_TOKEN``) — single source of truth cho
toàn bộ ``/api/*``.

Khi nào xoá hẳn:
    - Sau khi xoá ``icloud_hme/web/router.py``.
    - Sau khi rewrite test cũ sang test ``web/icloud_routes.py``.

Refs (history):
    requirements.md R10.10a (Web_API auth fail-fast nếu env unset)
    requirements.md R9.8 (mọi /api/icloud/run/* require Bearer)
    design.md §Components / Web_API (auth section)
    tasks.md task 27, task 5.5

Behavior:
    - Env ``ICLOUD_API_AUTH_TOKEN`` unset → fail-fast 503 lúc dependency
      check (project-rules: KHÔNG default insecure).
    - Env set + ``Authorization: Bearer <token>`` mismatch → 401.
    - Match → pass dependency.

SSE fallback (task 5.5):
    Trình duyệt EventSource KHÔNG cho phép set header tùy chỉnh, do đó
    ``GET /api/icloud/run/log/stream`` cũng chấp nhận token qua query
    ``?token=<bearer>``. Header vẫn ưu tiên trước; token vẫn fail-fast
    giống Bearer (không default insecure). Helper ``verify_request_token``
    gói luồng này, không expose query-mode cho các endpoint khác.
"""

from __future__ import annotations

import os
import secrets
from typing import Any


# HTTP status codes (avoid FastAPI imports trong unit test path).
_HTTP_503 = 503
_HTTP_401 = 401


class AuthError(Exception):
    """Auth failure — caller responsibility convert sang HTTP exception."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{status_code}: {detail}")


def verify_bearer_token(
    authorization_header: str | None,
    *,
    expected_token: str | None = None,
) -> None:
    """Verify ``Authorization: Bearer <token>`` header.

    Args:
        authorization_header: Raw header value (None nếu thiếu).
        expected_token: Override env (test). None → đọc env
            ``ICLOUD_API_AUTH_TOKEN``.

    Raises:
        AuthError(503): Server unconfigured (env unset, R10.10a).
        AuthError(401): Header thiếu / sai format / token mismatch.
    """
    token = expected_token if expected_token is not None else os.environ.get(
        "ICLOUD_API_AUTH_TOKEN"
    )
    if not token:
        # R10.10a: server SHALL fail-fast khi env unset (KHÔNG silently allow).
        raise AuthError(
            _HTTP_503,
            "Server unconfigured: ICLOUD_API_AUTH_TOKEN env not set",
        )

    if not authorization_header:
        raise AuthError(_HTTP_401, "Missing Authorization header")

    parts = authorization_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AuthError(
            _HTTP_401,
            "Invalid Authorization header format (expected 'Bearer <token>')",
        )

    provided = parts[1].strip()
    # Constant-time compare (tránh timing attack).
    if not secrets.compare_digest(provided, token):
        raise AuthError(_HTTP_401, "Invalid token")


def verify_query_token(
    query_token: str | None,
    *,
    expected_token: str | None = None,
) -> None:
    """Verify token đi qua query param (SSE / EventSource fallback).

    Args:
        query_token: Giá trị query ``token`` (None nếu thiếu).
        expected_token: Override env (test). None → đọc env
            ``ICLOUD_API_AUTH_TOKEN``.

    Raises:
        AuthError(503): Server unconfigured (env unset, R10.10a).
        AuthError(401): Query token thiếu hoặc mismatch.

    Lưu ý: chỉ dùng cho endpoint SSE — KHÔNG mở rộng cho path khác để
    tránh leak token qua URL log của reverse proxy ở các endpoint không
    cần thiết (R9.8 + project-rules: không default insecure).
    """
    token = expected_token if expected_token is not None else os.environ.get(
        "ICLOUD_API_AUTH_TOKEN"
    )
    if not token:
        raise AuthError(
            _HTTP_503,
            "Server unconfigured: ICLOUD_API_AUTH_TOKEN env not set",
        )

    if not query_token:
        raise AuthError(_HTTP_401, "Missing token query param")

    if not secrets.compare_digest(query_token.strip(), token):
        raise AuthError(_HTTP_401, "Invalid token")


__all__ = ["verify_bearer_token", "verify_query_token", "AuthError"]
