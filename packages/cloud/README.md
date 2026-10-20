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
| `ICE_CLOUD_TRUSTED_UI_DIGESTS` | Comma-separated sha256 digests of machine UI bundles the portal may render. Empty: only the cloud's own installed `ice_ui` renders; a machine reporting any other digest is shown with the installed UI and a mismatch banner. See "Uploaded UI bundles". | empty |
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

## Uploaded UI bundles

Any registered machine can upload a UI bundle, and a bundle holds HTML and static files that would be served from the cloud's own origin. So the portal **never renders or serves an uploaded bundle unless its digest is listed** in `ICE_CLOUD_TRUSTED_UI_DIGESTS`. Unlisted digests are stored but ignored: the machine's pages render with the installed UI and the mismatch banner. Even trusted bundles render in a Jinja sandbox without `request`, and their static files are sent with `Content-Security-Policy: sandbox`.

To trust a release, get its digest from the release you built or deployed and add it to the variable, then restart:

```
uv run python -c "from ice_ui.bundle import ui_digest; print(ui_digest())"
```

The deep link `/m/{short}` selects the machine and then redirects to `/fleet`; it never renders a machine page directly, so a cross-site click cannot land on one.

## Reverse proxy

The server speaks plain HTTP. Terminate HTTPS in a reverse proxy (Traefik, Caddy, nginx) in front of `127.0.0.1:26124`; session cookies are `Secure`, so sign-in does not work over http except with `ICE_CLOUD_INSECURE_COOKIES=1`.

- **No path prefix.** Machines sign the request path as the cloud sees it (`request.url.path`), and the portal templates use root-absolute URLs. Serve the cloud at the root of its host, or strip any prefix at the proxy before forwarding. A prefix left in place makes every machine signature fail (401).
- **Pass the real client address.** The claim back-off is keyed on the client IP. Behind a proxy that address must reach the app as the connection peer. uvicorn honours `X-Forwarded-For` from loopback peers by default, so a proxy on the same host works as is. When the proxy is not on loopback (for example a Docker bridge address), uvicorn ignores the header and every claimant shares one back-off bucket (one attacker can lock out legitimate claims, never bypass the check): set `FORWARDED_ALLOW_IPS` (uvicorn `--forwarded-allow-ips`) to the proxy's address.
- **Rate limiting** on `/login` and the machine API is a proxy concern for now.
- **Open registration (open item).** `POST /api/v1/machines/register` accepts any new keypair, so anyone can create unclaimed machine rows. Bundle uploads are limited to claimed machines (401 otherwise) and to 4 distinct new digests per machine in any 30 days (409 for a fifth; re-sending a digest the cloud already holds is a 200 and never counts), and the edge retries a refused bundle only about hourly, which bounds disk use, but registration itself and `/login` still need rate limiting at the proxy. An unclaimed machine still reports (its reports are stored and its last-seen time updates); only its bundle upload is refused, retried each pass, and the portal shows the installed UI with a mismatch banner until a claimed machine's bundle is accepted and trusted.
- **Body limits.** The app itself caps request bodies (`BodyLimitMiddleware`): 20 MiB for the UI-bundle `PUT`, 256 KiB for everything else, answered 413 with an empty body. A proxy limit at least that large is fine.

## Claiming a machine, and transfer

Ownership is proven by a code minted **at the machine**; the cloud only ever stores its hash. Identity is the machine's key fingerprint (shown as `xxxx-xxxx-xxxx-xxxx`), never the editable `machine_id`, which is just a display label.

**First claim (machine side).** Enable the uplink (`cloud.enabled` true, `cloud.url` set) and restart. The machine creates `data/cloud_identity.json` (Ed25519 key, mode 0600) and registers as an unclaimed machine. Its short fingerprint is on `/health` and `/users/ownership`. The live code is one of: the first-run setup code, a pending ownership-transfer code, or a "Link to cloud" code. For a machine already set up, open `/users/ownership`, choose **Link to cloud**, enter the owner PIN, and note the 8-digit code (valid 7 days, shown once; cancellable).

**First claim (cloud side).** Sign in, open `/claim`, enter the short fingerprint and the code. On success you are the owner, the machine appears in `/fleet`, and the claim code is spent in the cloud. Wrong codes, unknown machines and expired codes all give the same 403 and each failure costs the same scrypt time; repeated failures back off exponentially (429).

**Resale.** The seller starts a transfer at the machine (existing flow, `/users/ownership`), which mints a transfer code. The buyer signs in to the cloud, claims with that code and fingerprint, then completes `/setup` at the machine with the same code. **Claim on the cloud before pressing Done at the machine.** Done finalizes the local setup and sends the cloud a `DELETE` that closes the claim, so after Done the transfer code no longer works in the cloud; if the buyer pressed Done first, they mint a Link-to-cloud code and claim with that. A code the cloud has already consumed is never reopened by the machine re-announcing it. Claiming drops the previous owner's access immediately and deletes the technician memberships when the owner changes (re-claiming your own machine keeps your techs); the new owner re-invites whom they trust. A local transfer the cloud never heard about needs no special handling: the new owner mints a Link-to-cloud code and claims.

