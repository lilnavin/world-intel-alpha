"""Offline regressions for hostile RSS destinations and pinned connections."""
from types import SimpleNamespace
import errno
import io
import socket
import ssl
import requests
import urllib3

import pytest

from worldintel.core import enrich, feeds, safe_fetch


def test_enrichment_does_not_fetch_loopback_from_feed():
    calls = []
    response = SimpleNamespace(status_code=200, headers={'Content-Type': 'text/html'},
                               text='<p>private service content</p>', raise_for_status=lambda: None)
    session = SimpleNamespace(get=lambda *a, **k: calls.append(a) or response)
    assert enrich._fetch_html(session, 'http://127.0.0.1/private', 2) == ''
    assert calls == []


@pytest.mark.parametrize('url', [
    'file:///private', 'ftp://public.example/x', 'https://user:secret@public.example.org/',
    'http://127.0.0.1/', 'http://10.0.0.1/', 'http://172.16.0.1/', 'http://192.168.1.1/',
    'http://169.254.169.254/', 'http://100.64.0.1/', 'http://0.0.0.0/',
    'http://192.0.2.1/', 'http://198.18.0.1/', 'http://224.0.0.1/', 'http://240.0.0.1/',
    'http://[::1]/', 'http://[::]/', 'http://[fc00::1]/', 'http://[fe80::1]/',
    'http://[::ffff:127.0.0.1]/', 'http://[::ffff:10.0.0.1]/', 'http://[2001:db8::1]/',
    'http://[fe80::1%25eth0]/', 'https://public.example:0/', 'https://public.example:65536/',
    'http://public.example\\@127.0.0.1/', 'https://public.example/\r\nInjected: x',
])
def test_rejects_unsafe_destinations_before_connection(url, monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: _dns('93.184.216.34'))
    calls = _stub_pools(monkeypatch, [_response()])
    with pytest.raises(ValueError):
        safe_fetch.public_get(url, timeout=2, user_agent='GenericBrowser')
    assert calls == []


def _dns(ip, port=443):
    family = socket.AF_INET6 if ':' in ip else socket.AF_INET
    address = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', address)]


@pytest.mark.parametrize('addresses', [[], _dns('127.0.0.1'), _dns('::1'),
    _dns('93.184.216.34') + _dns('10.0.0.1'),
    _dns('2606:4700:4700::1111') + _dns('::ffff:192.168.1.1')])
def test_all_dns_answers_must_be_public(addresses, monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: addresses)
    with pytest.raises(safe_fetch.UnsafeURL):
        safe_fetch.public_get('https://public.example/', timeout=2, user_agent='GenericBrowser')


def _stub_pools(monkeypatch, responses):
    calls = []
    class Pool:
        def __init__(self, host, port, **kwargs):
            self.destination = (host, port, kwargs)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def urlopen(self, method, path, **kwargs):
            calls.append((self.destination, method, path, kwargs))
            return responses[min(len(calls) - 1, len(responses) - 1)]
    monkeypatch.setattr(safe_fetch, '_HTTPPool', Pool)
    monkeypatch.setattr(safe_fetch, '_HTTPSPool', Pool)
    return calls


def _response(status=200, location=None):
    return SimpleNamespace(status=status, headers={'Location': location} if location else
                           {'Content-Type': 'text/html; charset=utf-8'}, data=b'<p>Public news</p>')


@pytest.mark.parametrize('target', ['http://127.0.0.1/private', 'http://[::1]/private',
    'https://user:secret@public.example.org/', 'file:///private', 'https://private.example/'])
def test_redirect_destinations_are_checked_before_connecting(target, monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, *a, **k:
                        _dns('10.0.0.1' if host == 'private.example' else '93.184.216.34'))
    calls = _stub_pools(monkeypatch, [_response(302, target)])
    with pytest.raises(safe_fetch.UnsafeURL):
        safe_fetch.public_get('https://public.example/', timeout=2, user_agent='GenericBrowser')
    assert len(calls) == 1


