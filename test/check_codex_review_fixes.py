"""Offline regression checks: no network, rentals, or account creation."""
import ast
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_function(path, name, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8-sig"))
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    node.decorator_list = []
    namespace.update(Any=object, Optional=__import__('typing').Optional, Callable=__import__('typing').Callable, LogFn=object, SmsProvider=object)
    exec(compile(ast.Module(body=[node], type_ignores=[]), path, 'exec'), namespace)
    return namespace[name]


async def check():
    import db
    import db.repositories
    engine = object()
    repo = Mock()
    values = {'codex.sms_provider': 'chothuesimcode', 'codex.sms_api_key': 'fake', 'codex.sms_service_id': None, 'codex.mode': 'browser'}
    repo.get.side_effect = values.get
    factory = Mock(return_value='provider')
    ns = {'create_sms_provider': factory}
    resolve = load_function('codex_auth/runner.py', '_resolve_sms_provider', ns)
    mode = load_function('codex_auth/runner.py', '_resolve_codex_mode', ns)
    with patch.object(db, 'get_engine', return_value=engine), patch.object(db.repositories, 'SettingsRepository', return_value=repo) as constructor:
        assert resolve(log=lambda _: None) == 'provider'
        assert mode() is False
        constructor.assert_called_with(engine)
    factory.assert_called_once_with('chothuesimcode', 'fake', None)

    class Session:
        calls = 0
        def request(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError('Network must not run after deadline')
        def get(self, *args, **kwargs):
            return self.request(*args, **kwargs)
        def close(self):
            pass

    session = Session()
    ns = dict(asyncio=asyncio, time=SimpleNamespace(monotonic=lambda: 1), curl_requests=SimpleNamespace(Session=lambda **kw: session), generate_pkce=lambda: 'pkce', generate_state=lambda: 'state', build_authorize_url=lambda *a, **k: 'url', REDIRECT_URI='uri', _normalize_proxy=lambda p: p)
    api = load_function('codex_auth/api_runner.py', 'get_codex_auth_api', ns)
    try:
        await api(email='fake', password='fake', overall_timeout=0, log=lambda _: None)
    except TimeoutError:
        pass
    else:
        raise AssertionError('Expired API budget accepted')
    assert session.calls == 0

    # AST checks isolate the SMS timeout handler without browser/network dependencies.
    tree = ast.parse((ROOT / 'codex_auth/browser.py').read_text(encoding='utf-8-sig'))
    handler = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_handle_phone_verification')
    otp_try = next(n for n in ast.walk(handler) if isinstance(n, ast.Try) and any(isinstance(c, ast.Attribute) and c.attr == 'get_otp' for stmt in n.body for c in ast.walk(stmt)))
    assert any(isinstance(n, ast.Raise) for n in ast.walk(otp_try.handlers[0]))
    assert not any(isinstance(n, ast.Continue) for n in ast.walk(otp_try.handlers[0]))

    class Response:
        def __init__(self, data):
            self.data = data
    ns = dict(JSONResponse=Response, _get_settings_repo=lambda: repo, os=SimpleNamespace(environ={}))
    config = load_function('web/server.py', 'get_codex_sms_config', ns)
    import codex_auth.sms
    with patch.object(codex_auth.sms, 'get_env_sms_key', return_value=''):
        assert (await config()).data['service_id'] == '1005'
        values['codex.sms_provider'] = 'viotp'
        assert (await config()).data['service_id'] == '1234'

    js = (ROOT / 'web/static/settings_panel.js').read_text(encoding='utf-8')
    assert '["", "1234", "1005"].indexOf(dom.codexSmsServiceId.value.trim()) !== -1' in js
    runner = (ROOT / 'codex_auth/runner.py').read_text(encoding='utf-8')
    assert 'GPT_SIGNUP_DB_PATH' not in runner
    assert 'overall_timeout=max(0.0, deadline - time.monotonic())' in runner
    for path in ['codex_auth/runner.py', 'codex_auth/api_runner.py', 'codex_auth/browser.py', 'web/server.py']:
        ast.parse((ROOT / path).read_text(encoding='utf-8-sig'))
    print('PASS: DB singleton, expired API budget, SMS fail-safe, provider defaults, fallback budget, syntax')


if __name__ == '__main__':
    asyncio.run(check())
