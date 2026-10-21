"""Orchestrator: launch browser → drive OAuth → exchange token → build auth.json."""
from __future__ import annotations

import asyncio
import json
import time
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
from .sms import SmsProvider, create_sms_provider

LogFn = Callable[[str], None]


def _resolve_sms_provider(
    provider_override: Optional[SmsProvider] = None,
    log: LogFn = print,
) -> Optional[SmsProvider]:
    """Lấy SmsProvider từ override hoặc fallback đọc SQLite Settings table."""
    if provider_override is not None:
        return provider_override
    try:
        from db import get_engine
        from db.repositories import SettingsRepository

        repo = SettingsRepository(get_engine())
        p_name = repo.get("codex.sms_provider")
        p_key = repo.get("codex.sms_api_key")
        p_service = repo.get("codex.sms_service_id")
        if p_name and p_key:
            log(f"[codex] nạp cấu hình SMS từ DB Settings (provider={p_name})...")
            return create_sms_provider(p_name, p_key, p_service)
    except Exception as exc:
        log(f"[codex] lưu ý khi đọc SMS settings từ DB: {exc}")

    # 2. Fallback đọc từ Environment variables / file .dev.var / .env
    from .sms import get_env_sms_key
    env_key = get_env_sms_key()
    if env_key:
        import os
        p_name = os.environ.get("CODEX_SMS_PROVIDER", "viotp")
        p_service = os.environ.get("CODEX_SMS_SERVICE_ID")
        log(f"[codex] nạp cấu hình SMS từ ENV/.dev.var (provider={p_name})...")
        return create_sms_provider(p_name, env_key, p_service)

    return None


def _resolve_codex_mode(use_api_param: Optional[bool] = None) -> bool:
    """Trả True nếu chạy mode Pure API, False nếu chạy mode browser."""
    if use_api_param is not None:
        return use_api_param
    try:
        from db import get_engine
        from db.repositories import SettingsRepository

        repo = SettingsRepository(get_engine())
        mode = repo.get("codex.mode")
        if mode == "browser":
            return False
    except Exception:
        pass
    return True


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
    sms_provider: Optional[SmsProvider] = None,
    use_api: Optional[bool] = None,
    overall_timeout: float = 150.0,
    keep_open: bool = False,
    phone_state: Optional[Callable[[str], None]] = None,
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
        sms_provider: Provider nhận OTP SĐT (ViOTP/Chothuesimcode) tự động verify nếu gặp màn add-phone.
        use_api: True để chạy Pure HTTP API (nhẹ nhàng, không tốn RAM), False để chạy Camoufox. None = đọc từ Settings.
        keep_open: giữ browser mở sau khi xong (debug, chỉ khi headed).

    Returns:
        dict auth.json (format Codex CLI).

    Raises:
        CodexAuthError (+ subclass) nếu bất kỳ bước nào fail.
    """
    observed_phone = "unknown"

    def observe_phone(state: str) -> None:
        nonlocal observed_phone
        if state in ("required", "verified"):
            observed_phone = state
            if phone_state:
                phone_state(state)

    def completed(auth: dict[str, Any]) -> dict[str, Any]:
        if observed_phone == "unknown" and phone_state:
            phone_state("not_required")
        return auth

    deadline = time.monotonic() + overall_timeout
    resolved_sms = _resolve_sms_provider(sms_provider, log=log)
    should_use_api = _resolve_codex_mode(use_api)

    # 1. Thử chạy luồng Pure API siêu nhẹ trước nếu được kích hoạt (và không ở chế độ interactive)
    if should_use_api and not interactive:
        try:
            from .api_runner import get_codex_auth_api, CloudflareChallengeRequired
            log("[codex] ⚡ Thử chạy chế độ Pure API (nhẹ nhàng, không mở trình duyệt)...")
            auth = await get_codex_auth_api(
                email=email,
                password=password,
                secret=secret,
                sms_provider=resolved_sms,
                proxy=proxy,
                fetch_api_key=fetch_api_key,
                overall_timeout=min(overall_timeout, 120.0),
                phone_state=observe_phone,
                log=log,
            )
            return completed(auth)
        except CloudflareChallengeRequired as cf_exc:
            log(f"[codex] ⚠️ Chế độ API gặp Cloudflare challenge ({cf_exc}) → Tự động fallback sang Camoufox browser...")
        except Exception:
            # Only an explicit pre-login Cloudflare challenge warrants browser replay.
            # Retrying arbitrary SMS/auth failures can create a second paid rental.
            raise

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Codex OAuth overall_timeout đã hết; không fallback browser.")

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
            sms_provider=resolved_sms,
            redirect_uri=REDIRECT_URI,
            overall_timeout=max(0.0, deadline - time.monotonic()),
            interactive=interactive,
            phone_state=observe_phone,
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
        return completed(auth_json)
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
