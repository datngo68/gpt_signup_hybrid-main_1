"""Read-only login diagnostics; output contains allowlisted metadata only."""
from __future__ import annotations
import asyncio
import contextlib
import io
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

OUT = sys.stdout
ROOT = Path(__file__).resolve().parents[1]

def emit(message):
    print(message, file=OUT, flush=True)

async def main():
    from dotenv import dotenv_values
    from camoufox.async_api import AsyncCamoufox
    import pyotp
    values = dotenv_values(ROOT / '.dev.var')
    def value(*names):
        for name in names:
            for key, item in values.items():
                if item and (key.upper() == name or key.upper().endswith('_' + name)):
                    return item
        raise RuntimeError('CREDENTIAL_FIELD_MISSING')
    email = value('EMAIL', 'USERNAME')
    password = value('PASSWORD', 'PASS')
    secret = value('TOTP_SECRET', '2FA_SECRET', 'SECRET', 'TOTP')
    async with AsyncCamoufox(headless=False) as browser:
        page = await browser.new_page()
        def response(response):
            url = urlsplit(response.url)
            if url.hostname in {'chatgpt.com', 'auth.openai.com'} and response.request.resource_type == 'document':
                emit(f'DOCUMENT host={url.hostname} status={response.status}')
        page.on('response', response)
        await page.goto('https://chatgpt.com/auth/login', wait_until='domcontentloaded', timeout=60000)
        submitted = set()
        last = None
        for tick in range(90):
            url = urlsplit(page.url)
            host = url.hostname if url.hostname in {'chatgpt.com', 'auth.openai.com', 'challenges.cloudflare.com'} else 'other'
            path = url.path if url.path in {'/', '/auth/login', '/log-in', '/log-in/password', '/settings/security'} else ('mfa' if 'mfa' in url.path else 'other')
            selectors = {'email': 'input[type=email],input[name=email]', 'password': 'input[type=password]', 'otp': 'input[autocomplete=one-time-code],input[name=code],input[inputmode=numeric]', 'submit': 'button[type=submit]', 'login': 'button[data-testid=login-button]'}
            visible = {}
            for name, selector in selectors.items():
                visible[name] = [item for item in await page.locator(selector).all() if await item.is_visible()]
            login_text = page.get_by_role('button', name=re.compile(r'^(log in|login|đăng nhập|sign in)$', re.I))
            text_buttons = [item for item in await login_text.all() if await item.is_visible()]
            blocked = await page.locator('iframe[src*="challenges.cloudflare.com"], #challenge-running, #challenge-stage').count()
            challenge_text = await page.get_by_text(re.compile(r'verify you are human|checking your browser|xác minh bạn là con người', re.I)).count()
            state = (host, path, tuple((k, len(v)) for k,v in visible.items()), len(text_buttons), blocked, challenge_text)
            if state != last:
                emit(f'STAGE host={host} path={path} counts={dict(state[2])} login_text_buttons={len(text_buttons)} challenge_elements={blocked} challenge_text={challenge_text}')
                last = state
            if blocked or challenge_text:
                emit('BLOCKER SECURITY_CHALLENGE; NO_BYPASS_ATTEMPTED')
                return
            if host == 'chatgpt.com' and path == '/settings/security':
                marker = page.get_by_text(re.compile(r'multi.factor authentication|authenticator app|xác thực đa yếu tố|ứng dụng xác thực', re.I))
                if await marker.count():
                    emit('VERIFIED SECURITY_UI; MFA_UNCHANGED')
                    return
            acted = False
            for stage, credential in [('email', email), ('password', password), ('otp', None)]:
                if visible[stage] and stage not in submitted:
                    if stage == 'otp' and 'password' not in submitted:
                        continue
                    await visible[stage][0].fill(pyotp.TOTP(secret).now() if stage == 'otp' else credential)
                    if visible['submit']:
                        await visible['submit'][0].click()
                    else:
                        await visible[stage][0].press('Enter')
                    submitted.add(stage)
                    emit(f'ACTION submitted={stage}')
                    acted = True
                    break
            if not acted and 'email' not in submitted and 'login' not in submitted:
                buttons = visible['login'] or text_buttons
                if buttons:
                    await buttons[0].click()
                    submitted.add('login')
                    emit('ACTION opened_login')
            if not acted and host == 'chatgpt.com' and path == '/' and 'password' in submitted and not visible['password'] and 'security' not in submitted:
                await page.goto('https://chatgpt.com/settings/security', wait_until='domcontentloaded')
                submitted.add('security')
                emit('ACTION opened_security_read_only')
            await page.wait_for_timeout(1000)
        emit('BLOCKER LOGIN_WINDOW_EXHAUSTED; SEE_LAST_STAGE')

if __name__ == '__main__':
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            asyncio.run(asyncio.wait_for(main(), timeout=180))
    except Exception as exc:
        emit(f'BLOCKER exception_type={type(exc).__name__}')
        raise SystemExit(1)
