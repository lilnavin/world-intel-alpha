"""Public-only GETs for untrusted feeds/articles, with DNS-pinned sockets.

Each hop owns its pool. Caller sessions, environment proxies, netrc, cookies and
authentication are deliberately excluded from this acquisition boundary.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import requests
import urllib3
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.exceptions import NewConnectionError


class UnsafeURL(ValueError):
    """A destination is not an unauthenticated public HTTP(S) URL."""


def _public_address(address):
    ip = ipaddress.ip_address(address)
    return (ip.is_global and not ip.is_reserved and not ip.is_multicast
            and not ip.is_loopback and not ip.is_link_local and not ip.is_private
            and not ip.is_unspecified
            and (not getattr(ip, 'ipv4_mapped', None)
                 or _public_address(ip.ipv4_mapped)))


def _destination(url):
    if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 for c in url) or '\\' in url:
        raise UnsafeURL('Invalid public URL')
    parsed = urlsplit(url)
    if (parsed.scheme.lower() not in ('http', 'https') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or '%' in parsed.hostname):
        raise UnsafeURL('Public HTTP(S) destination required')
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme.lower() == 'https' else 80
    if not 1 <= port <= 65535:
        raise UnsafeURL('Invalid destination port')
    host = parsed.hostname.encode('idna').decode('ascii')
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    else:
        family = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
        addresses = [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, '',
                      (host, port, 0, 0) if literal.version == 6 else (host, port))]
    if not addresses or any(not _public_address(entry[4][0]) for entry in addresses):
        raise UnsafeURL('Non-public destination blocked')
    return parsed, host, port, tuple(addresses)


class _PinnedConnection:
    def __init__(self, *args, validated_addresses, **kwargs):
        self._validated_addresses = validated_addresses
        super().__init__(*args, **kwargs)

    def _new_conn(self):
        # Do not call create_connection: it would resolve the hostname again.
        error = None
        for family, socktype, proto, _, sockaddr in self._validated_addresses:
            sock = None
            try:
                sock = socket.socket(family, socktype, proto)
                sock.settimeout(self.timeout)
                for option in self.socket_options or ():
                    sock.setsockopt(*option)
                sock.connect(sockaddr)
                return sock
            except OSError as exc:
                error = exc
                if sock is not None:
                    sock.close()
        raise NewConnectionError(self, 'Public destination connection failed') from error


class _PinnedHTTPConnection(_PinnedConnection, HTTPConnection):
    pass


class _PinnedHTTPSConnection(_PinnedConnection, HTTPSConnection):
    pass


class _HTTPPool(urllib3.HTTPConnectionPool):
    ConnectionCls = _PinnedHTTPConnection


class _HTTPSPool(urllib3.HTTPSConnectionPool):
    ConnectionCls = _PinnedHTTPSConnection


def public_get(url: str, *, timeout: float, user_agent: str) -> requests.Response:
    """GET a public URL, rechecking and pinning each of at most five redirects."""
    for hop in range(6):
        parsed, host, port, addresses = _destination(url)
        # Preparation only quotes path/query characters; no Session/env lookup.
        prepared = requests.Request('GET', url).prepare()
        target = urlsplit(prepared.url)
        path = target.path or '/'
        if target.query:
            path += '?' + target.query
        https = parsed.scheme.lower() == 'https'
        pool_type = _HTTPSPool if https else _HTTPPool
        tls = {'cert_reqs': 'CERT_REQUIRED', 'ca_certs': requests.certs.where()} if https else {}
        with pool_type(host, port, validated_addresses=addresses, **tls) as pool:
            raw = pool.urlopen('GET', path, headers={'User-Agent': user_agent},
                               timeout=urllib3.Timeout(connect=timeout, read=timeout),
                               redirect=False, retries=False, preload_content=True)
            response = requests.Response()
            response.status_code = raw.status
            response.headers = requests.structures.CaseInsensitiveDict(raw.headers)
            response._content = raw.data
            response.encoding = requests.utils.get_encoding_from_headers(response.headers)
            response.url = prepared.url
        location = response.headers.get('Location')
        if response.status_code not in (301, 302, 303, 307, 308) or not location:
            return response
        if hop == 5:
            raise UnsafeURL('Public redirect limit exceeded')
        url = urljoin(response.url, location)
        if https and urlsplit(url).scheme.lower() == 'http':
            raise UnsafeURL('HTTPS redirect downgrade blocked')
    raise UnsafeURL('Public redirect limit exceeded')  # pragma: no cover
