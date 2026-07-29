"""Exception hierarchy cho codex_auth — fail-fast, không fallback che lỗi."""
from __future__ import annotations


class CodexAuthError(Exception):
    """Base: mọi lỗi trong luồng lấy Codex auth.json."""


class LoginError(CodexAuthError):
    """Đăng nhập thất bại (sai password, không tìm thấy input, timeout...)."""


class ConsentError(CodexAuthError):
    """Bước consent/authorize OAuth thất bại."""


class PhoneVerificationRequired(CodexAuthError):
    """Account bị chặn ở gate verify số điện thoại (add-phone).

    Đây là gate server-side của OpenAI — KHÔNG bypass được bằng frontend.
    Account cần đã verify phone / có workspace mới qua được Codex OAuth.
    """


class TokenExchangeError(CodexAuthError):
    """POST /oauth/token (đổi code → token, hoặc token-exchange API key) thất bại."""