**Technicians.** The owner opens `/machines/{short}/people`, invites by email (this machine or all machines owned at acceptance time), and passes on the invitation link (valid 7 days). Invitation links are shown, not emailed (the alert mailer does not send invitations). The invitee signs in with a Google account whose email matches, and opens the link.

## Platform admin

Add Google emails to `ICE_CLOUD_ADMINS` and restart; that is the whole procedure. Admins see `/admin`: every machine with owner or "unclaimed", last seen, claim open or not, plus **Unbind** (owner removed, memberships deleted, two-tap) and **Suspend/Unsuspend** for accounts. Admins can view every machine's pages but can never set an owner: ownership always passes through a code minted at the machine. Non-admins get 404 on `/admin`.

## Alerts

The cloud emails a claimed machine's owner and technicians when a fault is raised, when the machine stops reporting, and when it reports again. Nothing extra crosses the wire: each accepted report is compared with the previous report's fault list kept in `machine_alert_state`. A fault newly present is `raised`, one that vanished is `cleared`. A machine silent for more than 3 x its `report_interval_seconds` is flagged `offline` by a sweep that runs every 60 s, and its next report produces `online`. A fault that raises and clears between two reports is never seen. Unclaimed machines produce no events. Claiming or unbinding a machine deletes its alert state.

**Who gets what.** Owners and technicians receive `raised` faults of gate `safety` or `fulfillment`, plus `offline` and `online`. Only the owner receives `raised` faults of gate `alert` (for example `DATA-101`). `cleared` events never trigger an email alone; they ride along in the next email to that person. Each person can be muted per machine on `/machines/{short}/people` (the owner can mute anyone, a technician only themselves). A mute is removed when the person loses access. Suspended accounts get nothing.

**Cooldown and bundling.** At most one email per person per machine every 10 minutes, counted from the last successful send. Events inside the window are held as `pending` and go out together, with any `cleared` events since the person's last email, once the window closes (the sweep flushes, so within about a minute of it). A failed send is retried on later passes under the same cooldown gate, up to 5 attempts, then stays `failed`.

**SMTP.** Set in the cloud's environment (see `.env.example`):

| Variable | Meaning |
|---|---|
| `ICE_CLOUD_SMTP_HOST` | Unset = log-only: alerts are recorded and logged, nothing is sent. |
| `ICE_CLOUD_SMTP_PORT` | Default 587. |
| `ICE_CLOUD_SMTP_USER`, `ICE_CLOUD_SMTP_PASSWORD` | Optional pair. A user without a password is a startup error. |
| `ICE_CLOUD_SMTP_FROM` | Required when the host is set. |
| `ICE_CLOUD_SMTP_STARTTLS` | On unless `0`. |

Until `ICE_CLOUD_SMTP_HOST` is set the cloud logs one warning at boot and tells every machine `alerts_handled: false`, so the edge keeps emailing locally exactly as before. Once SMTP is configured, a claimed machine defers its own alert email to the cloud; if the cloud stops accepting reports for 3 x the report interval (3 minutes at the default), the edge resumes local email until reports are accepted again. The machine's `/health` page shows "Alerts: cloud" or "Alerts: local". A fault raised during such a gap is emailed by the cloud when the first report gets through; one that raised and cleared inside the gap is emailed by nobody.

**Where to look.** `/machines/{short}/alerts` lists the last 200 events with one line per recipient (`sent`, `pending`, `failed: <error>`, `muted`) and is open to anyone who can open the machine. `/fleet` shows alerts in the last 24 hours per machine. Admins see the SMTP status on `/admin` and can send a test email to their own address.

**Startup grace.** After a cloud restart the sweep flags nobody offline until 3 x that machine's interval has passed since the cloud started, so downtime does not email the fleet "offline" then "online".

**Open items.** No SMTP rate limiting beyond the cooldown. The sweep is one in-process task, so run one cloud instance. A `failed` delivery whose machine or recipient has since vanished burns its 5 attempts. Failed sends have no backoff beyond the cooldown gate. `FORWARDED_ALLOW_IPS` is unrelated to alerts.

## Key rotation

A machine's identity is its key. To rotate it, delete `data/cloud_identity.json` on the machine and restart. It comes back as a new, unclaimed machine with a new fingerprint: mint a Link-to-cloud code and claim again. The old record stays in the cloud until an admin unbinds it. There is no signed rotate command yet.

## Troubleshooting

- Server exits at start: read stderr; it names the missing or short `ICE_CLOUD_SESSION_KEY` or an unpaired Google id/secret.
- Machine gets 401 on every request: clock skew over 300 s, a path prefix in front of the app, or a regenerated identity file.
- Uploaded UI bundles render without `request` in their template context, by design.
