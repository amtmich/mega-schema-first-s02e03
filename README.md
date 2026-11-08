# S02E03 — Schema First

Authenticated remote MCP server deployed on the owner's Hetzner server.
**Solved on 2026-11-08.** The course accepted the tools, the log-file upload,
the returned note, and its later follow-up.
[The saved debrief](https://hq.mega.dev/challenges/schema-first#debrief) records
the checks, fixes, and lessons from this solve.

- [MCP endpoint](https://static.106.248.62.46.clients.your-server.de/mcp)
- [Plan and data policy](https://static.106.248.62.46.clients.your-server.de/plan)
- [Health](https://static.106.248.62.46.clients.your-server.de/health)

Real Gemini transcription passed on 2026-11-08 using the public 11-second
[`whisper.cpp` JFK sample](https://github.com/ggml-org/whisper.cpp/blob/master/samples/jfk.wav).
It returned three chronological segments starting at 0.00, 2.78, and 6.16 seconds,
including “ask not what your country can do for you”. The provider check also
passed on Hetzner under the restricted service user. Operator checks exercised
all four tool functions, and a note and public upload survived an app restart.
Public HTTPS, metadata, and 401 rejection without a token passed. The owner
completed native Codex OAuth and course consent. All four tools were then called
successfully through the harness's authenticated MCP connection. The course's
stranger checks also passed after improving transcription error messages to name
the URL and cause. Fourteen automated tests pass, including browser consent.

The application uses Python 3.12+ standard library (Python 3.14.4 on the host). It needs no runtime package
installation. It supports MCP `2026-07-28`, including stateless discovery,
per-request metadata, mirrored-header checks, `resultType`, and cache hints.
The previous revision, `2025-11-25`, uses the legacy initialization flow.

## Tools

| Tool | Input | Structured output |
| --- | --- | --- |
| `memo_write` | `text` (1–100,000 characters) | `id` |
| `memo_search` | `query` (empty string lists notes) | `notes: [{id, text, created}]` |
| `upload` | `filename`, `content_base64`, optional `mime_type` | `url` |
| `transcribe` | `url` | `segments: [{start, text}]`, seconds |

Tool schemas are defined in `tools.py`. Every successful tool call includes
validated `structuredContent` and an equivalent JSON text block. Uploaded files
are capped at 20 MB; transcription audio is capped at 14 MB so its base64 request
fits the provider's 20 MB total request limit. Inputs cannot access private IP
addresses. Uploaded file links are public, randomly addressed attachments on the
same server. All authorized clients share the single owner's notes and files.

## Run locally

Create `private/server.env` with owner-only permissions. Set:

```dotenv
PUBLIC_URL=http://127.0.0.1:8765
OWNER_PASSWORD=<a unique password of at least 16 characters>
DATA_DIR=/home/cezar/MEGA.dev/s02e03-schema-first/private/data
# Configure only after the owner approves provider use:
# GEMINI_API_KEY=<your key>
# GEMINI_MODEL=gemini-3.5-flash
```

Then run `python3 server.py`. The default binding is loopback only. Open `/plan`
for the public documentation; `/health` reports server health. Do not expose the
local HTTP port directly to the Internet. Production `PUBLIC_URL` must use HTTPS.

## Authorization

An unauthenticated `/mcp` request returns 401 with protected resource metadata.
Both `/.well-known/oauth-protected-resource/mcp` and the root metadata path work.
Metadata names the same-host `/plan` page as `resource_documentation`.

OAuth supports HTTPS Client ID Metadata Documents and dynamic registration.
Clients must request resource `<PUBLIC_URL>/mcp` and S256 PKCE. The owner enters
the owner password on the consent page, checks the client and exact callback,
and selects Approve. Registration alone does not grant access.

Access tokens expire after one hour. Refresh tokens rotate and expire after 30
days. Tokens and authorization codes are stored only as SHA-256 hashes. Browser
consent is protected by a random one-time form value plus an HttpOnly SameSite
cookie. Public authorization errors never redirect to an unregistered callback.

Administration operates locally against the mounted database:

```bash
python3 admin.py clients
python3 admin.py revoke-client <client_id>
python3 admin.py delete-note <note_id>
python3 admin.py delete-file <file_id>
```

`revoke-client` removes active access tokens, refresh tokens, pending consent,
and authorization codes for that client. Deleting a file invalidates its URL.
Keep the data directory private and back it up using SQLite's backup API.

## Verify

The development tests use the already available `jsonschema` package as an
independent validator. No paid API or hosted service is called by the tests.

```bash
timeout 120 python3 -m unittest -v test_server
```

Tests run a real loopback HTTP server, perform OAuth registration and browser
consent, exchange and revoke tokens, check replay/resource/PKCE failures, call
all four tools, validate JSON Schemas, verify persistence and public downloads,
and reject private network destinations. The transcription provider is replaced
with a test stand-in: **these tests do not verify real speech recognition.** The
separate real-provider check described above verifies that integration.
When `private/spec/schema.json` is present, tests also validate current-protocol
responses against the official MCP schema downloaded from its source repository.

The browser consent regression uses an already-installed Chromium or an already
cached `cypress/included:9.7.0` Docker image; it never downloads a browser or an
image. Without either available, that one test is skipped. It verifies native
form submission, the browser's Origin header, the cross-origin OAuth callback,
and the absence of a cross-origin referrer. Browser automation uses test-only
credentials on a temporary loopback server; it never approves real clients.

## Hetzner deployment

See [the Hetzner deployment plan](deploy/README.md), with a restricted systemd
service and an HTTPS proxy example. The service is installed and enabled on the
owner's Ubuntu 26.04 server, with Caddy 2.6.2 from Ubuntu's package repository.
The application listens only on loopback; Caddy publishes ports 80/443 and
manages a trusted Let's Encrypt certificate for the hostname above.

Deploy as a single Python service behind a host-provided HTTPS proxy, with a
persistent writable data directory. Set `BIND_HOST=0.0.0.0`, `PORT` as required by
the host, `PUBLIC_URL` to the public origin, and `DATA_DIR` to the persistent
mount. Configure a new owner password and the approved transcription provider
key through the host's secrets interface. The checked-in Dockerfile is optional;
a native Python runtime can simply start `python3 server.py --host 0.0.0.0`.

On the current host, source is root-owned at `/opt/schema-first`, persistent
data belongs to the restricted `schema-first` service user at
`/var/lib/schema-first`, and credentials reside in root-only
`/etc/schema-first.env`. The service has `NoNewPrivileges`, `ProtectSystem`, and
`ProtectHome` enabled. The Docker image is an optional alternative and has not
been built for this deployment.

The course requested a scheduler log with one specified first line added. The
owner approved publishing that file; it was uploaded through the native MCP tool
and verified byte-for-byte before submitting its URL. The course accepted it and
left a note, which was read through `memo_search` and returned successfully.

The course returned roughly two hours later and accepted the final claim. The
debrief is saved. The application and Caddy services remain enabled so the owner
can keep using the tools; ordinary MCP calls do not depend on the harness timer
or the owner's laptop being open.

Codex runs on the harness VPS, while the owner's browser runs on their computer.
For any future native Codex OAuth login, forward the configured callback port
5555 over SSH, then run `codex mcp login schema-first --oauth-client-registration
dcr` as the `cezar` user on the harness VPS. Open the URL printed by that client
in the owner's browser. The SSH tunnel is needed for the loopback login callback,
not for ordinary authenticated MCP calls or the course's web callback.

Keep all course requests/replies, keys, tokens, and logs under ignored `private/`.
The exact start reply and downloaded specifications are already saved there.

## References

- [MCP specification](https://modelcontextprotocol.io/specification/2026-07-28)
- [Gemini audio](https://ai.google.dev/gemini-api/docs/audio)
- [GenerateContent contract](https://ai.google.dev/api/generate-content)
- [Caddy automatic HTTPS](https://caddyserver.com/docs/automatic-https)