def test_https_redirect_cannot_downgrade_to_plaintext(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: _dns('93.184.216.34'))
    calls = _stub_pools(monkeypatch, [_response(302, 'http://public.example/insecure')])
    with pytest.raises(safe_fetch.UnsafeURL):
        safe_fetch.public_get('https://public.example/', timeout=2, user_agent='GenericBrowser')
    assert len(calls) == 1


def test_redirect_limit_is_finite(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: _dns('93.184.216.34'))
    calls = _stub_pools(monkeypatch, [_response(302, '/again')])
    with pytest.raises(safe_fetch.UnsafeURL, match='redirect limit'):
        safe_fetch.public_get('https://public.example/', timeout=2, user_agent='GenericBrowser')
    assert len(calls) == 6


def test_public_redirect_preserves_content_timeout_and_omits_ambient_auth(monkeypatch, tmp_path):
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:9999')
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:9999')
    netrc = tmp_path / 'synthetic.netrc'
    netrc.write_text('machine public.example login synthetic password SYNTHETIC_SECRET')
    monkeypatch.setenv('NETRC', str(netrc))
    monkeypatch.setattr(requests.sessions.Session, 'request',
                        lambda *a, **k: pytest.fail('Ambient session consulted'))
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: _dns('93.184.216.34'))
    calls = _stub_pools(monkeypatch, [_response(302, '/article?q=1'), _response()])
    response = safe_fetch.public_get('https://public.example/', timeout=3, user_agent='GenericBrowser')
    assert response.text == '<p>Public news</p>'
    assert response.url == 'https://public.example/article?q=1'
    assert [c[2] for c in calls] == ['/', '/article?q=1']
    for destination, method, path, kwargs in calls:
        assert method == 'GET'
        assert destination[:2] == ('public.example', 443)
        assert destination[2]['cert_reqs'] == 'CERT_REQUIRED'
        assert destination[2]['ca_certs']
        assert kwargs['headers'] == {'User-Agent': 'GenericBrowser'}
        assert kwargs['redirect'] is False
        assert kwargs['retries'] is False
        assert kwargs['timeout'].connect_timeout == kwargs['timeout'].read_timeout == 3


class _Socket:
    def __init__(self, *args):
        self.connections = []
        self.sent = []
        self.timeouts = []
        self.closed = False
    def settimeout(self, timeout):
        self.timeouts.append(timeout)
    def setsockopt(self, *args):
        pass
    def connect(self, address):
        self.connections.append(address)
    def sendall(self, data):
        self.sent.append(data)
    def makefile(self, *args):
        return io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 11\r\n\r\nPublic news')
    def close(self):
        self.closed = True


@pytest.mark.parametrize('scheme', ['http', 'https'])
def test_actual_transport_connects_only_to_vetted_address_with_original_host(scheme, monkeypatch):
    resolved = []
    def resolve(host, port, **kwargs):
        resolved.append((host, port))
        assert len(resolved) == 1, 'Re-resolution could allow DNS rebinding'
        return _dns('93.184.216.34', port)
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    sock = _Socket()
    monkeypatch.setattr(socket, 'socket', lambda *a, **k: sock)
    tls_calls = []
    def wrap(**kwargs):
        tls_calls.append(kwargs)
        return sock, True
    monkeypatch.setattr(urllib3.connection, '_ssl_wrap_socket_and_match_hostname', wrap)
    response = safe_fetch.public_get(f'{scheme}://public.example:8443/article',
                                    timeout=3, user_agent='GenericBrowser')
    assert response.text == 'Public news'
    assert resolved == [('public.example', 8443)]
    assert sock.connections == [('93.184.216.34', 8443)]
    request = b''.join(sock.sent)
    assert b'GET /article HTTP/1.1\r\n' in request
    assert b'Host: public.example:8443\r\n' in request
    assert b'Authorization:' not in request
    assert all(t == 3 for t in sock.timeouts)
    if scheme == 'https':
        assert tls_calls[0]['server_hostname'] == 'public.example'
        assert tls_calls[0]['cert_reqs'] in (ssl.CERT_REQUIRED, 'CERT_REQUIRED')
        assert tls_calls[0]['assert_hostname'] is not False
        assert tls_calls[0]['ca_certs']
    else:
        assert tls_calls == []


