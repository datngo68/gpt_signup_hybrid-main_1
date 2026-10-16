"""Offline production integration, export recovery and authenticated route checks."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.engine import DatabaseEngine
from mfa_rotation import RotationBlocked
from mfa_rotation_service import RotationService


async def check():
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
        root = Path(tmp)
        engine = DatabaseEngine(root / 'data.db')
        service = RotationService(engine, root / 'sessions')
        service.directory.mkdir()
        email = 'test@example.invalid'
        path = service.directory / 'signup.json'
        path.write_text(json.dumps({'email': email, 'password': 'password'}))
        with engine.get_connection() as conn:
            conn.execute('INSERT INTO chatgpt_accounts(email,password) VALUES (?,?)', (email, 'password'))
            conn.execute('INSERT INTO session_results(email,password) VALUES (?,?)', (email, 'password'))
            conn.execute("INSERT INTO jobs(id,email,combo,status,created_at,job_type,session_data) VALUES (?,?,?,?,?,?,?)",
                         ('test', email, email + '|password|OLD', 'success', 1, 'session', json.dumps({'user': {'email': email}, 'accessToken': 'stale'})))
        columns = {row['name'] for row in engine.raw_connection().execute('PRAGMA table_info(jobs)')}
        assert {'combo', 'session_data', 'secret', 'session_path'} <= columns
        calls = []
        async def request(method, url, body):
            calls.append(url)
            if url.endswith('mfa_info'):
                return {'factors': {'totp': [] if any(x.endswith('disable_in_house') for x in calls) else [{'id': 'old'}]}}
            if url.endswith('/enroll'):
                return {'secret': 'JBSWY3DPEHPK3PXP', 'session_id': 'new'}
            return {'success': True}
        with patch('mfa_rotation_service.atomic_write', side_effect=OSError('secret never returned')):
            try:
                await service.run(email=email, password='password', operation_id='rotate_test', request=request)
                raise AssertionError('export failure accepted')
            except RotationBlocked as error:
                assert str(error) == 'EXPORT_PENDING'
        assert service.journal.get(email)['phase'] == 'activated'
        count = len(calls)
        result = await service.run(email=email, password='password', operation_id='rotate_test')
        assert result['phase'] == 'complete' and len(calls) == count
        assert json.loads(path.read_text())['two_factor']['secret'] == result['secret']
        assert path.with_suffix('.2fa.json').exists()
        assert (service.directory / 'accounts.txt').read_text().count(email) == 1
        await service.run(email=email, password='password', operation_id='rotate_test')
        assert len(calls) == count
        assert engine.raw_connection().execute('SELECT secret_2fa FROM chatgpt_accounts').fetchone()[0] == result['secret']
        service.journal.save(email, dict(result, phase='activated'))
        service.recover()
        assert service.journal.get(email)['phase'] == 'complete'
        try:
            await service.run(email=email, password='password', operation_id='different_id')
            raise AssertionError('new rotation without authentication accepted')
        except RotationBlocked as error:
            assert str(error) == 'REAUTH_REQUIRED'
        async def next_request(method, url, body):
            if url.endswith('mfa_info') and len(calls) == count:
                return {'factors': {'totp': [{'id': 'old'}]}}
            return await request(method, url, body)
        repeated = await service.run(email=email, password='password', operation_id='different_id', request=next_request)
        assert repeated['phase'] == 'complete'
        new_count = len(calls)
        await service.run(email=email, password='password', operation_id='rotate_test')
        assert len(calls) == new_count
        row = engine.raw_connection().execute("SELECT combo,secret,session_data FROM jobs WHERE id='test'").fetchone()
        assert row['combo'].split('|')[2] == row['secret'] == repeated['secret']
        assert json.loads(row['session_data'])['two_factor']['secret'] == repeated['secret']
        assert json.loads(engine.raw_connection().execute('SELECT two_factor FROM session_results').fetchone()[0])['secret'] == repeated['secret']
        # Call actual route: authentication is explicit and precedes job lookup.
        from fastapi import HTTPException
        from starlette.requests import Request
        from web import server
        from web.auth import reset_token_for_tests
        reset_token_for_tests('offline-token')
        payload = server.RotateSessionTotpRequest(operation_id='rotate_test', confirmed=True)
        try:
            await server.rotate_session_totp('missing', payload, Request({'type': 'http', 'headers': [], 'query_string': b''}))
            raise AssertionError('unauthenticated rotation accepted')
        except HTTPException as error:
            assert error.status_code == 401
        job = SimpleNamespace(email=email, password='password', status='success', secret=None,
                              session_data={'user': {'email': email}, 'accessToken': 'stale', 'account': {'id': 'account'}})
        manager = SimpleNamespace(jobs={'test': job})
        authenticated = Request({'type': 'http', 'headers': [(b'x-api-token', b'offline-token')]})
        with patch.object(server, 'get_session_manager', return_value=manager), patch('db.get_engine', return_value=engine), patch.dict('os.environ', {'RUNTIME_DIR': str(root)}):
            response = await server.rotate_session_totp('test', payload, authenticated)
            assert json.loads(response.body)['phase'] == 'complete'
            assert result['secret'].encode() not in response.body
            assert job.secret == result['secret']
            service.journal.save(email, dict(result, phase='prepared'))
            async def stale_login(*args):
                raise RotationBlocked('REAUTH_REQUIRED')
            with patch('mfa_rotation_login.resolve_rotation_session', side_effect=stale_login):
                try:
                    await server.rotate_session_totp('test', payload, authenticated)
                    raise AssertionError('stale MFA accepted')
                except HTTPException as error:
                    assert error.detail == 'REAUTH_REQUIRED'
        engine.close()
    print('PASS: rotation API auth, stale MFA, durable exports, recovery, no duplicate rotation')


if __name__ == '__main__':
    asyncio.run(check())
