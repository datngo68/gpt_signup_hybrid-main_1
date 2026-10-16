"""Read-only Security UI smoke check. Never writes browser state or secrets."""
from __future__ import annotations

import asyncio
import contextlib
import io
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def inspect() -> None:
    from dotenv import dotenv_values
    from camoufox.async_api import AsyncCamoufox
    import pyotp

    config = dotenv_values(ROOT / ".dev.var")
    def credential(*names: str) -> str:
        for name in names:
            for key, value in config.items():
                if value and (key.upper() == name or key.upper().endswith('_' + name)):
                    return value
        raise RuntimeError('DEV_CREDENTIAL_FIELD_MISSING')

    email = credential('EMAIL', 'USERNAME')
    password = credential('PASSWORD', 'PASS')
    secret = credential('TOTP_SECRET', '2FA_SECRET', 'SECRET', 'TOTP')
    metadata: set[tuple[str, str, int]] = set()
    async with AsyncCamoufox(headless=False) as browser:
        page = await browser.new_page()
        def response_seen(response):
            url = urlsplit(response.url)
            if url.hostname in {'chatgpt.com', 'auth.openai.com'} and re.search(r'mfa|totp|security', url.path, re.I):
                # Never output arbitrary path segments, queries, headers, or bodies.
                safe = '/'.join(segment if segment in {'api', 'accounts', 'mfa', 'totp', 'security', 'auth', 'backend-api', 'settings', 'factors', 'enroll', 'challenge', 'verify'} else '{segment}' for segment in url.path.split('/'))
                metadata.add((response.request.method, safe, response.status))
        page.on('response', response_seen)
        await page.goto('https://chatgpt.com/auth/login', wait_until='domcontentloaded', timeout=60000)
        submitted: set[str] = set()
        for _ in range(45):
            if await page.get_by_text(re.compile(r'verify you are human|complete the captcha|checking your browser', re.I)).count():
                raise RuntimeError('SECURITY_CHALLENGE_STOPPED')
            for stage, selector, value in (
                ('email', 'input[type=email]', email),
                ('password', 'input[type=password]', password),
                ('totp', 'input[autocomplete=one-time-code]', pyotp.TOTP(secret).now()),
            ):
                field = page.locator(selector).first
                if stage not in submitted and await field.count() and await field.is_visible():
                    await field.fill(value)
                    await field.press('Enter')
                    submitted.add(stage)
                    break
            else:
                login = page.get_by_role('button', name=re.compile(r'^log in$', re.I)).first
                if not submitted and await login.count() and await login.is_visible():
                    await login.click()
                elif urlsplit(page.url).hostname == 'chatgpt.com' and 'password' in submitted:
                    await page.goto('https://chatgpt.com/#settings/Security', wait_until='domcontentloaded')
                    security = page.get_by_text(re.compile(r'multi.factor authentication|authenticator app', re.I)).first
                    await security.wait_for(timeout=20000)
                    print('SECURITY_UI_VERIFIED; MFA_STATE_UNCHANGED')
                    for method, path, status in sorted(metadata):
                        print(f'ENDPOINT {method} {path} {status}')
                    return
            await page.wait_for_timeout(1000)
        raise RuntimeError('LOGIN_NOT_COMPLETED_WITHIN_INSPECTION_WINDOW')


if __name__ == '__main__':
    # Suppress dependency output, including possible credential-bearing exceptions.
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            asyncio.run(asyncio.wait_for(inspect(), timeout=150))
    except Exception as exc:
        safe = str(exc) if str(exc) in {'DEV_CREDENTIAL_FIELD_MISSING', 'SECURITY_CHALLENGE_STOPPED', 'LOGIN_NOT_COMPLETED_WITHIN_INSPECTION_WINDOW'} else type(exc).__name__
        if isinstance(exc, ModuleNotFoundError):
            safe = f'ModuleNotFoundError: {exc.name}'
        print(f'INSPECTION_BLOCKED: {safe}')
        raise SystemExit(1)
    else:
        print('SECURITY_UI_VERIFIED; MFA_STATE_UNCHANGED')
