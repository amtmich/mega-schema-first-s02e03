#!/usr/bin/env python3
"""Remote MCP 2026-07-28, with 2025-11-25 compatibility. Standard library only."""
import argparse
import base64
import collections
import html
import http.cookies
import json
import os
from pathlib import Path
import re
import signal
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from auth import Auth, OAuthError, parameters
from storage import Store
from tools import BY_NAME, DEFINITIONS, Tools, validate

ROOT = Path(__file__).resolve().parent
CURRENT = '2026-07-28'
LEGACY = '2025-11-25'
VERSIONS = [CURRENT, LEGACY]
PREFIX = 'io.modelcontextprotocol/'
INFO = {'name': 'schema-first', 'version': '1.0.0', 'title': 'Schema First tools'}
MAX_BODY = 28_000_000
CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"


class RPCError(ValueError):
    def __init__(self, code, message, status=400, data=None):
        self.code, self.message, self.status, self.data = code, message, status, data
        super().__init__(message)


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()


def read_config():
    config = {}
    path = ROOT / 'private' / 'server.env'
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                config[k.strip()] = v.strip().strip('\"\'')
    config.update(os.environ)
    return config


class Application:
    def __init__(self, config, transcriber=None):
        self.config = config
        self.origin = config.get('PUBLIC_URL', 'http://127.0.0.1:8765').rstrip('/')
        p = urllib.parse.urlsplit(self.origin)
        if p.path or p.query or p.fragment or p.username or not p.hostname:
            raise ValueError('PUBLIC_URL must be an origin, without a path or credentials')
        if p.scheme != 'https' and not (p.scheme == 'http' and p.hostname in ('localhost', '127.0.0.1', '::1')):
            raise ValueError('PUBLIC_URL requires HTTPS except on loopback')
        self.store = Store(config.get('DATA_DIR', str(ROOT / 'private' / 'data')))
        self.auth = Auth(self.store, self.origin, config.get('OWNER_PASSWORD', ''))
        self.tools = Tools(self.store, self.origin, config, **({'transcriber': transcriber} if transcriber else {}))
        self.allowed_origins = {self.origin, *filter(None, config.get('ALLOWED_ORIGINS', '').split(','))}
        self.limits, self.lock = {}, threading.Lock()
        self.audio_slots = threading.BoundedSemaphore(2)

    def rate(self, bucket, identity, maximum, seconds=60):
        # Never trust X-Forwarded-For supplied by an arbitrary caller.
        now = time.monotonic()
        with self.lock:
            if len(self.limits) > 10000:
                self.limits = {k: v for k, v in self.limits.items() if v and v[-1] > now - 3600}
            events = self.limits.setdefault((bucket, identity), collections.deque())
            while events and events[0] < now - seconds:
                events.popleft()
            if len(events) >= maximum:
                return False
            events.append(now)
            return True

    def rpc(self, request, headers):
        if not isinstance(request, dict) or request.get('jsonrpc') != '2.0' or not isinstance(request.get('method'), str):
            raise RPCError(-32600, 'Expected one JSON-RPC 2.0 request')
        method, params = request['method'], request.get('params', {})
        if 'id' in request and (isinstance(request['id'], bool) or not isinstance(request['id'], (int, str))):
            raise RPCError(-32600, 'Request id must be a string or integer')
        if not isinstance(params, dict):
            raise RPCError(-32602, 'params must be an object')
        meta = params.get('_meta', {})
        if not isinstance(meta, dict):
            raise RPCError(-32602, '_meta must be an object')
        legacy = method == 'initialize' or (headers.get('MCP-Protocol-Version') == LEGACY
                    and meta.get(PREFIX + 'protocolVersion') in (None, LEGACY))
        if legacy:
            if method == 'initialize':
                # initialize belongs to the legacy era. Modern clients use server/discover.
                version = LEGACY
                result = {'protocolVersion': version, 'capabilities': {'tools': {'listChanged': False}},
                          'serverInfo': INFO, 'instructions': 'See ' + self.origin + '/plan'}
                return result, version
            version = LEGACY
        else:
            version = meta.get(PREFIX + 'protocolVersion')
            if not version or headers.get('MCP-Protocol-Version') != version:
                raise RPCError(-32020, 'MCP-Protocol-Version must match request metadata')
            if version not in VERSIONS:
                raise RPCError(-32022, 'Unsupported protocol version', data={'requested': version, 'supported': VERSIONS})
            if headers.get('Mcp-Method') != method:
                raise RPCError(-32020, 'Mcp-Method must match the request method')
            if method == 'tools/call':
                name = headers.get('Mcp-Name', '')
                if name.startswith('=?base64?') and name.endswith('?='):
                    try:
                        name = base64.b64decode(name[9:-2], validate=True).decode()
                    except (ValueError, UnicodeError):
                        raise RPCError(-32020, 'Invalid encoded Mcp-Name') from None
                if name != params.get('name'):
                    raise RPCError(-32020, 'Mcp-Name must match the tool name')
            if not isinstance(meta.get(PREFIX + 'clientCapabilities'), dict):
                raise RPCError(-32602, 'Each modern request must declare clientCapabilities')
        if 'id' not in request:
            if method not in ('notifications/initialized', 'notifications/cancelled'):
                raise RPCError(-32600, 'This operation requires a request id')
            return None, version
        if method == 'server/discover':
            result = {'supportedVersions': VERSIONS, 'capabilities': {'tools': {}},
                      'instructions': 'Four tools in one owner workspace. See ' + self.origin + '/plan',
                      'ttlMs': 3600000, 'cacheScope': 'private'}
        elif method == 'tools/list':
            result = {'tools': DEFINITIONS, 'ttlMs': 3600000, 'cacheScope': 'private'}
        elif method == 'tools/call':
            name = params.get('name')
            if not isinstance(name, str) or name not in BY_NAME:
                raise RPCError(-32602, 'Unknown tool name')
            arguments = params.get('arguments', {})
            try:
                validate(arguments, BY_NAME[name]['inputSchema'], 'arguments')
            except ValueError as e:
                raise RPCError(-32602, str(e)) from None
            acquired = name != 'transcribe' or self.audio_slots.acquire(blocking=False)
            if not acquired:
                result = {'isError': True, 'content': [{'type': 'text', 'text': 'Transcription is busy; try again later'}]}
            else:
                try:
                    structured = self.tools.call(name, arguments)
                    result = {'content': [{'type': 'text', 'text': json_bytes(structured).decode()}],
                              'structuredContent': structured, 'isError': False}
                except (ValueError, OSError) as e:
                    # No exception bodies, upstream replies, credentials, or URLs are logged.
                    message = str(e) if isinstance(e, ValueError) else 'The tool could not complete its network or storage operation'
                    result = {'isError': True, 'content': [{'type': 'text', 'text': message}]}
                finally:
                    if name == 'transcribe':
                        self.audio_slots.release()
        elif method == 'ping' and version == LEGACY:
            result = {}
        else:
            raise RPCError(-32601, 'Method not found', status=404 if version == CURRENT else 200)
        if version == CURRENT:
            result.update(resultType='complete', _meta={PREFIX + 'serverInfo': INFO})
        else:
            result.pop('ttlMs', None)
            result.pop('cacheScope', None)
        return result, version


