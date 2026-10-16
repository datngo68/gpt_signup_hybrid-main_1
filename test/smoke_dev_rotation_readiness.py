"""Opt-in read-only MFA readiness. Never disables/enrolls/activates a factor."""
from __future__ import annotations
import argparse
import asyncio
import contextlib
import io
import logging
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def check():
    from dotenv import dotenv_values
    from session_phase import _get_session_browser
    from curl_cffi.requests import AsyncSession
    from mfa_rotation import RotationBlocked
    from mfa_rotation_http import RotationTransport
    values = dotenv_values(Path(__file__).resolve().parents[1] / '.dev.var')
    def field(*names):
        for name in names:
            for key, value in values.items():
                if value and (key.upper() == name or key.upper().endswith('_' + name)):
                    return value
        raise RotationBlocked('DEV_CREDENTIAL_FIELD_MISSING')
    email = field('EMAIL', 'USERNAME')
    data = await asyncio.wait_for(_get_session_browser(
        email=email, password=field('PASSWORD', 'PASS'),
        secret=field('TOTP_SECRET', '2FA_SECRET', 'SECRET', 'TOTP'),
        headless=False, keep_browser_open=False, log=lambda *_a, **_k: None), 240)
    if str((data.get('user') or {}).get('email', '')).casefold() != email.casefold():
        raise RotationBlocked('ACCOUNT_IDENTITY_UNVERIFIED')
    async with AsyncSession(impersonate='firefox135') as session:
        transport = RotationTransport(session, access_token=data.get('accessToken', ''),
                                      account_id=(data.get('account') or {}).get('id'))
        info = await transport('GET', '/accounts/mfa_info', None)
        factors = info.get('factors', {}).get('totp')
        if not isinstance(factors, list) or len(factors) != 1:
            raise RotationBlocked('OLD_FACTOR_REQUIRED')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--allow-live-read-only', action='store_true')
    args = parser.parse_args()
    if not args.allow_live_read_only:
        parser.exit(0, 'SKIP: explicit --allow-live-read-only required; no credentials read\n')
    logging.disable(logging.CRITICAL)
    from mfa_rotation import RotationBlocked
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            asyncio.run(check())
    except RotationBlocked as error:
        print('BLOCKED:', str(error))
        raise SystemExit(1)
    except Exception:
        print('BLOCKED: LIVE_READINESS_FAILED')
        raise SystemExit(1)
    print('PASS: read-only fresh MFA readiness; rotation NOT executed')
