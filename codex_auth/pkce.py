"""PKCE + state generation — mirror chính xác openai/codex codex-rs/login/src/pkce.rs.

Codex dùng:
    code_verifier  = base64url-nopad(64 random bytes)
    code_challenge = base64url-nopad(SHA256(code_verifier_ascii))
    state          = base64url-nopad(32 random bytes)
"""
from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass


def _b64url_nopad(data: bytes) -> str:
    """Base64 URL-safe, bỏ padding '=' (RFC 7636 PKCE)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class PkceCodes:
    code_verifier: str
    code_challenge: str


def generate_pkce() -> PkceCodes:
    """Sinh cặp PKCE giống Codex CLI (64 bytes verifier, S256 challenge)."""
    verifier_bytes = os.urandom(64)
    code_verifier = _b64url_nopad(verifier_bytes)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = _b64url_nopad(digest)
    return PkceCodes(code_verifier=code_verifier, code_challenge=code_challenge)


def generate_state() -> str:
    """Sinh state ngẫu nhiên (32 bytes → base64url-nopad)."""
    return _b64url_nopad(os.urandom(32))
