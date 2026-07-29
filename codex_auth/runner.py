"""Orchestrator: launch browser → drive OAuth → exchange token → build auth.json."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from .browser import drive_oauth
from .errors import CodexAuthError
from .oauth import (
    REDIRECT_URI,
    build_auth_dot_json,
    exchange_code_for_tokens,
    obtain_api_key,
)
from .pkce import generate_pkce, generate_state

LogFn = Callable[[str], None]


def _parse_proxy_for_camoufox(proxy: Optional[str]) -> Optional[dict[str, str]]:
    """Parse 'http://user:pass@host:port' → dict cho Playwright/Camoufox proxy kwarg."""
    if not proxy:
        return None
    parsed = urlparse(proxy)
    if not parsed.hostname or not parsed.port:
        raise CodexAuthError(f"proxy không hợp lệ (cần scheme://host:port): {proxy}")
    server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
    out: dict[str, str] = {"server": server}
    if parsed.username:
        out["username"] = parsed.username
    if parsed.password:
        out["password"] = parsed.password
    return out


async def get_codex_auth(
    *,
    email: str,
    password: str,
    secret: Optional[str] = None,
    proxy: Optional[str] = None,
    headless: bool = False,
    interactive: bool = False,
    fetch_api_key: bool = True,
    overall_timeout: float = 150.0,
    keep_open: bool = False,
    log: LogFn = print,
) -> dict[str, Any]:
    """Lấy Codex auth.json cho 1 account ChatGPT.

    Args:
        email/password/secret: credentials. secret = TOTP base32 (None nếu account no-2FA).
        proxy: HTTP/SOCKS proxy URL (None = direct).
        headless: chạy ẩn browser (khuyến nghị False để tránh bị flag).
        interactive: human-in-the-loop — tự điền email/pass/2FA, gặp challenge
            cần người thật (device verify, phone OTP hợp lệ, captcha) thì chờ user
            tự xử lý. KHÔNG bypass gì. Nên đi kèm headless=False.
        fetch_api_key: thử token-exchange id_token → OPENAI_API_KEY (best-effort).
        keep_open: giữ browser mở sau khi xong (debug, chỉ khi headed).

    Returns:
        dict auth.json (format Codex CLI).

    Raises:
        CodexAuthError (+ subclass) nếu bất kỳ bước nào fail.
    """
    from camoufox.async_api import AsyncCamoufox

    pkce = generate_pkce()
    state = generate_state()
    proxy_kwargs: dict[str, Any] = {}
    parsed_proxy = _parse_proxy_for_camoufox(proxy)
    if parsed_proxy:
        proxy_kwargs["proxy"] = parsed_proxy

    log(f"[codex] launch Camoufox (headless={headless}, proxy={'yes' if proxy else 'no'})")
    cf = AsyncCamoufox(
        headless=headless,
        locale="en-US",
        geoip=bool(proxy),
        **proxy_kwargs,
    )
    browser = await cf.__aenter__()
    try:
        page = await browser.new_page()
        callback = await drive_oauth(
            page,
            pkce=pkce,
            state=state,
            email=email,
            password=password,
            secret=secret,
            redirect_uri=REDIRECT_URI,
            overall_timeout=overall_timeout,
            interactive=interactive,
            log=log,
        )

        log("[codex] đổi code → token...")
        tokens = exchange_code_for_tokens(callback.code, pkce, proxy=proxy)

        api_key: Optional[str] = None
        if fetch_api_key:
            api_key = obtain_api_key(tokens["id_token"], proxy=proxy)
            log(f"[codex] API key exchange: {'OK' if api_key else 'skip/none'}")

        auth_json = build_auth_dot_json(tokens, api_key=api_key)
        log(f"[codex] ✓ auth.json sẵn sàng — account_id={auth_json['tokens']['account_id']}")
        return auth_json
    finally:
        if keep_open and not headless:
            log("[codex] debug: giữ browser mở (Ctrl+C để thoát)")
        else:
            try:
                await cf.__aexit__(None, None, None)
            except Exception:
                pass


def get_codex_auth_sync(**kwargs: Any) -> dict[str, Any]:
    """Sync wrapper cho get_codex_auth."""
    return asyncio.run(get_codex_auth(**kwargs))


def write_auth_json(auth_json: dict[str, Any], out_path: str | Path) -> Path:
    """Ghi auth.json ra file (indent 2). Trả Path đã ghi."""
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(auth_json, indent=2), encoding="utf-8")
    return path
