"""Existing Gateway llm-task transport. Run only behind the owned process deadline."""
import ipaddress
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit, urlunsplit
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

MAX_RESPONSE = 16 * 1024 * 1024


def endpoint(url):
    parts = urlsplit(url)
    if parts.username or parts.password or parts.query or parts.fragment or not parts.hostname:
        raise ValueError('unsupported gateway URL')
    loopback = parts.hostname == 'localhost'
    try:
        loopback = loopback or ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        pass
    if parts.scheme != 'https' and not (parts.scheme == 'http' and loopback):
        raise ValueError('unsupported gateway URL')
    if parts.path not in ('', '/', '/tools/invoke'):
        raise ValueError('unsupported gateway path')
    return urlunsplit((parts.scheme, parts.netloc, '/tools/invoke', '', ''))


def extract(raw):
    envelope = json.loads(raw)
    if envelope.get('ok') is not True:
        raise ValueError('gateway failure')
    result = envelope.get('result', {})
    if result.get('isError') is True:
        raise ValueError('gateway tool failure')
    blocks = result.get('content', [])
    if len(blocks) != 1 or blocks[0].get('type') != 'text':
        raise ValueError('unsupported gateway content')
    text = blocks[0].get('text')
    if not isinstance(text, str) or not text.strip():
        raise ValueError('unsupported gateway content')
    parsed = json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if not isinstance(parsed, dict):
        raise ValueError('unsupported gateway content')
    details = result.get('details', {})
    if 'json' in details and details['json'] != parsed:
        raise ValueError('conflicting gateway content')
    return text


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    try:
        timeout = float(os.environ['WORLD_INTEL_TIMEOUT_S'])
        if timeout <= 0:
            raise ValueError()
        schema = json.loads(Path(os.environ['WORLD_INTEL_SCHEMA_PATH']).read_text(encoding='utf-8'))
        body = {'tool':'llm-task', 'args':{'prompt':sys.stdin.buffer.read().decode('utf-8'), 'input':{}, 'schema':schema, 'timeoutMs':max(1,int(timeout * 1000))}}
        if os.environ.get('WORLD_INTEL_OPENCLAW_AGENT_ID'):
            body['agentId'] = os.environ['WORLD_INTEL_OPENCLAW_AGENT_ID']
        token = os.environ[os.environ.get('WORLD_INTEL_OPENCLAW_TOKEN_ENV','OPENCLAW_GATEWAY_TOKEN')]
        if not token or '\r' in token or '\n' in token:
            raise ValueError()
        req = Request(endpoint(os.environ['WORLD_INTEL_OPENCLAW_GATEWAY_URL']), data=json.dumps(body).encode('utf-8'), headers={'Authorization':'Bearer ' + token, 'Content-Type':'application/json'}, method='POST')
        opener = build_opener(ProxyHandler({}), NoRedirect())
        try:
            with opener.open(req, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
        except HTTPError as error:
            sys.stdout.buffer.write(error.read(MAX_RESPONSE + 1))
            sys.stdout.buffer.flush()
            raise ValueError('gateway failed') from None
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()
        if len(raw) > MAX_RESPONSE:
            raise ValueError()
        text = extract(raw)
        Path(os.environ['WORLD_INTEL_OUTPUT_PATH']).write_bytes(text.encode('utf-8'))
        return 0
    except Exception:
        sys.stderr.write('Native host completion failed or is unsupported.\n')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
