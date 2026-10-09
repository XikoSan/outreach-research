"""Profile-scoped local credentials; never returned in profile/run payloads."""
import json
import os
import tempfile
from .config import settings
GLOBAL_ID='0'*32

def location(sid):
    if len(sid)!=32 or any(c not in '0123456789abcdef' for c in sid):raise ValueError('Invalid profile id')
    return settings.data_dir/'credentials'/f'{sid}.json'

def read(sid):
    path=location(sid)
    if not path.exists():return {}
    return json.loads(path.read_text())

def write(sid, value):
    path=location(sid);path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    os.chmod(path.parent,0o700)
    fd,temp=tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:json.dump(value,f)
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)

def status(sid=GLOBAL_ID):
    value=read(sid)
    return {'configured':bool(value.get('key')),'state':value.get('state','missing'),'checked_at':value.get('checked_at')}

def for_run(run):
    value=read(GLOBAL_ID)
    if value.get('provider')=='chatgpt' and value.get('access_token') and value.get('expires_at',0)>__import__('time').time()+60:return value['access_token']
    return value.get('key','')

def uses_chatgpt(run=None):
    value=read(GLOBAL_ID)
    return value.get('provider')=='chatgpt' and bool(value.get('access_token')) and value.get('expires_at',0)>__import__('time').time()+60

def migrate_global():
    # Run once. Never pick arbitrarily when old profiles used different accounts.
    marker=location(GLOBAL_ID).parent/'workspace-migrated'
    if marker.exists():return
    keys={v['key'] for p in marker.parent.glob('*.json') if p.stem!=GLOBAL_ID and (v:=read(p.stem)).get('key')}
    if not read(GLOBAL_ID).get('key') and len(keys)==1:write(GLOBAL_ID,{'key':keys.pop(),'state':'saved'})
    marker.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    marker.touch(mode=0o600)
