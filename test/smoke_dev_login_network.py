"""Instrument existing read-only boundary check; no production changes."""
import asyncio
import re
import runpy
from pathlib import Path
from urllib.parse import urlsplit
from playwright.async_api import Page

original_goto = Page.goto
closing = False

async def diagnostic_goto(page, *args, **kwargs):
    def failed(request):
        raw = request.failure or ''
        code = re.search(r'\bnet::ERR_[A-Z_]+\b', raw)
        firefox = re.search(r'\bNS_(?:ERROR|BINDING)_[A-Z_]+\b', raw)
        # Firefox native names are mapped to fixed categories, never printed raw.
        category = 'aborted' if firefox and firefox.group() in {'NS_BINDING_ABORTED', 'NS_ERROR_ABORT'} else 'network_failure' if firefox else 'unclassified'
        host = urlsplit(request.url).hostname or 'none'
        safe_host = host if re.fullmatch(r'[a-zA-Z0-9.-]+', host) else 'redacted'
        print(f'NETWORK_DETAIL host={safe_host} resource={request.resource_type} code={code.group() if code else "not_net_error"} category={category} phase={"teardown" if closing else "active"}', file=__import__('sys').__stdout__, flush=True)
    page.on('requestfailed', failed)
    async def inspect_alerts():
        await asyncio.sleep(12)
        for selector in ('[role=alert]', '[role=dialog]'):
            for item in await page.locator(selector).all():
                text = (await item.inner_text()).lower()
                phrases = {'network_error': ('network error', 'network connection', 'connection failed', 'failed to fetch'), 'auth_error': ('authentication error', 'unable to log in', 'unable to sign in', 'login failed'), 'retry': ('something went wrong', 'try again', 'unexpected error'), 'blocked': ('access denied', 'request blocked', 'temporarily blocked'), 'email_check': ('check your email', 'verification email', 'verify your email'), 'captcha': ('verify you are human', 'captcha'), 'cookie': ('enable cookies', 'cookies are disabled'), 'login_form': ('welcome back', 'log in', 'sign in', 'continue with', 'đăng nhập')}
                matches = [key for key, terms in phrases.items() if any(term in text for term in terms)]
                print(f'ALERT_CLASS selector={selector} categories={matches} empty={not bool(text.strip())}', file=__import__('sys').__stdout__, flush=True)
    asyncio.create_task(inspect_alerts())
    return await original_goto(page, *args, **kwargs)

Page.goto = diagnostic_goto
# Record teardown separately so aborts after context close cannot explain login failure.
from playwright.async_api import Browser
original_close = Browser.close
async def diagnostic_close(browser, *args, **kwargs):
    global closing
    closing = True
    return await original_close(browser, *args, **kwargs)
Browser.close = diagnostic_close
runpy.run_path(str(Path(__file__).with_name('smoke_dev_login_boundary.py')), run_name='__main__')