@pytest.mark.parametrize('scheme', ['http', 'https'])
@pytest.mark.parametrize('failure_stage', ['creation', 'connect'])
def test_actual_transport_falls_back_to_vetted_ipv4(scheme, failure_stage, monkeypatch):
    resolved = []
    def resolve(host, port, **kwargs):
        resolved.append((host, port))
        assert len(resolved) == 1, 'Fallback must not resolve the hostname again'
        return _dns('2606:4700:4700::1111', port) + _dns('93.184.216.34', port)
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    failed_sock, connected_sock = _Socket(), _Socket()
    def fail_connect(address):
        raise OSError(errno.ENETUNREACH, 'IPv6 unavailable')
    failed_sock.connect = fail_connect
    families = []
    def create_socket(family, socktype, proto):
        families.append(family)
        if family == socket.AF_INET6:
            if failure_stage == 'creation':
                raise OSError(errno.EAFNOSUPPORT, 'IPv6 unavailable')
            return failed_sock
        return connected_sock
    monkeypatch.setattr(socket, 'socket', create_socket)
    monkeypatch.setattr(urllib3.connection, '_ssl_wrap_socket_and_match_hostname',
                        lambda **kwargs: (connected_sock, True))
    response = safe_fetch.public_get(f'{scheme}://public.example:8443/article',
                                    timeout=3, user_agent='GenericBrowser')
    assert response.text == 'Public news'
    assert resolved == [('public.example', 8443)]
    assert families == [socket.AF_INET6, socket.AF_INET]
    assert connected_sock.connections == [('93.184.216.34', 8443)]
    assert b'Host: public.example:8443\r\n' in b''.join(connected_sock.sent)
    assert failed_sock.closed is (failure_stage == 'connect')


@pytest.mark.parametrize('failure_stage', ['creation', 'connect'])
def test_all_vetted_socket_failures_raise_existing_connection_error(failure_stage, monkeypatch):
    addresses = _dns('2606:4700:4700::1111') + _dns('93.184.216.34')
    families, sockets = [], []
    failure = OSError(errno.EAFNOSUPPORT, 'No supported socket')
    def fail_connect(address):
        raise failure
    def create_socket(family, socktype, proto):
        families.append(family)
        if failure_stage == 'creation':
            raise failure
        sock = _Socket()
        sock.connect = fail_connect
        sockets.append(sock)
        return sock
    monkeypatch.setattr(socket, 'socket', create_socket)
    monkeypatch.setattr(socket, 'getaddrinfo',
                        lambda *a, **k: pytest.fail('Pinned connection must not resolve DNS'))
    connection = safe_fetch._PinnedHTTPConnection('public.example', timeout=3,
                                                 validated_addresses=addresses)
    with pytest.raises(urllib3.exceptions.NewConnectionError,
                       match='Public destination connection failed') as exc:
        connection._new_conn()
    assert exc.value.__cause__ is failure
    assert families == [socket.AF_INET6, socket.AF_INET]
    assert all(sock.closed for sock in sockets)


def test_feeds_and_google_helper_cannot_bypass_public_boundary():
    session = SimpleNamespace(get=lambda *a, **k: pytest.fail('Caller session must be ignored'))
    health = {}
    assert feeds._fetch_one(session, {'name': 'hostile', 'url': 'http://127.0.0.1/rss'}, health) == []
    assert health['feed:hostile']['ok'] is False
    assert feeds._resolve_gnews(session, 'http://127.0.0.1/?news.google.com') == ''
