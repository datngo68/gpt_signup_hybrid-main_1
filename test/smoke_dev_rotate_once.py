"""Explicit single-account live maintenance. Stable operation; never prints credentials."""
import argparse
import asyncio
import contextlib
import io
import logging
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def run():
    from dotenv import dotenv_values
    from config import load_settings
    from db import get_engine
    from mfa_rotation import RotationBlocked
    from mfa_rotation_login import fresh_rotation_session
    from mfa_rotation_http import RotationTransport
    from mfa_rotation_service import RotationService, atomic_write
    from curl_cffi.requests import AsyncSession
    from session_phase import get_session
    root = Path(__file__).resolve().parents[1]
    values = dotenv_values(root / '.dev.var')
    def field(*names):
        for name in names:
            for key, value in values.items():
                if value and (key.upper() == name or key.upper().endswith('_' + name)):
                    return value
        raise RotationBlocked('DEV_CREDENTIAL_FIELD_MISSING')
    email, password = field('EMAIL', 'USERNAME'), field('PASSWORD', 'PASS')
    service = RotationService(get_engine(), load_settings().runtime_dir / 'sessions')
    operation = 'authorized_dev_rotation_once_v1'
    state = service.journal.get(email)
    try:
        if state and state.get('operation_id') != operation:
            raise RotationBlocked('EXISTING_ROTATION_REQUIRES_REVIEW')
        if state and state['phase'] in ('activated', 'complete'):
            result = await service.run(email=email, password=password, operation_id=operation)
        else:
            old_secret = field('TOTP_SECRET', '2FA_SECRET', 'SECRET', 'TOTP')
            data, account_id = await fresh_rotation_session(email, password, old_secret)
            async with AsyncSession(impersonate='firefox135') as session:
                transport = RotationTransport(session, access_token=data['accessToken'], account_id=account_id)
                # Readiness + durable write preflight BEFORE production service can disable.
                info = await transport('GET', '/accounts/mfa_info', None)
                factors = info.get('factors', {}).get('totp')
                if not isinstance(factors, list) or (not state and len(factors) != 1):
                    raise RotationBlocked('OLD_FACTOR_REQUIRED')
                atomic_write(service.directory / '.rotation-readiness', 'durability preflight\n')
                with service.journal.lock(email, service.directory / '.rotation-locks'):
                    service.engine.raw_connection().execute('SELECT state FROM mfa_rotations').fetchall()
                result = await service.run(email=email, password=password, operation_id=operation, request=transport)
        # Verify actual login with canonical NEW secret; no second remote mutation.
        verified = await asyncio.wait_for(get_session(email=email, password=password, secret=result['secret'],
            headless=False, keep_browser_open=False, log=lambda *_a, **_k: None), 240)
        if not isinstance(verified, dict) or str((verified.get('user') or {}).get('email', '')).casefold() != email.casefold() or not verified.get('accessToken'):
            raise RotationBlocked('NEW_SECRET_LOGIN_NOT_VERIFIED')
        return 'SUCCESS phase=complete new_secret_login=verified source_secret=obsolete'
    except RotationBlocked as error:
        state = service.journal.get(email)
        return 'BLOCKED code=' + str(error) + ' phase=' + (state.get('phase', 'none') if state else 'none')
    except Exception as error:
        state = service.journal.get(email)
        message = str(error).lower()
        category = 'unclassified'
        for label, markers in [('password_rejected', ('password error', 'wrong password')), ('otp_rejected', ('invalid code', 'incorrect code')), ('challenge', ('captcha', 'challenge')), ('timeout', ('timeout', 'timed out')), ('network', ('network', 'net::', 'ns_error')), ('password_field_missing', ('password input not found',)), ('totp_field_missing', ('totp input not found',)), ('session_cookie_timeout', ('timeout waiting session cookies',)), ('session_token_missing', ('accessToken thiếu'.lower(), 'session json',)), ('browser_launch', ('browser launch', 'browser/driver')), ('browser_pipe', ('pipe', 'target closed')), ('session_fetch_http', ('http ',))]:
            if any(marker in message for marker in markers):
                category = label
                break
        return 'BLOCKED code=LIVE_TRANSPORT_OR_LOGIN_FAILED exception=' + type(error).__name__ + ' category=' + category + ' phase=' + (state.get('phase', 'none') if state else 'none')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--approve-single-account-rotation', action='store_true')
    args = parser.parse_args()
    if not args.approve_single_account_rotation:
        parser.exit(0, 'SKIP: explicit --approve-single-account-rotation required\n')
    logging.disable(logging.CRITICAL)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        status = asyncio.run(run())
    print(status)
    raise SystemExit(0 if status.startswith('SUCCESS') else 1)
