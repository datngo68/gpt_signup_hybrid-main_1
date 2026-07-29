"""codex_auth — module standalone lấy Codex OAuth `auth.json` qua luồng PKCE chuẩn.

Độc lập hoàn toàn với code signup cũ. Chỉ phụ thuộc thư viện ngoài:
camoufox/playwright (browser), httpx (token exchange), pyotp (TOTP 2FA).

Public API:
    from codex_auth import get_codex_auth, CodexAuthError
    auth_json = await get_codex_auth(email=..., password=..., secret=...)

Flow chuẩn (tham chiếu openai/codex codex-rs/login):
    1. Sinh PKCE (code_verifier/code_challenge) + state.
    2. Mở authorize URL trên auth.openai.com bằng browser thật.
    3. Browser tự lái: login email → password → 2FA → consent.
    4. Chặn redirect về http://localhost:1455/auth/callback → lấy `code`.
    5. POST /oauth/token đổi code → {id_token, access_token, refresh_token}.
    6. (tùy chọn) token-exchange id_token → OPENAI_API_KEY.
    7. Build auth.json đúng format Codex CLI.
"""
from __future__ import annotations

from .errors import (
    CodexAuthError,
    ConsentError,
    LoginError,
    PhoneVerificationRequired,
    TokenExchangeError,
)
from .oauth import (
    CLIENT_ID,
    DEFAULT_ISSUER,
    REDIRECT_URI,
    build_auth_dot_json,
)
from .runner import get_codex_auth, get_codex_auth_sync

__all__ = [
    "get_codex_auth",
    "get_codex_auth_sync",
    "build_auth_dot_json",
    "CLIENT_ID",
    "DEFAULT_ISSUER",
    "REDIRECT_URI",
    "CodexAuthError",
    "LoginError",
    "ConsentError",
    "PhoneVerificationRequired",
    "TokenExchangeError",
]
