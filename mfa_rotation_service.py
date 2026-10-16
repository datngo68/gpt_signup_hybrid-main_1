"""Durable rotation orchestration. Export failures resume without remote mutation."""
from __future__ import annotations

import json
import os
from pathlib import Path

from db.mfa_rotation import RotationJournal
from mfa_rotation import RotationBlocked, rotate_totp


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.rotation.tmp')
    with temporary.open('w', encoding='utf-8', newline='\n') as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class RotationService:
    def __init__(self, engine, directory: Path):
        self.engine = engine
        self.directory = directory
        self.journal = RotationJournal(engine)
        self.journal.install()

    def sync(self, email: str, state: dict) -> dict:
        """Idempotent DB + file projection; journal stays activated until all succeed."""
        secret = state['secret']
        factor = {'enabled': True, 'secret': secret}
        with self.engine.get_connection() as conn:
            conn.execute('UPDATE session_results SET two_factor=? WHERE lower(email)=lower(?)',
                         (json.dumps(factor), email))
            conn.execute('UPDATE chatgpt_accounts SET secret_2fa=? WHERE lower(email)=lower(?)',
                         (secret, email))
            rows = conn.execute('SELECT id, combo, session_data FROM jobs WHERE lower(email)=lower(?)',
                                (email,)).fetchall()
            for row in rows:
                parts = row['combo'].split('|')
                if len(parts) == 3:
                    parts[2] = secret
                data = json.loads(row['session_data']) if row['session_data'] else None
                if isinstance(data, dict):
                    data['two_factor'] = factor
                conn.execute('UPDATE jobs SET secret=?, combo=?, session_data=? WHERE id=?',
                             (secret, '|'.join(parts), json.dumps(data) if data else None, row['id']))
        self.directory.mkdir(parents=True, exist_ok=True)
        matched = False
        for path in self.directory.glob('*.json'):
            if path.name.endswith('.2fa.json'):
                continue
            data = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data, dict) or str(data.get('email', '')).casefold() != email.casefold():
                continue
            matched = True
            data['two_factor'] = factor
            atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False))
            atomic_write(path.with_suffix('.2fa.json'), json.dumps({
                'email': email, 'user_id': data.get('user_id'), 'two_factor': factor,
            }, indent=2))
        if not matched:
            import hashlib
            name = hashlib.sha256(email.encode()).hexdigest()
            data = {'email': email, 'password': state['password'], 'two_factor': factor}
            atomic_write(self.directory / ('rotation-' + name + '.json'), json.dumps(data, indent=2))
            atomic_write(self.directory / ('rotation-' + name + '.2fa.json'), json.dumps(data, indent=2))
        # Serialize the shared aggregate across different account rotations too.
        with self.journal.lock('__accounts_export__', self.directory / '.rotation-locks'):
            path = self.directory / 'accounts.txt'
            lines = path.read_text(encoding='utf-8').splitlines() if path.exists() else []
            lines = [line for line in lines if line.split('|')[0].casefold() != email.casefold()]
            lines.append('|'.join((email, state['password'], secret)))
            atomic_write(path, '\n'.join(lines) + '\n')
        state = dict(state, phase='complete')
        self.journal.save(email, state)
        return state

    async def run(self, *, email, password, operation_id, request=None):
        email = email.strip().casefold()
        try:
            with self.journal.lock(email, self.directory / '.rotation-locks'):
                state = self.journal.get(email)
                completed_operations = list(state.get('completed_operations', [])) if state else []
                if state and operation_id in completed_operations:
                    if state['phase'] != 'complete':
                        raise RotationBlocked('ROTATION_IN_PROGRESS')
                    return state
                if state and state.get('operation_id') != operation_id:
                    if state['phase'] != 'complete':
                        raise RotationBlocked('ROTATION_ALREADY_EXISTS')
                    completed_operations.append(state['operation_id'])
                    state = None
                if state is None:
                    if request is None:
                        raise RotationBlocked('REAUTH_REQUIRED')
                    info = await request('GET', '/accounts/mfa_info', None)
                    factors = info.get('factors', {}).get('totp')
                    if not isinstance(factors, list) or len(factors) != 1 or not isinstance(factors[0], dict) or not factors[0].get('id'):
                        raise RotationBlocked('OLD_FACTOR_REQUIRED')
                    state = {'phase': 'prepared', 'old_factor_id': factors[0]['id'],
                             'operation_id': operation_id, 'password': password,
                             'completed_operations': completed_operations}
                    self.journal.save(email, state)
                if state['phase'] == 'complete':
                    return state
                if state['phase'] != 'activated':
                    if request is None:
                        raise RotationBlocked('REAUTH_REQUIRED')
                    async def save(value):
                        self.journal.save(email, value)
                    state = await rotate_totp(request=request, save=save, state=state)
                try:
                    return self.sync(email, state)
                except Exception:
                    raise RotationBlocked('EXPORT_PENDING') from None
        except OSError:
            raise RotationBlocked('ROTATION_BUSY') from None

    def recover(self):
        rows = self.engine.raw_connection().execute('SELECT email,state FROM mfa_rotations').fetchall()
        for row in rows:
            state = json.loads(row['state'])
            if state.get('phase') == 'activated':
                try:
                    with self.journal.lock(row['email'], self.directory / '.rotation-locks'):
                        self.sync(row['email'], state)
                except Exception:
                    # Journal remains authoritative; explicit API resume reports safe error.
                    continue
