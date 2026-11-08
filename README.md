# S02E03 — Schema First

Local implementation of an authenticated remote MCP server. **The challenge is
not solved yet:** public deployment, harness connection, and course acceptance
are pending. The owner approved publishing this repository and Gemini use, and
requested Hetzner hosting; the exact Hetzner target is not selected yet.

Real Gemini transcription passed on 2026-11-08 using the public 11-second
[`whisper.cpp` JFK sample](https://github.com/ggml-org/whisper.cpp/blob/master/samples/jfk.wav).
It returned three chronological segments starting at 0.00, 2.78, and 6.16 seconds,
including “ask not what your country can do for you”. This verifies the provider
adapter locally; remote operation and course acceptance remain unverified.

The application uses Python 3.12's standard library. It needs no runtime package
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

## Deployment plan (Hetzner target pending)

See [the Hetzner deployment plan](deploy/README.md), with a restricted systemd
service and an HTTPS proxy example. These files are prepared for review and have
not been installed on a host.

Deploy as a single Python service behind a host-provided HTTPS proxy, with a
persistent writable data directory. Set `BIND_HOST=0.0.0.0`, `PORT` as required by
the host, `PUBLIC_URL` to the public origin, and `DATA_DIR` to the persistent
mount. Configure a new owner password and the approved transcription provider
key through the host's secrets interface. The checked-in Dockerfile is optional;
a native Python runtime can simply start `python3 server.py --host 0.0.0.0`.

Render's free service has an ephemeral filesystem, so it cannot preserve this
SQLite database across restarts. A paid service with a persistent disk is one
option; an existing host with HTTPS and persistent storage is another. The
Docker image, if used, runs as UID 10001; the mounted directory must be writable
by that user. This project does not create a service, publish code, configure
keys, or change harness configuration by itself.

After approval:

1. Deploy and verify HTTPS metadata, owner consent, persistence, and all four
   tools with a real audio sample.
2. Add the remote server to the owner's harness using its supported MCP setup,
   and verify that the harness can discover and call its tools.
3. Submit the endpoint to the course. Preserve the returned note and connect
   link privately. Save the note through `memo_write` **before** the owner opens
   the course connect link.
4. The owner opens that link and approves access in their browser. Submit again
   to run the course's checks; read its newest note and return the `noteA` code
   in the required claim.
5. Only course acceptance marks the challenge solved. Save the debrief then.

Keep all course requests/replies, keys, tokens, and logs under ignored `private/`.
The exact start reply and downloaded specifications are already saved there.

## References

- [MCP specification](https://modelcontextprotocol.io/specification/2026-07-28)
- [Gemini audio](https://ai.google.dev/gemini-api/docs/audio)
- [GenerateContent contract](https://ai.google.dev/api/generate-content)
- [Render persistent disks](https://render.com/docs/disks)
