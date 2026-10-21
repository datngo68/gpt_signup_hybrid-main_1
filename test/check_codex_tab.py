"""Offline checks. No OAuth/SMS/network jobs are executed."""
from __future__ import annotations
import ast
import asyncio
import json
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['GPT_SIGNUP_WEB_TOKEN'] = 'codex-tab-offline-token'
from fastapi import FastAPI
from fastapi.testclient import TestClient
from web import codex_tab as tab
from db.repositories import _EXACT_KEYS, _validate_type_constraint, RepositoryError


def inputs():
    for value in (True, False, 0, 11, '2', 1.5, None):
        try: tab.validate_concurrency(value)
        except ValueError: pass
        else: raise AssertionError(value)
    assert tab.validate_concurrency(1) == 1
    assert tab.validate_concurrency(10) == 10
    for value in ('', 'x', 'a@b.com|', 'a@b.com|pass|bad-secret', 'a@b.com|pass|x|y', None):
        try: tab.parse_combos(value)
        except ValueError: pass
        else: raise AssertionError(value)
    assert len(tab.parse_combos('a@b.com|password\nb@c.com|password|ABCDEFGHIJKLMNOP')) == 2
    assert 'codex.concurrency' in _EXACT_KEYS
    _validate_type_constraint('ui.active_tab', 'codex')
    for value in (True, 0, 11, '1'):
        try: _validate_type_constraint('codex.concurrency', value)
        except RepositoryError: pass
        else: raise AssertionError(value)
    _validate_type_constraint('codex.concurrency', 10)
    print('PASS input validation / Settings key and type registration')


async def queue_checks():
    manager = tab.CodexTab()
    lock = threading.Lock()
    active = peak = 0
    def execute(credentials, ident, loop):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(.05)
        with lock: active -= 1
        return {'tokens': {'access_token': 'PRIVATE_TOKEN'}}
    manager._execute = execute
    rows = tab.parse_combos('\n'.join(f'a{i}@b.com|PRIVATE_PASSWORD' for i in range(6)))
    manager.start(rows, 2)
    await manager.task
    assert peak == 2
    snapshot = manager.snapshot()
    assert all(j['status'] == 'success' and j['phone_status'] == 'unknown' for j in snapshot['jobs'])
    assert 'PRIVATE' not in json.dumps(snapshot)
    manager.start(rows, 1)
    await asyncio.sleep(.01)
    manager.stop()
    assert any(j['status'] == 'stopping' for j in manager.jobs.values())
    assert any(j['status'] == 'stopped' for j in manager.jobs.values())
    assert not manager.task.done()
    await manager.shutdown()
    assert all(j['status'] == 'stopped' for j in manager.jobs.values())
    assert all('credentials' not in j and not j['download_ready'] for j in manager.jobs.values())
    ident = next(iter(manager.jobs))
    manager._event(ident, 'verified')
    assert manager.jobs[ident]['phone_status'] == 'verified'
    print('PASS concurrency limit / stop draining / status and credential redaction')


def routes():
    app = FastAPI()
    app.include_router(tab.router)
    client = TestClient(app)
    headers = {'X-API-Token': 'codex-tab-offline-token'}
    for method, path in [('GET','/jobs'), ('POST','/start'), ('POST','/stop'), ('GET','/jobs/test/auth.json')]:
        response = client.request(method, '/api/codex/tab' + path, json={})
        assert response.status_code == 401, (path, response.status_code)
    assert client.get('/api/codex/tab/jobs', headers=headers).status_code == 200
    assert client.post('/api/codex/tab/start', headers=headers, json={'combos':'bad'}).status_code == 400
    assert client.get('/api/codex/tab/jobs/missing/auth.json', headers=headers).status_code == 404
    tab.manager.jobs = {'test': dict(id='test', email='a@b.com', status='success', phone_status='verified', logs=['Đã xác minh số điện thoại'], download_ready=True, result={'tokens': {'access_token':'PRIVATE_TOKEN'}})}
    assert 'PRIVATE_TOKEN' not in client.get('/api/codex/tab/jobs', headers=headers).text
    response = client.get('/api/codex/tab/jobs/test/auth.json', headers=headers)
    assert response.status_code == 200 and response.json()['tokens']['access_token'] == 'PRIVATE_TOKEN'
    assert response.headers['cache-control'] == 'no-store'
    class Repo:
        def __init__(self, *args): pass
        def get(self, key): return 1
        def set(self, key, value): assert key == 'codex.concurrency' and value == 2
    with patch('db.get_engine', return_value=None), patch('db.repositories.SettingsRepository', Repo):
        for invalid in (True, 0, 11, '2'):
            response = client.post('/api/codex/tab/start', headers=headers, json={'combos':'a@b.com|password', 'concurrency':invalid})
            assert response.status_code == 400
        with patch.object(tab.manager, 'start') as start_mock:
            response = client.post('/api/codex/tab/start', headers=headers, json={'combos':'a@b.com|password', 'concurrency':2})
            assert response.status_code == 200
            assert start_mock.call_args.args[1] == 2
    print('PASS all endpoint auth / invalid input / protected download / token-free listing')


if __name__ == '__main__':
    for filename in ('web/codex_tab.py','web/server.py','db/repositories.py'):
        ast.parse((ROOT / filename).read_text(encoding='utf-8-sig'))
    inputs()
    asyncio.run(queue_checks())
    routes()
    print('PASS check_codex_tab.py (offline; no SMS calls, no renting, no signup)')
