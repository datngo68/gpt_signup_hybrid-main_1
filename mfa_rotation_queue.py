"""Bounded durable rotation queue. Interrupted mutations require explicit retry."""
from __future__ import annotations

import asyncio


class RotationQueue:
    def __init__(self, engine, manager, execute, *, max_concurrent=lambda: 1):
        self.engine, self.manager, self.execute = engine, manager, execute
        self.max_concurrent = max_concurrent
        self.task = None
        self.closing = False
        with engine.get_connection() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS mfa_rotation_queue (
                job_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL,
                status TEXT NOT NULL, error TEXT)''')
            conn.execute("UPDATE mfa_rotation_queue SET status='error', error='INTERRUPTED_CHECK_JOURNAL' WHERE status='running'")

    def snapshot(self):
        return [dict(row) for row in self.engine.raw_connection().execute(
            'SELECT job_id,operation_id,status,error FROM mfa_rotation_queue ORDER BY rowid')]

    def publish(self):
        self.manager._broadcast({'type': 'rotation', 'items': self.snapshot()})

    def enqueue(self, operations):
        with self.engine.get_connection() as conn:
            for job_id, operation_id in operations:
                row = conn.execute('SELECT status FROM mfa_rotation_queue WHERE job_id=?', (job_id,)).fetchone()
                if row and row['status'] in ('queued', 'running'):
                    continue
                conn.execute('''INSERT INTO mfa_rotation_queue VALUES (?,?,'queued',NULL)
                    ON CONFLICT(job_id) DO UPDATE SET operation_id=excluded.operation_id,status='queued',error=NULL''',
                             (job_id, operation_id))
        self.publish()
        self.start()

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    def stop_pending(self):
        with self.engine.get_connection() as conn:
            conn.execute("UPDATE mfa_rotation_queue SET status='cancelled' WHERE status='queued'")
        self.publish()

    def update(self, job_id, status, error=None):
        with self.engine.get_connection() as conn:
            conn.execute('UPDATE mfa_rotation_queue SET status=?,error=? WHERE job_id=?', (status, error, job_id))
        self.publish()

    async def _execute(self, row):
        try:
            await self.execute(row['job_id'], row['operation_id'])
        except asyncio.CancelledError:
            self.update(row['job_id'], 'error', 'INTERRUPTED_CHECK_JOURNAL')
            raise
        except Exception as exc:
            from fastapi import HTTPException
            error = exc.detail if isinstance(exc, HTTPException) else 'ROTATION_FAILED'
            self.update(row['job_id'], 'error', str(error))
        else:
            self.update(row['job_id'], 'success')

    async def run(self):
        active = set()
        try:
            while not self.closing:
                while len(active) < max(1, self.max_concurrent()):
                    row = self.engine.raw_connection().execute(
                        "SELECT job_id,operation_id FROM mfa_rotation_queue WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
                    if row is None:
                        break
                    self.update(row['job_id'], 'running')
                    active.add(asyncio.create_task(self._execute(row)))
                if not active:
                    return
                # Recheck live settings even when every active mutation is waiting.
                done, active = await asyncio.wait(active, timeout=0.25,
                                                  return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
        finally:
            # Never leave destructive child tasks behind when the scheduler exits.
            if active:
                await asyncio.shield(asyncio.gather(*active))
