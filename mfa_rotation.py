"""Fail-closed MFA rotation core; callers supply durable storage and account lock.

Transport follows Security bundle 759444.910ad8886c.js. No automatic retries
of destructive requests. Not exposed through Web until durable orchestration exists.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from totp_helper import generate_code, normalize_secret


class RotationBlocked(Exception):
    """Safe error code only: never include remote response bodies or secrets."""


async def rotate_totp(
    *,
    request: Callable[[str, str, dict | None], Awaitable[dict]],
    save: Callable[[dict], Awaitable[None]],
    state: dict[str, Any],
) -> dict[str, Any]:
    """Resume a journal while an external, account-scoped durable lock is held.

    ``request`` must enforce account identity, authenticated fresh MFA step-up,
    timeouts, status validation and JSON validation. ``save`` must commit before
    returning. The journal must already exist before invoking this function.
    Caller must sync exports before marking the journal complete.
    """
    journal = dict(state)

    async def checkpoint(phase: str) -> None:
        journal['phase'] = phase
        await save(dict(journal))

    async def factors() -> list[dict]:
        info = await request('GET', '/accounts/mfa_info', None)
        values = info.get('factors', {}).get('totp')
        if not isinstance(values, list) or any(
            not isinstance(item, dict) or not isinstance(item.get('id'), str)
            for item in values
        ):
            raise RotationBlocked('MFA_STATE_UNVERIFIED')
        return values

    old = journal.get('old_factor_id')
    if not isinstance(old, str) or not old:
        raise RotationBlocked('OLD_FACTOR_REQUIRED')
    phase = journal.get('phase')
    if phase == 'prepared':
        if old not in {item['id'] for item in await factors()}:
            raise RotationBlocked('OLD_FACTOR_NOT_ACTIVE')
        await checkpoint('disable_unknown')
        # Timeout leaves disable_unknown durable. Resume only reconciles.
        await request('POST', '/accounts/mfa/user/disable_in_house', {'factor_id': old})
        phase = 'disable_unknown'
    if phase == 'disable_unknown':
        current = await factors()
        if old in {item['id'] for item in current}:
            raise RotationBlocked('DISABLE_NOT_CONFIRMED')
        if current:
            raise RotationBlocked('UNEXPECTED_TOTP_FACTOR')
        await checkpoint('disabled')
        phase = 'disabled'
    if phase == 'disabled':
        # Enroll timeout cannot safely be replayed: server may hold a secret
        # whose response was lost. Manual reconciliation is required.
        await checkpoint('enroll_unknown')
        enrollment = await request('POST', '/accounts/mfa/enroll', {'factor_type': 'totp'})
        secret = normalize_secret(enrollment.get('secret', ''))
        session_id = enrollment.get('session_id')
        if not secret or not isinstance(session_id, str) or not session_id:
            raise RotationBlocked('ENROLL_RESPONSE_INVALID')
        generate_code(secret)  # Validate before persisting or activating.
        journal.update(secret=secret, session_id=session_id)
        await checkpoint('enrolled')  # Persistence failure MUST prevent activate.
        phase = 'enrolled'
    if phase == 'enrolled':
        await checkpoint('activate_unknown')
        result = await request('POST', '/accounts/mfa/user/activate_enrollment', {
            'factor_type': 'totp', 'session_id': journal['session_id'],
            'code': generate_code(journal['secret']),
        })
        if result.get('success') is not True:
            raise RotationBlocked('ACTIVATION_NOT_CONFIRMED')
        await checkpoint('activated')
        phase = 'activated'
    if phase == 'activated':
        return dict(journal)
    # No guessing based on generic error text, factor count or HTTP 200 alone.
    raise RotationBlocked('MANUAL_RECONCILIATION_REQUIRED')
