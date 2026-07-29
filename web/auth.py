"""Token-based auth cho web control plane.

Singleton token được sinh khi process start (hoặc đọc từ env), dùng để gate
toàn bộ /api/*. Token có thể đi qua:
  - Header   "X-API-Token: <token>"
  - Query    "?token=<token>"  (cho EventSource — JS EventSource không set header)
  - Cookie   "gsh_token=<token>"

CORS — server bind 127.0.0.1 default; khi user opt-in non-loopback bind, vẫn
yêu cầu token nên không cần SOP exception. Không bật CORS *.

⚠️  Bảo mật khi deploy public (non-loopback bind):
    - Reverse proxy (nginx, Cloudflare) thường log full URL kèm query string
      → ``?token=<value>`` ở SSE ``/api/icloud/run/log/stream`` lưu vào
      access log → token leak qua log retention.
    - Mitigation:
        * Cấu hình proxy SKIP query string khi log path SSE. Ví dụ nginx:
              location /api/icloud/run/log/stream {
                  access_log off;
                  proxy_pass http://localhost:8083;
              }
          Hoặc dùng ``log_format`` không ghi $args.
        * Rotate ``GPT_SIGNUP_WEB_TOKEN`` định kỳ (env + restart server).
        * Hạn chế quyền đọc access log (root only) + retention ngắn.
    - Loopback bind (127.0.0.1) — không cần lo (no proxy giữa).
"""
from __future__ import annotations

import logging
import os
import secrets
from typing import Final

from fastapi import HTTPException, Request


_ENV_KEY: Final[str] = "GPT_SIGNUP_WEB_TOKEN"
_HEADER_NAME: Final[str] = "X-API-Token"
_COOKIE_NAME: Final[str] = "gsh_token"
_QUERY_NAME: Final[str] = "token"
_SETTINGS_KEY: Final[str] = "web.auth_token"

_log = logging.getLogger(__name__)


_token_singleton: str | None = None


def _generate() -> str:
    return secrets.token_urlsafe(24)


def _load_or_create_persisted_token() -> str:
    """Đọc token đã persist trong Settings Store; chưa có thì sinh 1 lần rồi lưu.

    Token cố định qua các lần restart server → browser giữ token cũ vẫn hợp lệ,
    SSE tự reconnect, KHÔNG phải reload trang. Single source of truth là bảng
    ``settings`` (key ``web.auth_token``).
    """
    from db import get_engine, get_settings_repo

    repo = get_settings_repo(get_engine())
    existing = repo.get(_SETTINGS_KEY)
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    token = _generate()
    repo.set(_SETTINGS_KEY, token)
    return token


def get_token() -> str:
    """Lấy token hiện tại (lazy init).

    Thứ tự ưu tiên:
      1. env ``GPT_SIGNUP_WEB_TOKEN`` (cho automation / override).
      2. Token đã persist trong Settings Store (cố định qua các lần restart).
      3. Sinh mới 1 lần rồi persist vào Settings Store.
    """
    global _token_singleton  # noqa: PLW0603 — singleton hợp lý
    if _token_singleton is None:
        env_val = os.environ.get(_ENV_KEY, "").strip()
        if env_val:
            _token_singleton = env_val
        else:
            try:
                _token_singleton = _load_or_create_persisted_token()
            except Exception as exc:
                _log.warning(
                    "web.auth_token persist không khả dụng (%s) — "
                    "dùng token tạm cho process này (có thể phải reload trang sau restart)",
                    exc,
                )
                _token_singleton = _generate()
    return _token_singleton


def reset_token_for_tests(value: str | None = None) -> str:
    """Test-only helper: reset singleton. Không gọi từ runtime code."""
    global _token_singleton  # noqa: PLW0603
    _token_singleton = value
    return get_token()


def _extract_token(request: Request) -> str | None:
    header_val = request.headers.get(_HEADER_NAME)
    if header_val:
        return header_val.strip()
    query_val = request.query_params.get(_QUERY_NAME)
    if query_val:
        return query_val.strip()
    cookie_val = request.cookies.get(_COOKIE_NAME)
    if cookie_val:
        return cookie_val.strip()
    return None


def require_token(request: Request) -> None:
    """FastAPI dependency: raise 401 nếu token sai/thiếu.

    Constant-time compare để tránh timing oracle.
    """
    expected = get_token()
    provided = _extract_token(request)
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=401,
            detail="missing or invalid auth token",
            headers={"WWW-Authenticate": "Token"},
        )
