"""Offline queue checks: isolation, stop, crash recovery, selected API validation."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.engine import DatabaseEngine
from mfa_rotation_queue import RotationQueue


async def check():
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
        engine = DatabaseEngine(Path(tmp) / 'data.db')
        events, calls = [], []
        manager = SimpleNamespace(_broadcast=events.append)
        async def execute(job_id, operation_id):
            calls.append(job_id)
            if job_id == 'bad':
                raise RuntimeError('private error never emitted')
        queue = RotationQueue(engine, manager, execute)
        queue.enqueue([('bad', 'rotate_bad'), ('good', 'rotate_good')])
        await queue.task
        assert calls == ['bad', 'good']
        assert [row['status'] for row in queue.snapshot()] == ['error', 'success']
        assert 'private' not in json.dumps(events)
        queue.enqueue([('pending', 'rotate_pending')])
        queue.stop_pending()
        await queue.task
        assert 'pending' not in calls
        queue.update('good', 'running')
        recovered = RotationQueue(engine, manager, execute)
        assert recovered.snapshot()[1]['error'] == 'INTERRUPTED_CHECK_JOURNAL'
        recovered.start()
        await recovered.task
        assert calls == ['bad', 'good']
        removed = recovered.clear_finished()
        assert removed == 3
        assert recovered.snapshot() == []
        from fastapi import HTTPException
        from pydantic import ValidationError
        from starlette.requests import Request
        from web import server
        from web.auth import reset_token_for_tests
        reset_token_for_tests('offline-token')
        request = Request({'type': 'http', 'headers': [(b'x-api-token', b'offline-token')]})
        cleared = []
        session_manager = SimpleNamespace(clear_finished=lambda: 2)
        rotation_queue = SimpleNamespace(clear_finished=lambda: cleared.append(True))
        with patch.object(server, 'get_session_manager', return_value=session_manager), patch.object(server, 'get_rotation_queue', return_value=rotation_queue):
            response = await server.clear_finished_session_jobs()
            assert json.loads(response.body) == {'removed': 2}
            assert cleared == [True]
        try:
            server.QueueRotationsRequest(job_ids=[], confirmed=True)
            raise AssertionError('empty selection accepted')
        except ValidationError:
            pass
        jobs = {'good': SimpleNamespace(email='good@example.invalid', status='success', session_data={}),
                'bad': SimpleNamespace(email='bad@example.invalid', status='error', session_data=None)}
        queued = []
        stub = SimpleNamespace(enqueue=queued.extend, snapshot=lambda: [])
        async def metadata(*args):
            return SimpleNamespace(body=b'{"operation_id":"rotate_good"}')
        with patch.object(server, 'get_session_manager', return_value=SimpleNamespace(jobs=jobs)), patch.object(server, 'get_rotation_queue', return_value=stub), patch.object(server, 'rotation_operation_metadata', metadata):
            await server.queue_rotations(server.QueueRotationsRequest(job_ids=['good', 'good'], confirmed=True), request)
            assert queued == [('good', 'rotate_good')]
            try:
                await server.queue_rotations(server.QueueRotationsRequest(job_ids=['bad'], confirmed=True), request)
                raise AssertionError('failed account accepted')
            except HTTPException as exc:
                assert exc.status_code == 409
        engine.close()
    print('PASS: selected queue, empty selection, deduplication, failure isolation, pending stop, no ambiguous rerun')


if __name__ == '__main__':
    asyncio.run(check())
