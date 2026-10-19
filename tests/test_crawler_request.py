"""HTTPS requests must use the canonical Host header when pinned to a public IP."""
from io import BytesIO
from urllib.parse import urlsplit

from app import crawler


def test_https_request_does_not_append_default_port_to_host(monkeypatch):
    sent = []

    class Socket:
        def sendall(self, payload):
            sent.append(payload)

        def makefile(self, mode):
            return BytesIO(b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 4\r\n\r\ntest')

        def close(self):
            pass

    class Context:
        def wrap_socket(self, sock, server_hostname):
            assert server_hostname == 'example.test'
            return sock

    monkeypatch.setattr(crawler, 'public_address', lambda url: (urlsplit(url), '93.184.216.34'))
    monkeypatch.setattr(crawler.socket, 'create_connection', lambda *args, **kwargs: Socket())
    monkeypatch.setattr(crawler.ssl, 'create_default_context', lambda: Context())

    final, body = crawler.request('https://example.test/')
    assert final == 'https://example.test/'
    assert body == 'test'
    request = b''.join(sent)
    assert b'Host: example.test\r\n' in request
    assert b'Host: example.test:443\r\n' not in request
