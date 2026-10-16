"""Strict transport checks, no remote requests or real credentials."""
import asyncio
import base64
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mfa_rotation import RotationBlocked
from mfa_rotation_http import RotationTransport, require_fresh_mfa


def token(claims):
    value = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return 'test.' + value + '.test'


async def main():
    valid = token({'exp': time.time() + 3600, 'pwd_auth_time': time.time() * 1000,
                   'https://api.openai.com/mfa': {'required': 'yes'}})
    require_fresh_mfa(valid)
    for value in ('invalid', token({'pwd_auth_time': 0}), token({'pwd_auth_time': time.time() * 1000})):
        try:
            require_fresh_mfa(value)
        except RotationBlocked as exc:
            assert str(exc) == 'REAUTH_REQUIRED'
        else:
            raise AssertionError('invalid step-up accepted')

    class Response:
        status_code = 403
        def json(self):
            raise AssertionError('must not read error body')

    class Session:
        calls = 0
        async def request(self, *args, **kwargs):
            self.calls += 1
            assert kwargs['allow_redirects'] is False
            return Response()

    session = Session()
    transport = RotationTransport(session, access_token=valid, account_id='test-account')
    try:
        await transport('POST', '/accounts/mfa/user/disable_in_house', {'factor_id': 'test'})
    except RotationBlocked as exc:
        assert str(exc) == 'REAUTH_REQUIRED'
    else:
        raise AssertionError('403 accepted')
    assert session.calls == 1
    try:
        await transport('POST', '/unverified-endpoint', {})
    except RotationBlocked:
        pass
    else:
        raise AssertionError('unknown endpoint accepted')
    assert session.calls == 1
    print('PASS: step-up freshness, required MFA claim, no retries, endpoint allowlist, safe 403')


if __name__ == '__main__':
    asyncio.run(main())
