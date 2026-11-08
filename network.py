"""Bounded public downloads with DNS validation, pinning, and redirect checks."""
import http.client
import ipaddress
import socket
import time
import urllib.parse


def download(url, maximum=20_000_000, redirects=4, deadline_seconds=45):
    deadline = time.monotonic() + deadline_seconds
    for _ in range(redirects + 1):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Use a public HTTP(S) URL without embedded credentials')
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        if port not in (80, 443):
            raise ValueError('Only public ports 80 and 443 are supported')
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError('Private, loopback, link-local, and reserved addresses are blocked')
        ip = addresses[0][4][0]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('Download deadline exceeded')
        cls = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
        conn = cls(parsed.hostname, port, timeout=min(15, remaining))
        # HTTPSConnection still checks the certificate and uses the original hostname for SNI.
        conn._create_connection = lambda address, timeout=None, source_address=None: socket.create_connection(
            (ip, port), timeout, source_address)
        try:
            target = urllib.parse.urlunsplit(('', '', parsed.path or '/', parsed.query, ''))
            conn.request('GET', target, headers={'User-Agent': 'SchemaFirst/1.0', 'Accept-Encoding': 'identity'})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if not location:
                    raise ValueError('Redirect has no destination')
                url = urllib.parse.urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError('Download returned HTTP ' + str(response.status))
            declared = response.getheader('Content-Length')
            if declared and int(declared) > maximum:
                raise ValueError('Download exceeds size limit')
            chunks, length = [], 0
            while True:
                if time.monotonic() >= deadline:
                    raise ValueError('Download deadline exceeded')
                chunk = response.read1(min(65536, maximum + 1 - length))
                if not chunk:
                    break
                chunks.append(chunk)
                length += len(chunk)
                if length > maximum:
                    raise ValueError('Download exceeds size limit')
            return b''.join(chunks), response.getheader('Content-Type', '').split(';')[0].lower()
        finally:
            conn.close()
    raise ValueError('Too many redirects')
