"""Durable rotation journal, deliberately separate from enrollment pending.

The account lock is OS-backed: released after process death, never lease-expired
while a destructive request may still be running. Recovery always reads journal.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path


class RotationJournal:
    def __init__(self, engine):
        self.engine = engine

    def install(self):
        with self.engine.get_connection() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS mfa_rotations (
                email TEXT PRIMARY KEY COLLATE NOCASE,
                state TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )''')

    def get(self, email):
        row = self.engine.raw_connection().execute(
            'SELECT state FROM mfa_rotations WHERE email = ?', (email,)
        ).fetchone()
        return json.loads(row['state']) if row else None

    def save(self, email, state):
        with self.engine.get_connection() as conn:
            conn.execute('''INSERT INTO mfa_rotations(email,state) VALUES (?,?)
                ON CONFLICT(email) DO UPDATE SET state=excluded.state,
                updated_at=CURRENT_TIMESTAMP''', (email, json.dumps(state)))

    @contextlib.contextmanager
    def lock(self, email, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(email.strip().casefold().encode()).hexdigest()
        with (directory / (name + '.lock')).open('a+b') as stream:
            if stream.tell() == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