class Handler(BaseHTTPRequestHandler):
    server_version = 'SchemaFirst/1.0'
    protocol_version = 'HTTP/1.1'

    @property
    def app(self):
        return self.server.app

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, fmt, *args):
        # Request targets contain OAuth codes and state: do not log them.
        pass

    def send(self, status, body=None, content_type='application/json', headers=None):
        headers = dict(headers or {})
        if isinstance(body, (dict, list)):
            body = json_bytes(body)
        elif isinstance(body, str):
            body = body.encode()
        body = body or b''
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        # no-referrer turns a browser form POST's Origin into "null". Preserve
        # same-origin consent posts while still hiding cross-origin referrers.
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('Content-Security-Policy', headers.pop('Content-Security-Policy', CSP))
        if self.app.origin.startswith('https:'):
            self.send_header('Strict-Transport-Security', 'max-age=31536000')
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def guard(self):
        origin = self.headers.get('Origin')
        if origin and origin not in self.app.allowed_origins:
            self.send(403, {'error': 'origin_not_allowed'})
            return False
        if not self.app.rate('all', self.client_address[0], 300):
            self.send(429, {'error': 'rate_limited'}, headers={'Retry-After': '60'})
            return False
        return True

    def body(self, maximum=MAX_BODY):
        if self.headers.get('Transfer-Encoding'):
            raise ValueError('Chunked request bodies are not supported')
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or not lengths[0].isdigit():
            raise ValueError('One Content-Length is required')
        length = int(lengths[0])
        if length > maximum:
            raise ValueError('Request body exceeds size limit')
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError('Incomplete request body')
        return raw

    def form(self, raw):
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/x-www-form-urlencoded':
            raise ValueError('Expected form URL encoding')
        values = urllib.parse.parse_qs(raw.decode(), keep_blank_values=True, max_num_fields=30)
        return parameters(values)

    def authenticated(self):
        auth = self.headers.get('Authorization', '')
        if not auth.startswith('Bearer ') or not self.app.auth.authorized(auth[7:]):
            self.send(401, {'error': 'unauthorized'}, headers={'WWW-Authenticate':
                'Bearer resource_metadata="' + self.app.origin + '/.well-known/oauth-protected-resource/mcp", scope="tools"'})
            return False
        return True

    def do_GET(self):
        if not self.guard(): return
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        try:
            if path in ('/', '/plan'):
                self.send(200, (ROOT / 'plan.html').read_bytes(), 'text/html; charset=utf-8')
            elif path == '/health':
                self.send(200, {'ok': True, 'protocols': VERSIONS})
            elif path in ('/.well-known/oauth-protected-resource', '/.well-known/oauth-protected-resource/mcp'):
                self.send(200, self.app.auth.protected_metadata())
            elif path in ('/.well-known/oauth-authorization-server', '/.well-known/openid-configuration'):
                self.send(200, self.app.auth.metadata())
            elif path == '/authorize':
                values = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, max_num_fields=30)
                pending, browser, record = self.app.auth.begin(parameters(values))
                markup = (ROOT / 'consent.html').read_text()
                for key, value in {'PENDING': pending, 'CLIENT': record['client_name'],
                                   'CLIENT_ID': record['client_id'], 'REDIRECT': record['redirect_uri']}.items():
                    markup = markup.replace('{{' + key + '}}', html.escape(value, quote=True))
                cookie = 'consent=' + browser + '; HttpOnly; SameSite=Lax; Path=/authorize; Max-Age=600'
                if self.app.origin.startswith('https:'): cookie += '; Secure'
                # Browsers also check form-action on the post-consent redirect.
                # Permit only this validated client's callback origin. Quote any
                # special characters so metadata can never inject CSP directives.
                target = urllib.parse.urlsplit(record['redirect_uri'])
                hostname = target.hostname.encode('idna').decode('ascii')
                authority = '[' + hostname + ']' if ':' in hostname else hostname
                if target.port:
                    authority += ':' + str(target.port)
                callback_origin = target.scheme + '://' + urllib.parse.quote(authority, safe='[]:.-')
                consent_policy = CSP.replace("form-action 'self'", "form-action 'self' " + callback_origin)
                self.send(200, markup, 'text/html; charset=utf-8', {
                    'Set-Cookie': cookie, 'Content-Security-Policy': consent_policy})
            elif path.startswith('/files/'):
                file_id = path.removeprefix('/files/')
                file = self.app.store.file(file_id) if re.fullmatch(r'[A-Za-z0-9_-]{43}', file_id) else None
                if not file:
                    self.send(404, {'error': 'file_not_found'})
                else:
                    self.send(200, file['body'], file['mime'], {'Content-Disposition':
                        "attachment; filename*=UTF-8''" + urllib.parse.quote(file['filename'], safe='')})
            elif path == '/mcp':
                if self.authenticated():
                    self.send(405, {'error': 'use_post'}, headers={'Allow': 'POST'})
            else:
                self.send(404, {'error': 'not_found'})
        except OAuthError as e:
            self.send(e.status, {'error': e.error, 'error_description': e.description})
        except (ValueError, OSError):
            self.send(400, {'error': 'invalid_request'})

    def do_HEAD(self):
        self.do_GET()

    def do_DELETE(self):
        if not self.guard(): return
        if urllib.parse.urlsplit(self.path).path == '/mcp':
            if self.authenticated(): self.send(405, headers={'Allow': 'POST'})
        else:
            self.send(404, {'error': 'not_found'})

    def do_POST(self):
        if not self.guard():
            self.close_connection = True
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == '/mcp' and not self.authenticated():
            self.close_connection = True
            return
        request = None
        try:
            raw = self.body(MAX_BODY if path == '/mcp' else 65536)
            self.app.store.prune()
            if path == '/register':
                if not self.app.rate('register', self.client_address[0], 30):
                    return self.send(429, {'error': 'rate_limited'})
                self.send(201, self.app.auth.register(json.loads(raw)))
            elif path == '/authorize':
                if not self.app.rate('consent', 'owner', 10):
                    return self.send(429, {'error': 'rate_limited'})
                params = self.form(raw)
                cookies = http.cookies.SimpleCookie()
                cookies.load(self.headers.get('Cookie', ''))
                browser = cookies['consent'].value if 'consent' in cookies else ''
                redirect = self.app.auth.consent(params.get('pending', ''), browser,
                    params.get('password', ''), params.get('decision') == 'allow')
                self.send(303, headers={'Location': redirect,
                    'Set-Cookie': 'consent=; Path=/authorize; HttpOnly; SameSite=Lax; Max-Age=0'})
            elif path in ('/token', '/revoke'):
                params = self.form(raw)
                header = self.headers.get('Authorization', '')
                basic = header[6:] if header.startswith('Basic ') else None
                if path == '/token':
                    self.send(200, self.app.auth.token(params, basic))
                else:
                    self.app.auth.revoke(params, basic)
                    self.send(200)
            elif path == '/mcp':
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    return self.send(415, {'error': 'use_application_json'})
                request = json.loads(raw, parse_constant=lambda x: (_ for _ in ()).throw(ValueError('Non-finite JSON')))
                result, version = self.app.rpc(request, self.headers)
                if result is None:
                    self.send(202)
                else:
                    self.send(200, {'jsonrpc': '2.0', 'id': request['id'], 'result': result},
                              headers={'MCP-Protocol-Version': version})
            else:
                self.send(404, {'error': 'not_found'})
        except OAuthError as e:
            self.send(e.status, {'error': e.error, 'error_description': e.description})
        except RPCError as e:
            error = {'code': e.code, 'message': e.message}
            if e.data is not None: error['data'] = e.data
            envelope = {'jsonrpc': '2.0', 'error': error}
            if isinstance(request, dict) and isinstance(request.get('id'), (str, int)):
                envelope['id'] = request['id']
            self.send(e.status, envelope)
        except json.JSONDecodeError:
            self.send(400, {'jsonrpc': '2.0', 'error': {'code': -32700, 'message': 'Invalid JSON'}})
        except (ValueError, UnicodeError, OSError):
            self.close_connection = True
            self.send(400, {'error': 'invalid_request'})
        except Exception:
            # Avoid secret-bearing tracebacks on the public server.
            self.close_connection = True
            self.send(500, {'error': 'internal_error'})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default=None)
    parser.add_argument('--port', type=int, default=None)
    args = parser.parse_args()
    config = read_config()
    app = Application(config)
    host = args.host or config.get('BIND_HOST', '127.0.0.1')
    port = args.port or int(config.get('PORT', '8765'))
    server = Server((host, port), Handler)
    server.app = app
    def stop(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print('Schema First listening on ' + host + ':' + str(port), flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
