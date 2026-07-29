"""OAuth constants + token exchange + auth.json builder.

Tham chiếu chính xác openai/codex codex-rs/login/src/server.rs & token_data.rs.
Phần network ở đây (/oauth/token) là API thường, KHÔNG bị Cloudflare JS challenge
(codex CLI gọi bằng reqwest trần) → dùng httpx được.
"""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from .errors import TokenExchangeError
from .pkce import PkceCodes

# ── Hằng số OAuth (giống Codex CLI) ──────────────────────────────────
DEFAULT_ISSUER = "https://auth.openai.com"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"  # client_id thật của Codex CLI (lấy từ `codex login`)
DEFAULT_PORT = 1455
REDIRECT_URI = f"http://localhost:{DEFAULT_PORT}/auth/callback"
SCOPE = "openid profile email offline_access api.connectors.read api.connectors.invoke"
ORIGINATOR = "codex_cli_rs"

# Grant types cho token-exchange API key
_TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
_ID_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:id_token"

_HTTP_TIMEOUT = 30.0


def build_authorize_url(
    pkce: PkceCodes,
    state: str,
    *,
    issuer: str = DEFAULT_ISSUER,
    client_id: str = CLIENT_ID,
    redirect_uri: str = REDIRECT_URI,
) -> str:
    """Dựng authorize URL khớp build_authorize_url() của codex-rs."""
    query = [
        ("response_type", "code"),
        ("client_id", client_id),
        ("redirect_uri", redirect_uri),
        ("scope", SCOPE),
        ("code_challenge", pkce.code_challenge),
        ("code_challenge_method", "S256"),
        ("id_token_add_organizations", "true"),
        ("codex_cli_simplified_flow", "true"),
        ("state", state),
        ("originator", ORIGINATOR),
    ]
    return f"{issuer}/oauth/authorize?{urlencode(query)}"


def _httpx_proxy(proxy: Optional[str]) -> Optional[str]:
    return proxy or None


def exchange_code_for_tokens(
    code: str,
    pkce: PkceCodes,
    *,
    issuer: str = DEFAULT_ISSUER,
    client_id: str = CLIENT_ID,
    redirect_uri: str = REDIRECT_URI,
    proxy: Optional[str] = None,
) -> dict[str, str]:
    """POST /oauth/token (grant_type=authorization_code) → {id_token, access_token, refresh_token}.

    Raises TokenExchangeError nếu status != 2xx hoặc thiếu field.
    """
    token_endpoint = f"{issuer.rstrip('/')}/oauth/token"
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": pkce.code_verifier,
    }
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT, proxy=_httpx_proxy(proxy)) as client:
            resp = client.post(
                token_endpoint,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                content=urlencode(body),
            )
    except httpx.HTTPError as exc:
        raise TokenExchangeError(f"network error khi đổi code→token: {exc}") from exc

    if resp.status_code // 100 != 2:
        raise TokenExchangeError(
            f"token endpoint trả status {resp.status_code}: {resp.text[:300]}"
        )

    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        raise TokenExchangeError(f"token response không phải JSON: {resp.text[:200]}") from exc

    missing = [k for k in ("id_token", "access_token", "refresh_token") if not data.get(k)]
    if missing:
        raise TokenExchangeError(f"token response thiếu field: {missing}")

    return {
        "id_token": data["id_token"],
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
    }


def obtain_api_key(
    id_token: str,
    *,
    issuer: str = DEFAULT_ISSUER,
    client_id: str = CLIENT_ID,
    proxy: Optional[str] = None,
) -> Optional[str]:
    """Token-exchange id_token → OPENAI_API_KEY. Best-effort: trả None nếu fail.

    (Codex cũng để api_key = optional; account ChatGPT thường không exchange được
    sang API key nếu không có platform org → None là hợp lệ.)
    """
    token_endpoint = f"{issuer.rstrip('/')}/oauth/token"
    body = {
        "grant_type": _TOKEN_EXCHANGE_GRANT,
        "client_id": client_id,
        "requested_token": "openai-api-key",
        "subject_token": id_token,
        "subject_token_type": _ID_TOKEN_TYPE,
    }
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT, proxy=_httpx_proxy(proxy)) as client:
            resp = client.post(
                token_endpoint,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                content=urlencode(body),
            )
    except httpx.HTTPError:
        return None
    if resp.status_code // 100 != 2:
        return None
    try:
        return resp.json().get("access_token")
    except json.JSONDecodeError:
        return None


def _decode_jwt_payload(jwt: str) -> dict[str, Any]:
    """Decode payload JWT (base64url-nopad). Trả {} nếu format sai."""
    parts = jwt.split(".")
    if len(parts) != 3 or not all(parts):
        return {}
    payload_b64 = parts[1]
    padding = "=" * (-len(payload_b64) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_b64 + padding)
        return json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return {}


def extract_account_id(id_token: str) -> Optional[str]:
    """Lấy chatgpt_account_id từ claim 'https://api.openai.com/auth' của id_token."""
    claims = _decode_jwt_payload(id_token)
    auth = claims.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        acc = auth.get("chatgpt_account_id")
        if isinstance(acc, str) and acc:
            return acc
    return None


def build_auth_dot_json(
    tokens: dict[str, str],
    *,
    api_key: Optional[str] = None,
    last_refresh: Optional[str] = None,
) -> dict[str, Any]:
    """Build dict auth.json đúng format Codex CLI.

    {
      "OPENAI_API_KEY": <str|null>,
      "tokens": {"id_token","access_token","refresh_token","account_id"},
      "last_refresh": "<RFC3339 UTC>"
    }
    """
    if last_refresh is None:
        last_refresh = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "OPENAI_API_KEY": api_key,
        "tokens": {
            "id_token": tokens["id_token"],
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "account_id": extract_account_id(tokens["id_token"]),
        },
        "last_refresh": last_refresh,
    }
