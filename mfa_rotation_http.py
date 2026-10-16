"""Strict HTTP transport for the proven in-house MFA endpoints.

No token refresh or login-endpoint substitution for Security step-up.
"""
from __future__ import annotations

import base64
import json
import time

from mfa_rotation import RotationBlocked


def require_fresh_mfa(token: str) -> dict:
    try:
        encoded = token.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
        now = time.time()
        expiry = claims['exp']
        if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not now < expiry:
            raise ValueError()
        age = now * 1000 - claims['pwd_auth_time']
        required = claims.get('https://api.openai.com/mfa', {}).get('required')
        if not 0 <= age < 240000 or required != 'yes':
            raise ValueError()
    except (ValueError, KeyError, IndexError, TypeError):
        raise RotationBlocked('REAUTH_REQUIRED') from None
    # Local parsing is a freshness gate, not JWT signature verification.
    # The server must authenticate the token and account on every request.
    return claims


class RotationTransport:
    def __init__(self, session, *, access_token: str, account_id: str):
        if not account_id or not isinstance(account_id, str):
            raise RotationBlocked('ACCOUNT_ID_REQUIRED')
        require_fresh_mfa(access_token)
        self.session = session
        self.headers = {'Authorization': 'Bearer ' + access_token,
                        'ChatGPT-Account-ID': account_id,
                        'Content-Type': 'application/json'}

    async def __call__(self, method, path, body):
        allowed = {
            ('GET', '/accounts/mfa_info'),
            ('POST', '/accounts/mfa/user/disable_in_house'),
            ('POST', '/accounts/mfa/enroll'),
            ('POST', '/accounts/mfa/user/activate_enrollment'),
        }
        if (method, path) not in allowed:
            raise RotationBlocked('UNSUPPORTED_MFA_REQUEST')
        response = await self.session.request(
            method, 'https://chatgpt.com/backend-api' + path,
            headers=self.headers, json=body, timeout=30,
            allow_redirects=False,
        )
        if response.status_code in (401, 403):
            raise RotationBlocked('REAUTH_REQUIRED')
        if not 200 <= response.status_code < 300:
            raise RotationBlocked('MFA_HTTP_ERROR')
        try:
            result = response.json()
        except (ValueError, TypeError):
            raise RotationBlocked('MFA_RESPONSE_INVALID') from None
        if not isinstance(result, dict):
            raise RotationBlocked('MFA_RESPONSE_INVALID')
        return result
