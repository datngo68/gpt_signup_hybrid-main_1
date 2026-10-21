"""Offline ZIP/auth/clear regression, no paid work."""
import io
import json
import os
from pathlib import Path
import sys
import zipfile
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['GPT_SIGNUP_WEB_TOKEN'] = 'bulk-offline-token'
from fastapi import FastAPI
from fastapi.testclient import TestClient
from web import codex_tab as tab
app = FastAPI()
app.include_router(tab.router)
client = TestClient(app)
headers = {'X-API-Token':'bulk-offline-token'}
def job(ident, status, tokens=None, email='fake@example.com'):
    return dict(id=ident, email=email,status=status,phone_status='unknown',logs=[],download_ready=status=='success',result={'tokens':tokens or {'access_token':'SECRET'}})
assert client.get('/api/codex/tab/auth.zip').status_code == 401
assert client.post('/api/codex/tab/clear').status_code == 401
tab.manager.jobs = {'a'*32:job('a'*32,'success'),'b'*32:job('b'*32,'error')}
response = client.get('/api/codex/tab/auth.zip',headers=headers)
assert response.status_code == 200 and response.headers['cache-control']=='no-store'
with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
    assert archive.namelist()==['a'*32+'/auth.json']
    assert json.loads(archive.read(archive.namelist()[0]))['tokens']['access_token']=='SECRET'
assert 'SECRET' not in client.get('/api/codex/tab/jobs',headers=headers).text
assert client.get('/api/codex/tab/9router.json').status_code == 401
tab.manager.jobs.update({
    'd'*32: job('d'*32, 'success', {'access_token':'ACCESS','refresh_token':'REFRESH','id_token':'ID','expires_in':3600,'account_id':'LOCAL'}, 'router@example.com'),
    'e'*32: job('e'*32, 'success', {'access_token':'ACCESS2','expires_in':12.5,'unknown':'NO'}),
    '0'*32: job('0'*32, 'success', {'access_token':'ACCESS3','expires_in':True}),
    'f'*32: job('f'*32, 'success', {'refresh_token':'NO_ACCESS'}),
})
export = client.get('/api/codex/tab/9router.json', headers=headers)
assert export.status_code == 200
assert export.headers['cache-control'] == 'no-store'
assert export.headers['content-disposition'] == 'attachment; filename="codex-9router.json"'
assert export.json() == {'accounts': [
    {'accessToken':'SECRET','email':'fake@example.com'},
    {'accessToken':'ACCESS','refreshToken':'REFRESH','idToken':'ID','expiresIn':3600,'email':'router@example.com'},
    {'accessToken':'ACCESS2','expiresIn':12.5,'email':'fake@example.com'},
    {'accessToken':'ACCESS3','email':'fake@example.com'},
]}
serialized = export.text
for forbidden in ('password','totp','phone','account_id','unknown','status','download_ready','logs'):
    assert forbidden not in serialized
assert 'ACCESS' not in client.get('/api/codex/tab/jobs',headers=headers).text
tab.manager.jobs = {'invalid': job('invalid', 'success', {'refresh_token':'ONLY'})}
empty = client.get('/api/codex/tab/9router.json', headers=headers)
assert empty.status_code == 409 and 'access token' in empty.json()['detail']
tab.manager.jobs = {'a'*32:job('a'*32,'success')}
tab.manager.jobs['c'*32]=job('c'*32,'stopping')
assert client.post('/api/codex/tab/clear',headers=headers).status_code==409
assert len(tab.manager.jobs)==2
del tab.manager.jobs['c'*32]
assert client.post('/api/codex/tab/clear',headers=headers).json()['jobs']==[]
assert client.get('/api/codex/tab/auth.zip',headers=headers).status_code==404
print('PASS ZIP + exact 9Router schema/auth/no-store/empty/optional tokens, token-free listing, atomic clear')
