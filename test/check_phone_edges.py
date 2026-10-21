"""Offline phone regressions: no real SMS or signup."""
import asyncio
import ast
from pathlib import Path
import sys
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from codex_auth import sms, browser, runner, api_runner
from codex_auth.errors import PhoneVerificationRequired


def providers():
    with patch.object(sms.urllib.request, 'urlopen', side_effect=OSError('https://host/?token=PRIVATE_KEY')):
        try: sms._http_get_json('https://host/?token=PRIVATE_KEY')
        except sms.SmsError as exc:
            assert 'PRIVATE_KEY' not in str(exc) and exc.__cause__ is None
        else: raise AssertionError('Expected error')
    for provider, responses in [(sms.ViotpProvider('key'), [
                                   {'status_code':200,'data':[{'id':1234,'name':'OpenAI | ChatGPT'}]},
                                   {'status_code':200,'data':{'request_id':'r'}},
                               ]),
                               (sms.ChothuesimcodeProvider('key'), [{'ResponseCode':0,'Result':{'Id':'r'}}])]:
        with patch.object(sms, '_http_get_json', side_effect=responses), patch.object(provider, 'cancel', return_value=True) as cancel:
            try: provider.request_phone()
            except sms.SmsError: pass
            else: raise AssertionError('Malformed rental accepted')
            cancel.assert_called_once_with('r')
    print('PASS HTTP secret redaction / malformed rental cleanup')


async def flows():
    provider = Mock()
    provider.request_phone.return_value = ('r', '0912345678')
    provider.cancel.return_value = True
    provider.get_otp = AsyncMock(side_effect=sms.SmsError('timeout'))
    page = Mock()
    page.url = 'https://auth.openai.com/add-phone'
    page.keyboard.press = AsyncMock()
    page.inner_text = AsyncMock(return_value='')
    field = Mock()
    field.click = AsyncMock()
    field.type = AsyncMock()
    field.fill = AsyncMock()
    with patch.object(browser, '_first_visible', AsyncMock(side_effect=[None,field,None,field])), patch.object(browser, '_click_first', AsyncMock(return_value=True)), patch.object(browser.asyncio, 'sleep', AsyncMock()):
        try: await browser.handle_phone_verification(page, provider, log=lambda _:None)
        except PhoneVerificationRequired: pass
        else: raise AssertionError('Missing OTP accepted')
    assert provider.request_phone.call_count == 1
    provider.cancel.assert_called()
    provider.reset_mock()
    with patch.object(browser, '_first_visible', AsyncMock(side_effect=RuntimeError('DOM failure'))):
        try: await browser.handle_phone_verification(page, provider, log=lambda _:None)
        except RuntimeError: pass
        else: raise AssertionError('Expected DOM error')
    provider.cancel.assert_called_once_with('r')
    with patch.object(runner, '_resolve_sms_provider', return_value=None), patch.object(api_runner, 'get_codex_auth_api', AsyncMock(side_effect=PhoneVerificationRequired('OTP timeout'))) as api:
        try: await runner.get_codex_auth(email='a@b.com',password='p',use_api=True,log=lambda _:None)
        except PhoneVerificationRequired: pass
        else: raise AssertionError('SMS failure replayed')
        assert api.await_count == 1
    with patch.object(runner, '_resolve_sms_provider', return_value=None), patch.object(api_runner, 'get_codex_auth_api', AsyncMock(return_value={'tokens':{}})):
        result = await runner.get_codex_auth(email='a@b.com',password='p',use_api=True,log=lambda _:None)
        assert result == {'tokens':{}}
    events = []
    async def fake_api(**kwargs):
        return {'tokens': {}}
    with patch.object(runner, '_resolve_sms_provider', return_value=None), patch.object(api_runner, 'get_codex_auth_api', fake_api):
        await runner.get_codex_auth(email='a@b.com', password='p', use_api=True, phone_state=events.append, log=lambda _:None)
    assert events == ['not_required']
    for outcome in ('verified', 'timeout', 'rejected'):
        events = []
        async def gated_api(**kwargs):
            kwargs['phone_state']('required')
            if outcome != 'verified':
                raise PhoneVerificationRequired(outcome)
            kwargs['phone_state']('verified')
            return {'tokens': {}}
        with patch.object(runner, '_resolve_sms_provider', return_value=None), patch.object(api_runner, 'get_codex_auth_api', gated_api):
            try:
                await runner.get_codex_auth(email='a@b.com', password='p', use_api=True, phone_state=events.append, log=lambda _:None)
            except PhoneVerificationRequired:
                assert outcome != 'verified'
        assert events == (['required','verified'] if outcome == 'verified' else ['required'])
    from types import SimpleNamespace
    class FakeBrowser:
        async def new_page(self): return object()
    class FakeCamoufox:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return FakeBrowser()
        async def __aexit__(self, *args): pass
    async def challenge(**kwargs):
        kwargs['phone_state']('required')
        raise api_runner.CloudflareChallengeRequired('challenge')
    events = []
    with patch.object(runner, '_resolve_sms_provider', return_value=None), patch.object(api_runner, 'get_codex_auth_api', challenge), patch.dict(sys.modules, {'camoufox.async_api':SimpleNamespace(AsyncCamoufox=FakeCamoufox)}), patch.object(runner, 'drive_oauth', AsyncMock(return_value=SimpleNamespace(code='code'))), patch.object(runner, 'exchange_code_for_tokens', return_value={}), patch.object(runner, 'build_auth_dot_json', return_value={'tokens':{'account_id':'fake'}}):
        await runner.get_codex_auth(email='a@b.com',password='p',use_api=True,fetch_api_key=False,phone_state=events.append,log=lambda _:None)
    assert events == ['required'], events
    print('PASS OTP absent cleanup / DOM failure cleanup / no duplicate fallback / no-SMS no-phone auth')
    print('PASS structured not_required / verified / timeout-rejected required / fallback preserves required')


if __name__ == '__main__':
    for name in ('browser','api_runner','runner','sms'):
        ast.parse((ROOT/'codex_auth'/f'{name}.py').read_text(encoding='utf-8-sig'))
    providers()
    asyncio.run(flows())
    print('PASS check_phone_edges.py (offline)')
