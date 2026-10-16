"""Offline checks: rotation session selection, no live account requests."""
import asyncio
import base64
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mfa_rotation import RotationBlocked
import mfa_rotation_login as login


def data(**claims):
    payload = {'exp': time.time() + 3600, 'pwd_auth_time': time.time() * 1000,
               'email': 'test@example.invalid', 'https://api.openai.com/mfa': {'required': 'yes'},
               'https://api.openai.com/auth': {'chatgpt_account_id': 'account'}}
    payload.update(claims)
    token = 'x.' + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=') + '.x'
    return {'user': {'email': 'test@example.invalid'}, 'account': {'id': 'account'}, 'accessToken': token}


async def check():
    job = SimpleNamespace(email='test@example.invalid', password='dummy', secret='JBSWY3DPEHPK3PXP',
                          reg_mode='pure_request', session_data=data())
    manager = SimpleNamespace(headless=True, _job_timeout=30,
                              _begin_job_proxy=AsyncMock(return_value='http://proxy.invalid'))
    with patch('session_phase.get_session', new_callable=AsyncMock) as browser, patch('session_phase.get_session_pure_request', new_callable=AsyncMock, return_value=data()) as http:
        result, account = await login.resolve_rotation_session(job, manager, job.secret)
        assert result is job.session_data and account == 'account'
        browser.assert_not_called(); http.assert_not_called(); manager._begin_job_proxy.assert_not_called()
        job.session_data = data(exp=time.time() - 1)
        await login.resolve_rotation_session(job, manager, job.secret)
        http.assert_awaited_once(); browser.assert_not_called()
        assert http.call_args.kwargs['proxy'] == 'http://proxy.invalid'
        job.session_data = data(pwd_auth_time=0)
        job.reg_mode = 'browser'
        browser.return_value = data()
        await login.resolve_rotation_session(job, manager, job.secret)
        assert browser.call_args.kwargs['headless'] is True
        assert browser.call_args.kwargs['proxy'] == 'http://proxy.invalid'
        assert browser.call_args.kwargs['keep_browser_open'] is False
        count = browser.await_count + http.await_count
        job.session_data = data()
        job.session_data['user']['email'] = 'other@example.invalid'
        try:
            await login.resolve_rotation_session(job, manager, job.secret)
            raise AssertionError('identity mismatch accepted')
        except RotationBlocked as exc:
            assert str(exc) == 'ACCOUNT_IDENTITY_UNVERIFIED'
        assert browser.await_count + http.await_count == count
        job.session_data = data()
        job.session_data['account']['id'] = 'other'
        try:
            await login.resolve_rotation_session(job, manager, job.secret)
            raise AssertionError('account mismatch accepted')
        except RotationBlocked as exc:
            assert str(exc) == 'ACCOUNT_IDENTITY_UNVERIFIED'
    from web import server
    from fastapi import HTTPException
    job.session_data = data()
    job.status = 'success'
    manager.jobs = {'test': job}
    journal = SimpleNamespace(get=lambda _email: None)
    service = SimpleNamespace(journal=journal)
    payload = server.RotateSessionTotpRequest(operation_id='rotate_offline', confirmed=True)
    events = []
    class Transport:
        def __init__(self, *args, **kwargs):
            pass
        async def __call__(self, method, path, body):
            events.append(method)
            if len(events) == 1:
                raise RotationBlocked('REAUTH_REQUIRED')
            return {}
    async def run(**kwargs):
        await kwargs['request']('GET', '/accounts/mfa_info', None)
        return {'phase': 'complete', 'secret': job.secret}
    service.run = run
    with patch.object(server, 'get_session_manager', return_value=manager), patch('db.get_engine'), patch('mfa_rotation_service.RotationService', return_value=service), patch('mfa_rotation_http.RotationTransport', Transport), patch.object(login, 'fresh_rotation_session', new_callable=AsyncMock, return_value=(data(), 'account')) as fresh:
        await server._rotate_session_totp('test', payload)
        assert events == ['GET', 'GET'] and fresh.await_count == 1
        events.clear(); fresh.reset_mock()
        async def mutate(**kwargs):
            await kwargs['request']('POST', '/accounts/mfa/user/disable_in_house', {})
        service.run = mutate
        try:
            await server._rotate_session_totp('test', payload)
            raise AssertionError('ambiguous POST accepted')
        except HTTPException as exc:
            assert exc.detail == 'REAUTH_REQUIRED'
        assert events == ['POST']; fresh.assert_not_called()
        journal.get = lambda _email: {'phase': 'activated'}
        async def export(**kwargs):
            assert 'request' not in kwargs
            return {'phase': 'complete', 'secret': job.secret}
        service.run = export
        with patch.object(login, 'resolve_rotation_session', side_effect=AssertionError('activated must not login')):
            await server._rotate_session_totp('test', payload)
    print('PASS: reuse zero logins; expired/stale fallback; pure HTTP; browser settings; identity/account rejection; GET reauth once; POST never retried; activated zero login')

if __name__ == '__main__':
    asyncio.run(check())
