"""Run: .venv/Scripts/python.exe test/check_mfa_rotation.py"""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mfa_rotation import RotationBlocked, rotate_totp


async def check():
    for failure in ('none', 'save_secret', 'disable_timeout', 'false_success'):
        calls = []
        journal = []
        active = True

        async def save(state):
            if failure == 'save_secret' and state['phase'] == 'enrolled':
                raise OSError('storage unavailable')
            journal.append(dict(state))

        async def request(method, path, body):
            nonlocal active
            calls.append(path)
            if method == 'GET':
                return {'factors': {'totp': [{'id': 'old'}] if active else []}}
            if path.endswith('disable_in_house'):
                active = False
                if failure == 'disable_timeout':
                    raise TimeoutError()
                return {}
            if path.endswith('/enroll'):
                return {'secret': 'JBSWY3DPEHPK3PXP', 'session_id': 'pending'}
            assert journal[-1]['secret']
            return {'success': failure != 'false_success'}

        try:
            result = await rotate_totp(request=request, save=save,
                                       state={'phase': 'prepared', 'old_factor_id': 'old'})
        except (RotationBlocked, OSError, TimeoutError):
            assert failure != 'none'
        else:
            assert failure == 'none' and result['phase'] == 'activated'
        if failure == 'save_secret':
            assert not any(p.endswith('activate_enrollment') for p in calls)
        if failure == 'disable_timeout':
            assert journal[-1]['phase'] == 'disable_unknown'
            # Resume reconciles, never repeats disable.
            await rotate_totp(request=request, save=save, state=journal[-1])
            assert sum(p.endswith('disable_in_house') for p in calls) == 1
        if failure == 'false_success':
            assert journal[-1]['phase'] == 'activate_unknown'
    print('PASS: strict activation, persistence barrier, disable timeout reconciliation')


if __name__ == '__main__':
    asyncio.run(check())
