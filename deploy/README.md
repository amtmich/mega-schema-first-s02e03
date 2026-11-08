# Hetzner deployment plan

These are reviewable deployment files, not an installer. Do not run privileged
commands or deploy until the owner approves the exact target and changes.
An existing server with Python 3.12+, persistent disk space, an HTTPS reverse
proxy, and available capacity is sufficient. No new server is required if one
is already available. Server creation and any associated charges require a
separate approval.

## Layout

- `/opt/schema-first`: public source code, root-owned, read-only to the service.
- `/var/lib/schema-first`: persistent SQLite data, owned by `schema-first`, mode
  0700. Back this up separately from the source repository.
- `/etc/schema-first.env`: environment and secrets, root-owned, mode 0600.
- `/etc/systemd/system/schema-first.service`: the provided service unit.
- A dedicated hostname such as `mcp.example.com`, pointed at the approved server.
- App binds only to `127.0.0.1:8765`; the existing reverse proxy publishes HTTPS.

The application has no pip dependencies. A dedicated unprivileged Linux service
user runs it. The systemd unit restricts writes to its data directory and private
temporary directory, and restarts it after failure or server reboot.

## Before deployment

1. Confirm the SSH hostname/user, host key, operating system, existing services,
   reverse proxy, free port, firewall rules, disk capacity, and available domain.
2. Ask the owner to approve the precise service installation, secret transfer,
   DNS/proxy changes, and public deployment. If Caddy is not installed, its
   installation needs approval too. Preserve the existing proxy configuration.
3. Transfer only public source files plus the separately approved secrets.
   Never transfer the private course logs or the development SQLite database.

## Service setup after approval

Create the dedicated service user and directories. Copy the source to
`/opt/schema-first`, the service unit into systemd's unit directory, and a filled
`server.env.example` into `/etc/schema-first.env`. Use a new owner password and
the approved Gemini key. Do not put either secret in shell arguments, Git,
cloud-init user-data, the Caddyfile, or the chat transcript.

Set ownership and permissions as described above, then enable and start
`schema-first.service`. Verify `http://127.0.0.1:8765/health` on the host before
changing the public reverse proxy.

## HTTPS

If Caddy already serves this host, add a dedicated virtual host like the example;
validate the complete configuration before reloading it. Do not replace an
existing Caddyfile. If nginx already runs, add a separate server block and use
the host's existing certificate management instead of installing a second proxy.

[Caddy's automatic HTTPS](https://caddyserver.com/docs/automatic-https) requires
DNS pointing to the server and inbound ports 80 and 443. It obtains and renews
the certificate. Do not expose the application's loopback port in the firewall.
Keep SSH access restricted according to the existing host policy.

## Verification and recovery

From a different machine, verify HTTPS health, resource metadata, `/plan`, 401
without credentials, and OAuth consent. Use the harness to call all four tools,
including real audio. Restart only the dedicated application service and verify
that a previously written note and uploaded file remain available.

Inspect service status using `systemctl status schema-first --no-pager`. The app
does not log request URLs, bodies, passwords, or tokens. Keep proxy access logs
private because OAuth callback query parameters can contain temporary codes.

To roll back, restore the previous source revision and restart only this service.
To remove it, stop and disable this dedicated service and remove only its virtual
host. Retain the database until the owner explicitly approves its deletion.
