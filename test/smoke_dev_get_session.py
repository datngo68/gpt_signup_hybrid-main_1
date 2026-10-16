"""Run the actual Get Session helper; never output credentials or session data."""
from __future__ import annotations
import asyncio
import contextlib
import io
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

async def main() -> bool:
    from dotenv import dotenv_values
    from session_phase import _get_session_browser
    config = dotenv_values(ROOT / '.dev.var')
    def credential(*names: str) -> str:
        for name in names:
            for key, value in config.items():
                if value and (key.upper() == name or key.upper().endswith('_' + name)):
                    return value
        raise ValueError('DEV_CREDENTIAL_FIELD_MISSING')
    result = await asyncio.wait_for(_get_session_browser(
        email=credential('EMAIL', 'USERNAME'),
        password=credential('PASSWORD', 'PASS'),
        secret=credential('TOTP_SECRET', '2FA_SECRET', 'SECRET', 'TOTP'),
        headless=False,
        keep_browser_open=False,
        log=lambda *_args, **_kwargs: None,
    ), timeout=240)
    return bool(result and (result.get('accessToken') or result.get('access_token') or result.get('session_token') or result.get('user')))

if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            present = asyncio.run(main())
    except Exception as exc:
        message = str(exc).lower()
        category = 'unclassified'
        for name, markers in (
            ('dependency_missing', ('no module named',)),
            ('credential_missing', ('dev_credential_field_missing',)),
            ('security_challenge', ('captcha', 'challenge', 'verify you are human')),
            ('password_field_missing', ('password input not found',)),
            ('login_rejected', ('login failed', 'password error')),
            ('bootstrap_failed', ('bootstrap', 'authorize url', 'csrf')),
            ('network_or_driver', ('network', 'driver', 'browser launch', 'net::err_', 'ns_error')),
            ('timeout', ('timeout', 'timed out')),
        ):
            if any(marker in message for marker in markers):
                category = name
                break
        if isinstance(exc, TimeoutError):
            category = 'timeout'
        print(f'GET_SESSION_FAILED exception={type(exc).__name__} category={category}')
        raise SystemExit(1)
    else:
        print(f'GET_SESSION_RESULT session_present={present}')
        raise SystemExit(0 if present else 1)
