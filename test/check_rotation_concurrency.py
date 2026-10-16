"""Offline controlled-event checks; no network or credentials."""
import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.engine import DatabaseEngine
from mfa_rotation_queue import RotationQueue

async def check():
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
        engine = DatabaseEngine(Path(tmp) / 'data.db')
        limit = 2
        started = {str(i): asyncio.Event() for i in range(8)}
        release = {str(i): asyncio.Event() for i in range(8)}
        active = set()
        peak = 0
        events = []
        async def execute(job_id, operation_id):
            nonlocal peak
            active.add(job_id)
            peak = max(peak, len(active))
            started[job_id].set()
            try:
                await release[job_id].wait()
                if job_id == '0':
                    raise RuntimeError('secret must not escape')
            finally:
                active.remove(job_id)
        queue = RotationQueue(engine, SimpleNamespace(_broadcast=events.append), execute,
                              max_concurrent=lambda: limit)
        queue.enqueue([(str(i), 'op' + str(i)) for i in range(7)])
        queue.enqueue([('0', 'duplicate')])
        await asyncio.wait_for(started['0'].wait(), 2)
        await asyncio.wait_for(started['1'].wait(), 2)
        assert peak == 2 and not started['2'].is_set()
        release['0'].set()
        await asyncio.wait_for(started['2'].wait(), 2)
        assert len(active) == 2 and queue.snapshot()[0]['error'] == 'ROTATION_FAILED'
        limit = 3
        await asyncio.wait_for(started['3'].wait(), 2)
        assert len(active) == 3
        limit = 1
        release['1'].set()
        release['2'].set()
        await asyncio.sleep(.3)
        assert not started['4'].is_set()
        release['3'].set()
        await asyncio.wait_for(started['4'].wait(), 2)
        assert active == {'4'}
        queue.stop_pending()
        assert all(r['status'] == 'cancelled' for r in queue.snapshot()[5:])
        queue.enqueue([('7', 'op7')])
        queue.closing = True
        waiter = asyncio.ensure_future(asyncio.shield(queue.task))
        await asyncio.sleep(.05)
        assert not waiter.done() and not started['7'].is_set()
        release['4'].set()
        await asyncio.wait_for(waiter, 2)
        assert not active and not started['7'].is_set() and peak == 3
        assert 'secret must not escape' not in str(events)
        queue.update('7', 'running')
        recovered = RotationQueue(engine, SimpleNamespace(_broadcast=events.append), execute)
        assert recovered.snapshot()[-1]['error'] == 'INTERRUPTED_CHECK_JOURNAL'
        recovered.start()
        await recovered.task
        assert not started['7'].is_set()
        # Shutdown must await every active child, not just the first completion.
        limit = 2
        for job_id in ('5', '6'):
            release[job_id].clear()
        draining = RotationQueue(engine, SimpleNamespace(_broadcast=events.append), execute,
                                 max_concurrent=lambda: limit)
        draining.enqueue([('5', 'again5'), ('6', 'again6')])
        await asyncio.wait_for(started['5'].wait(), 2)
        await asyncio.wait_for(started['6'].wait(), 2)
        draining.closing = True
        release['5'].set()
        await asyncio.sleep(.3)
        assert not draining.task.done() and active == {'6'}
        release['6'].set()
        await asyncio.wait_for(draining.task, 2)
        assert not active
        engine.close()
    print('PASS: overlap, cap, refill, dynamic limits, error isolation, pending stop, graceful drain, recovery')

if __name__ == '__main__':
    asyncio.run(check())
