"""Read-only deployment check; never submits a rotation or prints credentials."""
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
BASE = 'http://127.0.0.1:8083'


def get(path, token=None):
    headers = {'X-API-Token': token} if token else {}
    try:
        with urlopen(Request(BASE + path, headers=headers), timeout=10) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()


if __name__ == '__main__':
    status, body = get('/')
    assert status == 200
    match = re.search(rb'<meta\s+name=[\"\'](?:api-token|auth-token|gpt-api-token)[\"\']\s+content=[\"\']([^\"\']+)', body)
    if not match:
        match = re.search(rb'<meta[^>]+name=[\"\']([^\"\']*token[^\"\']*)[\"\'][^>]+content=[\"\']([^\"\']+)', body)
        token = match.group(2).decode() if match else None
    else:
        token = match.group(1).decode()
    assert token, 'No loopback auth metadata; credentials not printed'
    status, body = get('/openapi.json')
    paths = json.loads(body)['paths']
    print('Live queue route in OpenAPI:', '/api/session/rotations' in paths)
    status, body = get('/api/session/rotations', token)
    print('Authenticated queue GET:', status)
    if status == 200:
        counts = Counter(item['status'] for item in json.loads(body)['items'])
        print('Queue status counts:', dict(counts))
        assert not counts['queued'] and not counts['running'], 'Queue active: do not restart'
    elif '--preflight' not in sys.argv:
        raise AssertionError('Live queue route absent')
    for path in ['/api/jobs', '/api/session/jobs', '/api/link/jobs', '/api/upi/jobs']:
        status, body = get(path, token)
        assert status == 200, (path, status)
        data = json.loads(body)
        jobs = data if isinstance(data, list) else data.get('jobs', [])
        counts = Counter(job['status'] for job in jobs)
        print(path, 'status counts:', dict(counts))
        assert not any(counts[s] for s in ('running', 'queued')), 'Jobs active: do not restart'
    for path in ['/api/icloud/run/status', '/api/icloud/autoreg/status']:
        status, body = get(path, token)
        assert status == 200, (path, status)
        data = json.loads(body)
        active = data.get('running', data.get('is_running', False))
        print(path, 'running:', active)
        assert not active, 'Runner active: do not restart'
    with sqlite3.connect((ROOT / 'runtime/data.db').as_uri() + '?mode=ro', uri=True) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'mfa_rotation_queue' in names:
            count = conn.execute("SELECT COUNT(*) FROM mfa_rotation_queue WHERE status IN ('queued','running')").fetchone()[0]
            print('Durable active queue count:', count)
            assert count == 0
        if 'mfa_rotations' in names:
            phases = Counter(json.loads(row[0]).get('phase') for row in conn.execute('SELECT state FROM mfa_rotations'))
            print('Journal phase counts:', dict(phases))
    import msvcrt
    locked = 0
    for path in (ROOT / 'runtime/sessions/.rotation-locks').glob('*.lock'):
        with path.open('r+b') as stream:
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                locked += 1
            else:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    print('Active rotation locks:', locked)
    assert locked == 0
    if '--preflight' not in sys.argv:
        assert {'get', 'post'} <= paths['/api/session/rotations'].keys()
        assert 'post' in paths['/api/session/rotations/stop']
    print('PASS: read-only deployment and restart safety checks')
