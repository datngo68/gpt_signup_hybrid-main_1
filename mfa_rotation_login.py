"""Verified session reuse and mode-aware reauthentication; no mutation."""
from __future__ import annotations
import asyncio
from mfa_rotation import RotationBlocked
from mfa_rotation_http import require_fresh_mfa
from totp_helper import normalize_secret, generate_code


def verified_rotation_session(data, email, expected_account=None):
    if not isinstance(data, dict) or not isinstance(data.get('user'), dict):
        raise RotationBlocked('ACCOUNT_IDENTITY_UNVERIFIED')
    if str(data['user'].get('email', '')).casefold() != email.casefold():
        raise RotationBlocked('ACCOUNT_IDENTITY_UNVERIFIED')
    token = data.get('accessToken')
    if not isinstance(token, str):
        raise RotationBlocked('REAUTH_REQUIRED')
    claims = require_fresh_mfa(token)
    auth = claims.get('https://api.openai.com/auth') or {}
    account_id = auth.get('chatgpt_account_id')
    if not isinstance(account_id, str) or not account_id:
        raise RotationBlocked('ACCOUNT_ID_REQUIRED')
    account = data.get('account')
    if (isinstance(account, dict) and account.get('id') and account['id'] != account_id
            or expected_account and expected_account != account_id
            or claims.get('email') and str(claims['email']).casefold() != email.casefold()):
        raise RotationBlocked('ACCOUNT_IDENTITY_UNVERIFIED')
    return data, account_id


async def fresh_rotation_session(email, password, old_secret, *, reg_mode='browser',
                                 headless=True, proxy=None, timeout=240):
    from session_phase import get_session, get_session_pure_request
    from config import env_insecure_tls
    try:
        secret = normalize_secret(old_secret)
        generate_code(secret)
    except Exception:
        raise RotationBlocked('OLD_SECRET_INVALID') from None
    kwargs = dict(email=email, password=password, secret=secret, proxy=proxy,
                  log=lambda *_a, **_k: None)
    if reg_mode == 'pure_request':
        call = get_session_pure_request
    elif reg_mode in ('browser', 'hybrid'):
        call = get_session
        kwargs.update(headless=headless, tls_insecure=env_insecure_tls(), keep_browser_open=False)
    else:
        raise RotationBlocked('UNSUPPORTED_LOGIN_MODE')
    try:
        data = await asyncio.wait_for(call(**kwargs), timeout=timeout)
    except Exception:
        raise RotationBlocked('FRESH_LOGIN_REQUIRED') from None
    return verified_rotation_session(data, email)


async def resolve_rotation_session(job, manager, old_secret, *, force=False):
    data = job.session_data
    expected_account = None
    if isinstance(data, dict):
        user = data.get('user')
        if isinstance(user, dict) and user.get('email') and str(user['email']).casefold() != job.email.casefold():
            raise RotationBlocked('ACCOUNT_IDENTITY_UNVERIFIED')
        account = data.get('account')
        if isinstance(account, dict):
            expected_account = account.get('id')
        try:
            verified = verified_rotation_session(data, job.email)
        except RotationBlocked as exc:
            if str(exc) != 'REAUTH_REQUIRED':
                raise
        else:
            if not force:
                return verified
            expected_account = verified[1]
    if not old_secret:
        raise RotationBlocked('OLD_SECRET_REQUIRED')
    proxy = await manager._begin_job_proxy(job, lambda *_a, **_k: None)
    fresh, account_id = await fresh_rotation_session(
        job.email, job.password, old_secret, reg_mode=job.reg_mode,
        headless=manager.headless, proxy=proxy, timeout=manager._job_timeout)
    return verified_rotation_session(fresh, job.email, expected_account)
