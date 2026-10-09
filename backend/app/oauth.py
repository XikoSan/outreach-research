"""Sign in with ChatGPT for local Plus-plan usage (PKCE, loopback callback)."""
import base64, hashlib, json, secrets, time
from urllib.parse import urlencode
import httpx, jwt
from jwt import PyJWKClient
from .config import settings
from . import credentials

AUTH='https://auth.openai.com/api/accounts/authorize'; TOKEN='https://auth.openai.com/api/accounts/oauth/token'; RESOURCE='https://api.openai.com/v1'; REDIRECT='http://127.0.0.1:8000/api/auth/chatgpt/callback'
_pending={}

def start():
    verifier=base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b'=').decode();state=secrets.token_urlsafe(32);nonce=secrets.token_urlsafe(24)
    host='urn:uuid:'+secrets.token_hex(16)
    _pending[state]={'verifier':verifier,'nonce':nonce,'host':host,'created':time.time()}
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    params={'client_id':'dynamic_agent_client','agent_name_hint':'XTECH Research','ext_agent_host_id':host,'response_type':'code','redirect_uri':REDIRECT,'scope':'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct','resource':RESOURCE,'state':state,'nonce':nonce,'code_challenge_method':'S256','code_challenge':challenge}
    return AUTH+'?'+urlencode(params)

def callback(params):
    state=params.get('state','');pending=_pending.pop(state,None)
    if not pending or time.time()-pending['created']>600:raise ValueError('OAuth state недействителен или истёк')
    if params.get('error'):raise ValueError('Вход отменён')
    code=params.get('code');client_id=params.get('client_id')
    if not code or not client_id:raise ValueError('OAuth callback неполный')
    response=httpx.post(TOKEN,data={'grant_type':'authorization_code','authorization_code':code,'client_id':client_id,'code_verifier':pending['verifier'],'redirect_uri':REDIRECT,'resource':RESOURCE},timeout=20)
    response.raise_for_status();data=response.json();token=data.get('id_token');access=data.get('access_token')
    if not token or not access:raise ValueError('OAuth не вернул токены')
    jwks=PyJWKClient('https://auth.openai.com/.well-known/jwks.json').get_signing_key_from_jwt(token)
    claims=jwt.decode(token,jwks.key,algorithms=['RS256'],audience=client_id,issuer='https://auth.openai.com')
    scopes=set((data.get('scope') or '').split())
    if 'chatgpt.tokens.use.direct' not in scopes:raise ValueError('У аккаунта не разрешено использование лимитов ChatGPT')
    credentials.write(credentials.GLOBAL_ID,{'provider':'chatgpt','client_id':client_id,'access_token':access,'refresh_token':data.get('refresh_token',''),'id_token':token,'scopes':sorted(scopes),'subject':claims.get('sub'),'email':claims.get('email',''),'expires_at':time.time()+int(data.get('expires_in',3600)),'state':'verified'})
    return {'email':claims.get('email',''),'scopes':sorted(scopes)}
