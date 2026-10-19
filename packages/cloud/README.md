# ice_cloud: fleet portal

A multi-owner fleet server. Machines prove who they are with a key they generated; people sign in with Google. Each machine has one owner, owners invite technicians, and a platform admin (the operating company) is an email allowlist. Spec: `docs/superpowers/specs/2026-10-19-cloud-identity-tenancy-design.md`. Wire contract: `docs/contracts/edge-cloud/CONTRACT.md`.

The edge works fully offline without this server. The cloud is additive and off unless `cloud.enabled` is true in the machine's `config.json`.

## Configuration

All configuration is environment variables, read once at start (`ice_cloud.settings`).

| Variable | Meaning | Default |
|---|---|---|
| `ICE_CLOUD_SESSION_KEY` | Cookie HMAC key, at least 32 bytes. Required; the server exits 1 without it. | none |
| `ICE_CLOUD_PUBLIC_URL` | External base URL, for the OIDC redirect URI. Required when Google is configured. | none |
| `ICE_CLOUD_GOOGLE_CLIENT_ID`, `ICE_CLOUD_GOOGLE_CLIENT_SECRET` | Google OAuth client. Set both or neither; with neither, `/login` says sign-in is not configured and the machine API still works. | none |
| `ICE_CLOUD_ADMINS` | Comma-separated admin emails (case-insensitive). | empty |
| `ICE_CLOUD_INSECURE_COOKIES` | `1` drops the `Secure` cookie flag, for local http only. | unset |
| `ICE_CLOUD_DATA` | Data directory: `cloud.db` (SQLite), `machines/` (report snapshots), `ui/` (uploaded bundles). | `./cloud_data` |
| `ICE_CLOUD_HOST` | Bind host. Port is fixed at 26124. | `127.0.0.1` |

Generate a session key: `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Changing it signs everyone out.

## Run locally

```
export ICE_CLOUD_SESSION_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export ICE_CLOUD_INSECURE_COOKIES=1
uv run python -m ice_cloud
```

Without Google credentials nobody can sign in (the portal pages need a session), but machines can register and report. To sign in locally, set up the Google client below with `ICE_CLOUD_PUBLIC_URL=http://127.0.0.1:26124`.

With Docker: copy `.env.example` to `.env`, fill the `ICE_CLOUD_*` block, then `docker compose -f docker/docker-compose.cloud.yml up --build`. The host port is bound to `127.0.0.1:26124`; the container binds `0.0.0.0` inside.

## Google sign-in

1. In Google Cloud Console create an OAuth client of type **Web application** (APIs and Services, Credentials).
2. Add the authorized redirect URI `{ICE_CLOUD_PUBLIC_URL}/auth/google/callback`, exactly (scheme, host, port, no trailing slash), for example `https://fleet.example.com/auth/google/callback`.
3. Put the client id and secret in `ICE_CLOUD_GOOGLE_CLIENT_ID` / `ICE_CLOUD_GOOGLE_CLIENT_SECRET` and restart.

The flow is authorization code with PKCE; the ID token is verified (signature, issuer, audience, expiry, nonce) and the email must be verified. The first sign-in creates the account; anyone with a Google account can sign up, and then claim a machine or accept an invitation. Sessions last 30 days idle, 90 days absolute. Provider code is in `ice_cloud.oidc_google`, the protocol in `ice_cloud.oidc`, sessions and dependencies in `ice_cloud.auth`.

## Reverse proxy

The server speaks plain HTTP. Terminate HTTPS in a reverse proxy (Traefik, Caddy, nginx) in front of `127.0.0.1:26124`; session cookies are `Secure`, so sign-in does not work over http except with `ICE_CLOUD_INSECURE_COOKIES=1`.

- **No path prefix.** Machines sign the request path as the cloud sees it (`request.url.path`), and the portal templates use root-absolute URLs. Serve the cloud at the root of its host, or strip any prefix at the proxy before forwarding. A prefix left in place makes every machine signature fail (401).
- **Pass the real client address.** The claim back-off is keyed on the client IP. Behind a proxy that address must reach the app as the connection peer. The cloud does not yet read `X-Forwarded-For`, so today every claimant behind the proxy shares one back-off bucket (one attacker can lock out legitimate claims, never bypass the check). Open item.
- **Rate limiting** on `/login` and the machine API is a proxy concern for now.
- **Body limits.** The app itself caps request bodies (`BodyLimitMiddleware`): 20 MiB for the UI-bundle `PUT`, 256 KiB for everything else, answered 413 with an empty body. A proxy limit at least that large is fine.

## Claiming a machine, and transfer

Ownership is proven by a code minted **at the machine**; the cloud only ever stores its hash. Identity is the machine's key fingerprint (shown as `xxxx-xxxx-xxxx-xxxx`), never the editable `machine_id`, which is just a display label.

**First claim (machine side).** Enable the uplink (`cloud.enabled` true, `cloud.url` set) and restart. The machine creates `data/cloud_identity.json` (Ed25519 key, mode 0600) and registers as an unclaimed machine. Its short fingerprint is on `/health` and `/users/ownership`. The live code is one of: the first-run setup code, a pending ownership-transfer code, or a "Link to cloud" code. For a machine already set up, open `/users/ownership`, choose **Link to cloud**, enter the owner PIN, and note the 8-digit code (valid 7 days, shown once; cancellable).

**First claim (cloud side).** Sign in, open `/claim`, enter the short fingerprint and the code. On success you are the owner, the machine appears in `/fleet`, and the claim code is spent in the cloud. Wrong codes, unknown machines and expired codes all give the same 403 and each failure costs the same scrypt time; repeated failures back off exponentially (429).

**Resale.** The seller starts a transfer at the machine (existing flow, `/users/ownership`), which mints a transfer code. The buyer signs in to the cloud, claims with that code and fingerprint, then completes `/setup` at the machine with the same code. The two steps are independent and either order works. Claiming drops the previous owner's access immediately and deletes every technician membership; the new owner re-invites whom they trust. A local transfer the cloud never heard about needs no special handling: the new owner mints a Link-to-cloud code and claims.

**Technicians.** The owner opens `/machines/{short}/people`, invites by email (this machine or all machines owned at acceptance time), and passes on the invitation link (valid 7 days). There is no cloud mailer yet, so the link is shown, not emailed. The invitee signs in with a Google account whose email matches, and opens the link.

## Platform admin

Add Google emails to `ICE_CLOUD_ADMINS` and restart; that is the whole procedure. Admins see `/admin`: every machine with owner or "unclaimed", last seen, claim open or not, plus **Unbind** (owner removed, memberships deleted, two-tap) and **Suspend/Unsuspend** for accounts. Admins can view every machine's pages but can never set an owner: ownership always passes through a code minted at the machine. Non-admins get 404 on `/admin`.

## Key rotation

A machine's identity is its key. To rotate it, delete `data/cloud_identity.json` on the machine and restart. It comes back as a new, unclaimed machine with a new fingerprint: mint a Link-to-cloud code and claim again. The old record stays in the cloud until an admin unbinds it. There is no signed rotate command yet.

## Troubleshooting

- Server exits at start: read stderr; it names the missing or short `ICE_CLOUD_SESSION_KEY` or an unpaired Google id/secret.
- Machine gets 401 on every request: clock skew over 300 s, a path prefix in front of the app, or a regenerated identity file.
- Uploaded UI bundles render without `request` in their template context, by design.
