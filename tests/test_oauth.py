from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
import pytest
from app import oauth

@pytest.mark.parametrize('valid_nonce', [True, False])
def test_code_exchange_and_nonce(monkeypatch, valid_nonce):
    query = parse_qs(urlparse(oauth.start()).query)
    state = query['state'][0]
    pending = dict(oauth._pending[state])
    saved = []
    def post(url, *, data, timeout):
        assert url == oauth.TOKEN
        assert data == {'grant_type': 'authorization_code', 'code': 'test-code',
                        'client_id': 'oaiapp_test', 'code_verifier': pending['verifier'],
                        'redirect_uri': oauth.REDIRECT, 'resource': oauth.RESOURCE}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
            'id_token': 'test-id-token', 'access_token': 'test-access-token',
            'scope': 'chatgpt.tokens.use.direct', 'expires_in': 3600})
    monkeypatch.setattr(oauth.httpx, 'post', post)
    monkeypatch.setattr(oauth, 'PyJWKClient', lambda url: SimpleNamespace(
        get_signing_key_from_jwt=lambda token: SimpleNamespace(key='test-key')))
    monkeypatch.setattr(oauth.jwt, 'decode', lambda *a, **k: {
        'sub': 'test-account', 'nonce': pending['nonce'] if valid_nonce else 'wrong'})
    monkeypatch.setattr(oauth.credentials, 'write', lambda *args: saved.append(args))
    params = {'state': state, 'code': 'test-code', 'client_id': 'oaiapp_test'}
    if valid_nonce:
        oauth.callback(params)
        assert len(saved) == 1
    else:
        with pytest.raises(ValueError, match='nonce'):
            oauth.callback(params)
        assert not saved
    with pytest.raises(ValueError, match='state'):
        oauth.callback(params)
