"""Exercise real HTTP, OAuth, storage, and schemas without a paid API or hosting."""
import base64
import concurrent.futures
import http.client
import io
import json
from pathlib import Path
import socket
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
import wave
from unittest.mock import patch

import jsonschema

from auth import OAuthError, challenge
from network import download
from server import Application, CURRENT, LEGACY, PREFIX, Handler, Server
from storage import Store, digest
from tools import BY_NAME, DEFINITIONS

PASSWORD = 'local-test-owner-password-only'
VERIFIER = 'a' * 64


def wav():
    out = io.BytesIO()
    with wave.open(out, 'wb') as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000)
        f.writeframes(b'\x00\x00' * 1600)
    return out.getvalue()


class Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.server = Server(('127.0.0.1', 0), Handler)
        cls.origin = 'http://127.0.0.1:' + str(cls.server.server_port)
        cls.app = Application({'PUBLIC_URL': cls.origin, 'OWNER_PASSWORD': PASSWORD,
                               'DATA_DIR': cls.temp.name},
                              transcriber=lambda body, mime, cfg: {'segments': [{'start': 0.25, 'text': 'Test speech.'}]})
        cls.server.app = cls.app
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=5)
        cls.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            raw = response.read()
            return response.status, dict(response.getheaders()), raw
        finally:
            conn.close()

    def form(self, path, params, headers=None):
        return self.request('POST', path, urllib.parse.urlencode(params),
                            {'Content-Type': 'application/x-www-form-urlencoded', **(headers or {})})

    def register(self, redirect_uri=None):
        status, _, body = self.request('POST', '/register', json.dumps({
            'client_name': 'Test Client', 'redirect_uris': [redirect_uri or 'http://localhost:9999/callback'],
            'token_endpoint_auth_method': 'none', 'grant_types': ['authorization_code', 'refresh_token'],
            'application_type': 'native'}), {'Content-Type': 'application/json'})
        self.assertEqual(status, 201)
        return json.loads(body)

    def authorization(self, redirect_uri=None):
        client = self.register(redirect_uri)
        params = {'client_id': client['client_id'], 'redirect_uri': client['redirect_uris'][0],
                  'response_type': 'code', 'scope': 'tools', 'state': 'test-state',
                  'resource': self.origin + '/mcp', 'code_challenge': challenge(VERIFIER),
                  'code_challenge_method': 'S256'}
        return client, params

    def test_10_browser_consent_preserves_origin(self):
        candidates = list((Path.home() / '.cache/ms-playwright').glob(
            'chromium_headless_shell-*/*/chrome-headless-shell'))
        command, container = None, None
        if candidates and shutil.which('node'):
            probe = subprocess.run([str(candidates[-1]), '--version'], capture_output=True, timeout=5)
            if probe.returncode == 0:
                chromium = str(candidates[-1])
                command = ['node', 'tests/browser-consent.mjs']
        # An already-cached browser image is an alternative; never pull/install one.
        if command is None and shutil.which('docker'):
            image = 'cypress/included:9.7.0'
            probe = subprocess.run(['docker', 'image', 'inspect', image], capture_output=True, timeout=5)
            if probe.returncode == 0:
                chromium = 'google-chrome'
                container = 'schema-first-browser-test-' + str(self.server.server_port)
                helper = Path(__file__).parent / 'tests/browser-consent.mjs'
                command = ['docker', 'run', '--rm', '--pull=never', '-i', '--network', 'host',
                           '--name', container, '--entrypoint', 'node', '--workdir', '/tmp',
                           '--volume', str(helper.resolve()) + ':/test/browser-consent.mjs:ro',
                           image, '/test/browser-consent.mjs']
        if command is None:
            self.skipTest('Requires a usable installed Chromium or the already-cached browser image')
        # A real OAuth callback is on another origin. localhost and 127.0.0.1
        # reach this fixture server while still being distinct browser origins.
        _, params = self.authorization(self.origin.replace('127.0.0.1', 'localhost') + '/health')
        fixture = {'chromium': chromium, 'password': PASSWORD,
                   'url': self.origin + '/authorize?' + urllib.parse.urlencode(params)}
        (Path(__file__).parent / 'private').mkdir(exist_ok=True)
        try:
            result = subprocess.run(command, input=json.dumps(fixture),
                                    text=True, capture_output=True, timeout=30, cwd=Path(__file__).parent)
        finally:
            if container:
                subprocess.run(['docker', 'rm', '-f', container], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = json.loads(result.stdout)
        self.assertEqual(evidence['postedOrigin'], self.origin, evidence)
        self.assertTrue(evidence['body'].get('ok'), evidence)
        self.assertFalse(evidence['callbackReferrerSent'], evidence)

    def test_11_transcription_errors_name_the_url_and_reason(self):
        _, tokens, _ = self.issue()
        for url, downloaded, failure, expected in (
                ('https://course.example/login', (b'<!doctype html><title>Sign in</title>', 'text/html'),
                 None, 'Unsupported audio file'),
                ('https://course.example/missing.mp3', None,
                 ValueError('Download returned HTTP 404'), 'HTTP 404')):
            with self.subTest(url=url), patch('tools.download', return_value=downloaded, side_effect=failure):
                status, body = self.rpc(tokens['access_token'], 'tools/call',
                    {'name': 'transcribe', 'arguments': {'url': url}})
                self.assertEqual(status, 200)
                self.assertTrue(body['result']['isError'])
                message = body['result']['content'][0]['text']
                self.assertIn(url, message)
                self.assertIn(expected, message)

    def issue(self, repeat_resource=False):
        client, params = self.authorization()
        query = urllib.parse.urlencode(params)
        if repeat_resource:
            query += '&' + urllib.parse.urlencode({'resource': params['resource']})
        status, headers, body = self.request('GET', '/authorize?' + query)
        self.assertEqual(status, 200)
        import re
        pending = re.search(b'name="pending" value="([^"]+)"', body).group(1).decode()
        cookie = headers['Set-Cookie'].split(';')[0]
        status, headers, _ = self.form('/authorize', {'pending': pending, 'password': PASSWORD, 'decision': 'allow'},
                                      {'Cookie': cookie, 'Origin': self.origin})
        self.assertEqual(status, 303)
        answer = urllib.parse.parse_qs(urllib.parse.urlsplit(headers['Location']).query)
        self.assertEqual(answer['state'], ['test-state'])
        self.assertEqual(answer['iss'], [self.origin])
        code = answer['code'][0]
        token_params = {'grant_type': 'authorization_code', 'client_id': client['client_id'], 'code': code,
                        'code_verifier': VERIFIER, 'resource': self.origin + '/mcp',
                        'redirect_uri': client['redirect_uris'][0]}
        body_params = list(token_params.items())
        if repeat_resource:
            body_params.append(('resource', token_params['resource']))
        status, _, body = self.form('/token', body_params)
        self.assertEqual(status, 200, body)
        return client, json.loads(body), token_params

    def test_08_codex_repeated_identical_resource_roundtrip(self):
        _, tokens, _ = self.issue(repeat_resource=True)
        status, body = self.rpc(tokens['access_token'], 'tools/list')
        self.assertEqual(status, 200)
        self.assertEqual(len(body['result']['tools']), 4)

    def test_09_conflicting_resources_and_duplicate_security_fields(self):
        _, params = self.authorization()
        base = list(params.items())
        for field, value, expected in (
                ('resource', 'https://evil.example/mcp', 'invalid_target'),
                ('state', params['state'], 'invalid_request'),
                ('client_id', params['client_id'], 'invalid_request'),
                ('redirect_uri', 'https://evil.example/callback', 'invalid_request'),
                ('code_challenge', params['code_challenge'], 'invalid_request')):
            with self.subTest(field=field):
                status, _, body = self.request('GET', '/authorize?' + urllib.parse.urlencode(base + [(field, value)]))
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)['error'], expected)
        for field, values, expected in (
                ('resource', [params['resource'], 'https://evil.example/mcp'], 'invalid_target'),
                ('code_verifier', [VERIFIER, 'b' * 64], 'invalid_request')):
            status, _, body = self.form('/token', [(field, value) for value in values])
            self.assertEqual(status, 400)
            self.assertEqual(json.loads(body)['error'], expected)

    def rpc(self, token, method, params=None, version=CURRENT, extra_headers=None):
        params = dict(params or {})
        if version == CURRENT:
            params['_meta'] = {PREFIX + 'protocolVersion': CURRENT, PREFIX + 'clientCapabilities': {},
                               PREFIX + 'clientInfo': {'name': 'integration', 'version': '1'}}
        request = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                   'Authorization': 'Bearer ' + token, 'MCP-Protocol-Version': version, 'Mcp-Method': method}
        if method == 'tools/call': headers['Mcp-Name'] = params.get('name', '')
        headers.update(extra_headers or {})
        status, response_headers, body = self.request('POST', '/mcp', json.dumps(request), headers)
        return status, json.loads(body) if body else None

    def official(self, definition, value):
        path = Path(__file__).parent / 'private/spec/schema.json'
        if path.exists():
            schema = json.loads(path.read_text())
            jsonschema.Draft202012Validator({**schema, '$ref': '#/$defs/' + definition}).validate(value)

    def test_01_unauthenticated_and_metadata(self):
        status, headers, _ = self.request('POST', '/mcp', b'{}')
        self.assertEqual(status, 401)
        self.assertIn('resource_metadata=', headers['WWW-Authenticate'])
        for path in ('/.well-known/oauth-protected-resource', '/.well-known/oauth-protected-resource/mcp'):
            status, _, body = self.request('GET', path)
            self.assertEqual(status, 200)
            metadata = json.loads(body)
            self.assertEqual(metadata['resource'], self.origin + '/mcp')
            self.assertEqual(metadata['resource_documentation'], self.origin + '/plan')
        status, _, body = self.request('GET', '/plan')
        self.assertEqual(status, 200)
        for word in (b'memo_write', b'memo_search', b'upload', b'transcribe', b'Gemini', b'OAuth', b'SQLite'):
            self.assertIn(word, body)

    def test_02_oauth_roundtrip_replay_resource_refresh_revoke(self):
        client, tokens, code_params = self.issue()
        self.assertTrue(self.app.auth.authorized(tokens['access_token']))
        status, _, _ = self.form('/token', code_params)
        self.assertEqual(status, 400)
        refresh = {'grant_type': 'refresh_token', 'refresh_token': tokens['refresh_token'],
                   'client_id': client['client_id'], 'resource': self.origin + '/mcp'}
        status, _, body = self.form('/token', {**refresh, 'resource': 'https://another.example/mcp'})
        self.assertEqual(status, 400)
        status, _, body = self.form('/token', refresh)
        self.assertEqual(status, 200)
        new = json.loads(body)
        self.assertNotEqual(new['refresh_token'], tokens['refresh_token'])
        status, _, _ = self.form('/token', refresh)
        self.assertEqual(status, 400)
        status, _, _ = self.form('/revoke', {'client_id': client['client_id'], 'token': new['access_token']})
        self.assertEqual(status, 200)
        self.assertFalse(self.app.auth.authorized(new['access_token']))
        with self.app.store.connect() as db:
            dump = '\n'.join(db.iterdump())
        self.assertNotIn(new['access_token'], dump)
        self.assertNotIn(new['refresh_token'], dump)

    def test_03_pkce_redirect_consent_csrf(self):
        client, params = self.authorization()
        with self.assertRaises(OAuthError): self.app.auth.begin({**params, 'redirect_uri': 'https://evil.example/callback'})
        with self.assertRaises(OAuthError): self.app.auth.begin({**params, 'code_challenge_method': 'plain'})
        pending, browser, _ = self.app.auth.begin(params)
        with self.assertRaises(OAuthError): self.app.auth.consent(pending, 'wrong-browser', PASSWORD, True)
        with self.assertRaises(OAuthError): self.app.auth.consent(pending, browser, 'wrong-password', True)
        redirect = self.app.auth.consent(pending, browser, PASSWORD, True)
        code = urllib.parse.parse_qs(urllib.parse.urlsplit(redirect).query)['code'][0]
        exchange = {'grant_type': 'authorization_code', 'client_id': client['client_id'], 'resource': self.origin + '/mcp',
                    'redirect_uri': client['redirect_uris'][0], 'code': code, 'code_verifier': 'b' * 64}
        with self.assertRaises(OAuthError): self.app.auth.token(exchange)
        good = self.app.auth.token({**exchange, 'code_verifier': VERIFIER})
        self.assertTrue(self.app.auth.authorized(good['access_token']))

    def test_04_modern_discovery_headers_and_unknown_version(self):
        _, tokens, _ = self.issue()
        token = tokens['access_token']
        status, body = self.rpc(token, 'server/discover')
        self.assertEqual(status, 200)
        self.assertEqual(body['result']['supportedVersions'], [CURRENT, LEGACY])
        self.assertEqual(body['result']['resultType'], 'complete')
        self.assertIn(PREFIX + 'serverInfo', body['result']['_meta'])
        self.official('DiscoverResult', body['result'])
        status, body = self.rpc(token, 'tools/list', extra_headers={'Mcp-Method': 'different'})
        self.assertEqual((status, body['error']['code']), (400, -32020))
        status, body = self.rpc(token, 'tools/list', extra_headers={'MCP-Protocol-Version': '1900-01-01'})
        self.assertEqual((status, body['error']['code']), (400, -32020))
        status, body = self.rpc(token, 'tools/list', extra_headers={'MCP-Protocol-Version': LEGACY})
        self.assertEqual((status, body['error']['code']), (400, -32020))
        unknown = {'jsonrpc': '2.0', 'id': 9, 'method': 'server/discover', 'params': {'_meta': {
            PREFIX + 'protocolVersion': '1900-01-01', PREFIX + 'clientCapabilities': {}}}}
        status, _, raw = self.request('POST', '/mcp', json.dumps(unknown), {'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + token, 'MCP-Protocol-Version': '1900-01-01', 'Mcp-Method': 'server/discover'})
        self.assertEqual((status, json.loads(raw)['error']['code']), (400, -32022))
        status, body = self.rpc(token, 'does/not-exist')
        self.assertEqual((status, body['error']['code']), (404, -32601))
        status, _, _ = self.request('GET', '/mcp', headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(status, 405)

    def test_05_four_tools_structured_results_and_storage(self):
        _, tokens, _ = self.issue()
        token = tokens['access_token']
        status, body = self.rpc(token, 'tools/list')
        self.assertEqual(status, 200)
        self.official('ListToolsResult', body['result'])
        definitions = body['result']['tools']
        self.assertEqual({t['name'] for t in definitions}, {'memo_write', 'memo_search', 'upload', 'transcribe'})
        for definition in definitions:
            jsonschema.Draft202012Validator.check_schema(definition['inputSchema'])
            jsonschema.Draft202012Validator.check_schema(definition['outputSchema'])
        def call(name, args):
            status, body = self.rpc(token, 'tools/call', {'name': name, 'arguments': args})
            self.assertEqual(status, 200, body)
            result = body['result']
            self.assertFalse(result['isError'], result)
            self.official('CallToolResult', result)
            jsonschema.validate(result['structuredContent'], BY_NAME[name]['outputSchema'])
            self.assertEqual(json.loads(result['content'][0]['text']), result['structuredContent'])
            return result['structuredContent']
        text = 'Exact note: Zażółć gęślą jaźń 🧪\nsecond line %_'
        note = call('memo_write', {'text': text})
        search = call('memo_search', {'query': 'GĘŚLĄ'})
        self.assertEqual(search['notes'][0]['id'], note['id'])
        self.assertEqual(search['notes'][0]['text'], text)
        self.assertEqual(Store(self.temp.name).search_notes('%_')['notes'][0]['text'], text)
        audio = wav()
        result = call('upload', {'filename': 'sample.wav', 'content_base64': base64.b64encode(audio).decode()})
        status, headers, raw = self.request('GET', urllib.parse.urlsplit(result['url']).path)
        self.assertEqual(status, 200)
        self.assertEqual(raw, audio)
        self.assertEqual(headers['Content-Type'], 'audio/x-wav')
        self.assertIn('attachment', headers['Content-Disposition'])
        transcript = call('transcribe', {'url': result['url']})
        self.assertEqual(transcript['segments'][0]['start'], 0.25)

    def test_06_input_errors_tool_errors_and_origin(self):
        _, tokens, _ = self.issue()
        token = tokens['access_token']
        for args in ({}, {'text': 12}, {'text': 'hi', 'extra': True}):
            status, body = self.rpc(token, 'tools/call', {'name': 'memo_write', 'arguments': args})
            self.assertEqual((status, body['error']['code']), (400, -32602))
        status, body = self.rpc(token, 'tools/call', {'name': 'upload', 'arguments': {
            'filename': '../unsafe.txt', 'content_base64': 'aGk='}})
        self.assertTrue(body['result']['isError'])
        status, body = self.rpc(token, 'tools/call', {'name': 'transcribe', 'arguments': {'url': 'http://127.0.0.1/audio.wav'}})
        self.assertTrue(body['result']['isError'])
        for origin in ('https://evil.example', 'null'):
            status, _, _ = self.request('POST', '/mcp', b'{}', {'Origin': origin, 'Authorization': 'Bearer ' + token})
            self.assertEqual(status, 403)

    def test_07_legacy_compatibility(self):
        _, tokens, _ = self.issue()
        token = tokens['access_token']
        status, body = self.rpc(token, 'initialize', {'protocolVersion': LEGACY, 'capabilities': {},
            'clientInfo': {'name': 'legacy-test', 'version': '1'}}, version=LEGACY)
        self.assertEqual(status, 200)
        self.assertEqual(body['result']['protocolVersion'], LEGACY)
        status, body = self.rpc(token, 'tools/list', version=LEGACY)
        self.assertEqual(status, 200)
        self.assertNotIn('resultType', body['result'])
        self.assertEqual(len(body['result']['tools']), 4)
        status, _, raw = self.request('POST', '/mcp', json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}),
            {'Authorization': 'Bearer ' + token, 'MCP-Protocol-Version': LEGACY, 'Content-Type': 'application/json'})
        self.assertEqual((status, raw), (202, b''))


class Security(unittest.TestCase):
    def test_private_destinations_and_redirect(self):
        for url in ('http://127.0.0.1/test', 'http://169.254.169.254/latest', 'http://[::1]/test',
                    'file:///etc/passwd', 'http://user:pass@example.com/test', 'https://example.com:444/test'):
            with self.subTest(url=url), self.assertRaises(ValueError): download(url)
        with patch('socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.1', 443))]):
            with self.assertRaises(ValueError): download('https://public-looking.example/audio.wav')

    def test_code_single_use_under_concurrency(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            store.put('code', 'test', {'ok': True}, 60)
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: store.get('code', 'test', consume=True), range(8)))
            self.assertEqual(sum(r is not None for r in results), 1)

    def test_invalid_provider_output_is_rejected(self):
        from tools import Tools
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            tools = Tools(store, 'https://tools.example', {}, transcriber=lambda *_: {'segments': [{'start': -1, 'text': 'bad'}]})
            file_id = store.save_file('audio.wav', 'audio/wav', wav())
            with self.assertRaises(ValueError): tools.call('transcribe', {'url': 'https://tools.example/files/' + file_id})


if __name__ == '__main__':
    unittest.main()
