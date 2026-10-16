"""Isolate email-submit boundary without printing identity or server messages."""
import asyncio
import contextlib
import io
import sys
from pathlib import Path
from urllib.parse import urlsplit
OUT = sys.stdout

def emit(text):
    print(text, file=OUT, flush=True)

async def main():
    from dotenv import dotenv_values
    from camoufox.async_api import AsyncCamoufox
    config = dotenv_values(Path(__file__).resolve().parents[1] / '.dev.var')
    email = next(v for k,v in config.items() if v and (k.upper() == 'EMAIL' or k.upper().endswith('_EMAIL')))
    async with AsyncCamoufox(headless=False) as browser:
        page = await browser.new_page()
        def response_seen(response):
            url = urlsplit(response.url)
            if url.hostname in {'chatgpt.com', 'auth.openai.com'} and response.request.resource_type in {'document', 'fetch', 'xhr'}:
                category = 'auth' if any(x in url.path for x in ('auth', 'login', 'log-in', 'signin', 'sign-in')) else 'other'
                emit(f'HTTP host={url.hostname} category={category} method={response.request.method} status={response.status}')
        page.on('response', response_seen)
        page.on('requestfailed', lambda request: emit('NETWORK_FAILED resource=' + request.resource_type))
        await page.goto('https://chatgpt.com/auth/login', wait_until='domcontentloaded', timeout=60000)
        field = page.locator('input[type=email]').first
        await field.wait_for(timeout=20000)
        await field.fill(email)
        emit('FIELD_VALIDITY ' + str(await field.evaluate('(e) => ({valid:e.validity.valid,typeMismatch:e.validity.typeMismatch,valueMissing:e.validity.valueMissing})')))
        button = page.locator('button[type=submit]').first
        emit('SUBMIT enabled=' + str(await button.is_enabled()))
        await button.click()
        for tick in range(8):
            await page.wait_for_timeout(3000)
            data = await page.evaluate('''() => ({email:document.querySelectorAll('input[type=email]').length,password:document.querySelectorAll('input[type=password]').length,alerts:document.querySelectorAll('[role=alert]').length,invalid:document.querySelectorAll('input:invalid').length,challenge:document.querySelectorAll('iframe[src*="challenges.cloudflare.com"],#challenge-stage').length,dialogs:document.querySelectorAll('[role=dialog]').length})''')
            # Only classify known error phrases internally; never output page text.
            text = (await page.locator('body').inner_text()).lower()
            flags = {name: any(term in text for term in terms) for name,terms in {'rate_limit':['too many requests','rate limit'], 'try_again':['something went wrong','try again','đã xảy ra lỗi'], 'unsupported':['unsupported country','not available in your country'], 'captcha':['verify you are human','checking your browser'], 'email_error':['invalid email','email is not valid']}.items()}
            emit(f'BOUNDARY tick={tick} counts={data} error_flags={flags}')
            if data['password'] or data['challenge'] or any(flags.values()):
                return

if __name__ == '__main__':
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            asyncio.run(asyncio.wait_for(main(), timeout=90))
    except Exception as exc:
        emit('BLOCKER exception_type=' + type(exc).__name__)
        raise SystemExit(1)
