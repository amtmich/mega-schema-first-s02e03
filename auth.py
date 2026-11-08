"""OAuth 2.1: owner consent, S256 PKCE, resource-bound opaque tokens, rotation."""
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.parse

from network import download
from storage import digest

SCOPE = 'tools'


class OAuthError(ValueError):
    def __init__(self, error, description, status=400):
        self.error, self.description, self.status = error, description, status
        super().__init__(description)


def challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()


def valid_redirect(uri):
    if not isinstance(uri, str) or len(uri) > 4096 or any(ord(c) < 32 for c in uri):
        return False
    try:
        p = urllib.parse.urlsplit(uri)
        _ = p.port
        return bool(p.hostname and not p.username and not p.password and not p.fragment and
                    (p.scheme == 'https' or (p.scheme == 'http' and p.hostname in ('localhost', '127.0.0.1', '::1'))))
    except ValueError:
        return False


class Auth:
    def __init__(self, store, origin, owner_password):
        self.store, self.origin, self.resource = store, origin, origin + '/mcp'
        if len(owner_password) < 16:
            raise ValueError('OWNER_PASSWORD must contain at least 16 characters')
        self.owner_hash = digest(owner_password)

    def metadata(self):
        return {'issuer': self.origin, 'authorization_endpoint': self.origin + '/authorize',
                'token_endpoint': self.origin + '/token', 'registration_endpoint': self.origin + '/register',
                'revocation_endpoint': self.origin + '/revoke',
                'response_types_supported': ['code'],
                'grant_types_supported': ['authorization_code', 'refresh_token'],
                'token_endpoint_auth_methods_supported': ['none', 'client_secret_post', 'client_secret_basic'],
                'code_challenge_methods_supported': ['S256'], 'scopes_supported': [SCOPE],
                'client_id_metadata_document_supported': True,
                'authorization_response_iss_parameter_supported': True,
                'service_documentation': self.origin + '/plan'}

    def protected_metadata(self):
        return {'resource': self.resource, 'authorization_servers': [self.origin],
                'bearer_methods_supported': ['header'], 'scopes_supported': [SCOPE],
                'resource_name': 'Schema First tools', 'resource_documentation': self.origin + '/plan'}

    def register(self, metadata):
        if not isinstance(metadata, dict):
            raise OAuthError('invalid_client_metadata', 'Client metadata must be an object')
        uris = metadata.get('redirect_uris')
        if not isinstance(uris, list) or not 1 <= len(uris) <= 20 or not all(valid_redirect(u) for u in uris):
            raise OAuthError('invalid_redirect_uri', 'Register exact HTTPS or localhost redirect URIs')
        method = metadata.get('token_endpoint_auth_method', 'client_secret_basic')
        if method not in ('none', 'client_secret_post', 'client_secret_basic'):
            raise OAuthError('invalid_client_metadata', 'Unsupported token authentication method')
        grants = metadata.get('grant_types', ['authorization_code'])
        if not isinstance(grants, list) or not set(grants) <= {'authorization_code', 'refresh_token'}:
            raise OAuthError('invalid_client_metadata', 'Unsupported grant type')
        if metadata.get('response_types', ['code']) != ['code']:
            raise OAuthError('invalid_client_metadata', 'Only code responses are supported')
        name = metadata.get('client_name', 'Unnamed MCP client')
        if not isinstance(name, str) or len(name) > 200:
            raise OAuthError('invalid_client_metadata', 'Invalid client name')
        client_id = secrets.token_urlsafe(24)
        result = {'client_id': client_id, 'client_id_issued_at': int(time.time()),
                  'redirect_uris': uris, 'client_name': name, 'grant_types': grants,
                  'response_types': ['code'], 'token_endpoint_auth_method': method}
        saved = dict(result)
        if method != 'none':
            result['client_secret'] = secrets.token_urlsafe(32)
            result['client_secret_expires_at'] = 0
            saved['secret_hash'] = digest(result['client_secret'])
        self.store.put('client', client_id, saved, 365 * 86400)
        return result

    def client(self, client_id):
        if not isinstance(client_id, str) or len(client_id) > 4096:
            raise OAuthError('invalid_client', 'Invalid client ID')
        result = self.store.get('client', client_id)
        if result:
            return result
        p = urllib.parse.urlsplit(client_id)
        if p.scheme != 'https' or not p.path or p.path == '/' or p.fragment:
            raise OAuthError('invalid_client', 'Unknown client ID')
        try:
            body, mime = download(client_id, maximum=65536, redirects=0, deadline_seconds=15)
            result = json.loads(body)
            if not isinstance(result, dict) or result.get('client_id') != client_id:
                raise ValueError('Metadata client_id does not match its URL')
            if not isinstance(result.get('client_name'), str) or not 1 <= len(result['client_name']) <= 200:
                raise ValueError('Client metadata needs a name')
            uris = result.get('redirect_uris')
            if not isinstance(uris, list) or not 1 <= len(uris) <= 20 or not all(valid_redirect(u) for u in uris):
                raise ValueError('Client metadata needs valid redirect URIs')
            if result.get('token_endpoint_auth_method', 'none') != 'none':
                raise ValueError('URL clients must use PKCE without a shared secret')
            result['token_endpoint_auth_method'] = 'none'
        except (ValueError, OSError, TypeError):
            raise OAuthError('invalid_client', 'Could not validate public client metadata') from None
        self.store.put('client', client_id, result, 300)
        return result

    def begin(self, params):
        client = self.client(params.get('client_id', ''))
        redirect = params.get('redirect_uri', '')
        if redirect not in client['redirect_uris']:
            raise OAuthError('invalid_request', 'Redirect URI does not match client registration')
        if params.get('response_type') != 'code':
            raise OAuthError('unsupported_response_type', 'Use the authorization code flow')
        if params.get('resource') != self.resource:
            raise OAuthError('invalid_target', 'Resource must exactly match the MCP endpoint')
        if params.get('code_challenge_method') != 'S256' or not re.fullmatch(r'[A-Za-z0-9_-]{43}', params.get('code_challenge', '')):
            raise OAuthError('invalid_request', 'S256 PKCE is required')
        scope = params.get('scope', SCOPE)
        if scope.split() != [SCOPE]:
            raise OAuthError('invalid_scope', 'Only tools scope is supported')
        pending = secrets.token_urlsafe(32)
        browser = secrets.token_urlsafe(32)
        record = {key: params.get(key, '') for key in
                  ('client_id', 'redirect_uri', 'state', 'resource', 'code_challenge')}
        record.update(scope=SCOPE, browser_hash=digest(browser), client_name=client['client_name'])
        self.store.put('pending', digest(pending), record, 600)
        return pending, browser, record

    def consent(self, pending, browser, password, allow):
        record = self.store.get('pending', digest(pending))
        if not record or not hmac.compare_digest(record['browser_hash'], digest(browser)):
            raise OAuthError('invalid_request', 'Consent expired or browser does not match')
        if not hmac.compare_digest(digest(password), self.owner_hash):
            raise OAuthError('access_denied', 'Incorrect owner password', 403)
        record = self.store.get('pending', digest(pending), consume=True)
        if not record:
            raise OAuthError('invalid_request', 'Consent was already used')
        response = {'iss': self.origin}
        if record['state']:
            response['state'] = record['state']
        if allow:
            code = secrets.token_urlsafe(32)
            self.store.put('code', digest(code), record, 120)
            response['code'] = code
        else:
            response['error'] = 'access_denied'
        return record['redirect_uri'] + ('&' if '?' in record['redirect_uri'] else '?') + urllib.parse.urlencode(response)

    def authenticate_client(self, params, basic=None):
        client_id, secret = params.get('client_id', ''), params.get('client_secret', '')
        if basic:
            try:
                client_id, secret = base64.b64decode(basic, validate=True).decode().split(':', 1)
                client_id, secret = urllib.parse.unquote_plus(client_id), urllib.parse.unquote_plus(secret)
            except (ValueError, UnicodeError):
                raise OAuthError('invalid_client', 'Malformed client authentication', 401) from None
        client = self.client(client_id)
        method = client.get('token_endpoint_auth_method', 'none')
        if method != 'none' and not hmac.compare_digest(digest(secret), client.get('secret_hash', '')):
            raise OAuthError('invalid_client', 'Client authentication failed', 401)
        if (method == 'client_secret_basic' and not basic) or (method == 'client_secret_post' and basic):
            raise OAuthError('invalid_client', 'Use the registered authentication method', 401)
        return client_id

    def token(self, params, basic=None):
        client_id = self.authenticate_client(params, basic)
        if params.get('resource') != self.resource:
            raise OAuthError('invalid_target', 'Resource must exactly match the MCP endpoint')
        grant = params.get('grant_type')
        if grant == 'authorization_code':
            code_hash = digest(params.get('code', ''))
            record = self.store.get('code', code_hash)
            verifier = params.get('code_verifier', '')
            if not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}', verifier):
                raise OAuthError('invalid_grant', 'Invalid PKCE verifier')
            if (not record or record['client_id'] != client_id or
                    record['redirect_uri'] != params.get('redirect_uri') or
                    record['resource'] != self.resource or
                    not hmac.compare_digest(record['code_challenge'], challenge(verifier))):
                raise OAuthError('invalid_grant', 'Code, client, redirect, resource, or PKCE does not match')
            if not self.store.get('code', code_hash, consume=True):
                raise OAuthError('invalid_grant', 'Code was already used')
        elif grant == 'refresh_token':
            refresh_hash = digest(params.get('refresh_token', ''))
            record = self.store.get('refresh', refresh_hash)
            if not record or record['client_id'] != client_id or record['resource'] != self.resource:
                raise OAuthError('invalid_grant', 'Invalid refresh token')
            if params.get('scope', SCOPE) != SCOPE:
                raise OAuthError('invalid_scope', 'Refresh cannot change scope')
            if not self.store.get('refresh', refresh_hash, consume=True):
                raise OAuthError('invalid_grant', 'Refresh token was already rotated')
        else:
            raise OAuthError('unsupported_grant_type', 'Use authorization_code or refresh_token')
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        record = {'client_id': client_id, 'resource': self.resource, 'scope': SCOPE}
        self.store.put('access', digest(access), record, 3600)
        self.store.put('refresh', digest(refresh), record, 30 * 86400)
        return {'access_token': access, 'token_type': 'Bearer', 'expires_in': 3600,
                'refresh_token': refresh, 'scope': SCOPE}

    def authorized(self, token):
        record = self.store.get('access', digest(token))
        return bool(record and record.get('resource') == self.resource and record.get('scope') == SCOPE)

    def revoke(self, params, basic=None):
        client_id = self.authenticate_client(params, basic)
        key = digest(params.get('token', ''))
        for kind in ('access', 'refresh'):
            record = self.store.get(kind, key)
            if record and record.get('client_id') == client_id:
                self.store.delete(kind, key)
