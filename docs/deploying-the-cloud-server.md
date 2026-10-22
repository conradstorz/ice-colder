# Deploying the cloud server

How the fleet portal (`ice_cloud`, `packages/cloud`) runs on a public host, as built on
2026-10-21 for `https://vmc.ohr3023.org`. The host is a DigitalOcean droplet, but nothing
below is DigitalOcean-specific beyond the two control-panel items in section 9. The portal
itself is documented in `packages/cloud/README.md`; this page is the operations side: what
the box runs, how it was hardened, how to update it, and how to rebuild it from scratch.

## 1. The host

| | |
|---|---|
| Provider | DigitalOcean droplet, NYC1, 1 vCPU, 512 MB RAM, 10 GB disk ($4/month) |
| OS | Ubuntu 24.04 LTS x64 |
| Public IP | `165.227.203.198` |
| DNS | `vmc.ohr3023.org` A record (Cloudflare, **DNS only**, grey cloud) |
| Access | `ssh root@vmc.ohr3023.org`, key only (the workstation's `~/.ssh/id_ed25519`, which is also published at `github.com/conradstorz.keys`) |
| Deployment files | `/opt/ice-cloud/docker/` |
| Data | `/opt/ice-cloud/cloud_data/` (SQLite `cloud.db`, report snapshots, UI bundles) |

512 MB is enough: idle, the cloud container uses about 75 MB and Caddy about 17 MB, with
roughly 120 MB of the 1 GB swap file in use. Never `docker build` on this host; CI builds
the image and the host only pulls it. Nothing else should be deployed on this tier.

## 2. What runs

Two containers from one compose project (`ice_cloud`):

- **`ice-cloud`**: `ghcr.io/conradstorz/ice-colder-cloud:latest`, the portal, bound to
  `127.0.0.1:26124` on the host. CI publishes this image on every push to `main`
  (`.github/workflows/ci.yml`, `image` job; tags `latest` and `sha-<commit>`, amd64 only).
- **`ice-cloud-caddy`**: `caddy:2-alpine`, the HTTPS front on 80/443. It obtains and renews
  the Let's Encrypt certificate for `ICE_CLOUD_DOMAIN`, redirects HTTP to HTTPS, adds the
  security headers, strips the `Server` header and proxies to `cloud:26124`.

The compose files are the repository's own, copied verbatim:

| On the host | In the repository |
|---|---|
| `docker-compose.cloud.yml` | `docker/docker-compose.cloud.yml` |
| `docker-compose.cloud.proxy.yml` | `docker/docker-compose.cloud.proxy.yml` |
| `Caddyfile` | `docker/Caddyfile` |
| `.env` (mode 0600) | the `ICE_CLOUD_*` block of the workstation `.env`, plus `ICE_CLOUD_DOMAIN` |

Every compose command on the host is run from `/opt/ice-cloud/docker` with both files:

```
cd /opt/ice-cloud/docker
docker compose -f docker-compose.cloud.yml -f docker-compose.cloud.proxy.yml <command>
```

The proxy override also hardens the cloud container: it runs as uid 1000 with a read-only
root filesystem, every capability dropped and `no-new-privileges`, and calls the venv's
`python -m ice_cloud` directly so `uv` needs no cache or home directory. Because of the uid,
**`/opt/ice-cloud/cloud_data` must be owned by `1000:1000`, recursively (`chown -R`, including `cloud_data/backups`, so a root-created `backups/` does not block the overlay)**; a permission error at start
means that `chown` was missed. It also sets `FORWARDED_ALLOW_IPS=*` so uvicorn trusts
Caddy's `X-Forwarded-For` from the bridge network (the claim back-off is keyed on the real
client address; only Caddy can reach the port).

## 3. Environment

`/opt/ice-cloud/docker/.env` holds exactly these, all documented in `.env.example`:

```
ICE_CLOUD_PUBLIC_URL=https://vmc.ohr3023.org
ICE_CLOUD_DOMAIN=vmc.ohr3023.org
ICE_CLOUD_GOOGLE_CLIENT_ID=...
ICE_CLOUD_GOOGLE_CLIENT_SECRET=...
ICE_CLOUD_ADMINS=conradstorz@gmail.com
ICE_CLOUD_SESSION_KEY=...            # secrets.token_urlsafe(48)
ICE_CLOUD_TRUSTED_UI_DIGESTS=
ICE_CLOUD_INSECURE_COOKIES=
```

The Google OAuth client lives in the Google Cloud project `gte-automation-project`, type
Web application, with the single authorized redirect URI
`https://vmc.ohr3023.org/auth/google/callback`. The client-secret JSON Google offers for
download does not include the redirect URI, so a client created from the console must have
it added by hand or sign-in fails with `redirect_uri_mismatch`.

Registration is open, so three optional variables bound what an unclaimed machine can
cost (defaults shown; each is a positive integer, anything else stops the cloud at start):

```
ICE_CLOUD_MAX_UNCLAIMED_MACHINES=200        # new registrations beyond this answer 429
ICE_CLOUD_UNCLAIMED_TTL_DAYS=7              # unseen this long and never claimed: purged
ICE_CLOUD_MAX_UNCLAIMED_REPORT_BYTES=16384  # larger reports from unclaimed machines: 413
```

Independently, each client address may register at most 10 new machines per rolling hour
(further ones answer 429 with `Retry-After`); machines that are already registered are
never throttled.

SMTP is not configured, so the cloud answers every report with `alerts_handled: false` and
each edge keeps emailing its own alerts (`packages/cloud/README.md`, Alerts).

## 4. Host hardening

Applied once, by hand, on 2026-10-21. Each item is idempotent and can be re-applied.

- **SSH** (`/etc/ssh/sshd_config.d/10-harden.conf`): `PermitRootLogin prohibit-password`,
  `PasswordAuthentication no`, `KbdInteractiveAuthentication no`, `X11Forwarding no`,
  `MaxAuthTries 3`, `LoginGraceTime 30`, `AllowUsers root`. Check with `sshd -T` and restart
  with `systemctl restart ssh` after `sshd -t` passes.
- **fail2ban** (`/etc/fail2ban/jail.local`): `backend = systemd`, sshd jail in `aggressive`
  mode, 4 failures in 10 minutes gives a 1 hour ban, escalating up to a week. Ubuntu names
  the unit `ssh.service`, not `sshd.service`, so the jail carries
  `journalmatch = _SYSTEMD_UNIT=ssh.service + _COMM=sshd`; without that line the default
  jail watches a unit that does not exist and never bans anyone. Verify with
  `fail2ban-client status sshd` (the `Journal matches` line must say `ssh.service`).
- **Firewall**: `ufw` with default deny incoming, allowing `22/tcp`, `80/tcp` and `443`
  (TCP and UDP, for HTTP/3). Docker publishes ports past ufw, which is why the cloud port is
  bound to loopback in the compose file rather than relying on the firewall.
- **Patching**: `unattended-upgrades` with `Automatic-Reboot "true"` at 09:30 UTC and
  `Remove-Unused-Dependencies "true"` (`/etc/apt/apt.conf.d/52unattended-upgrades-local`).
  A reboot brings both containers back through their `restart: unless-stopped` policy.
- **Docker daemon** (`/etc/docker/daemon.json`): json-file logs capped at 10 MB x 3 per
  container, `live-restore` so a daemon restart keeps containers running, and
  `no-new-privileges` for every container.
- **Swap**: a 1 GB `/swapfile` in `/etc/fstab`, `vm.swappiness=10`
  (`/etc/sysctl.d/99-swap.conf`).
- **HTTP headers** (Caddyfile): `Strict-Transport-Security` (one year, subdomains),
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` (the portal uses no frames),
  `Referrer-Policy: strict-origin-when-cross-origin`, `Server` removed.

Things still open on the proxy side are listed in `packages/cloud/README.md` under Reverse
proxy: there is no rate limiting on `/login` or on machine registration beyond the app's own
claim back-off.

## 5. Updating the cloud

CI publishes a new image for every push to `main`. The host does not run Watchtower, so an
update is manual and takes a few seconds of downtime:

```
cd /opt/ice-cloud/docker
docker compose -f docker-compose.cloud.yml -f docker-compose.cloud.proxy.yml pull cloud
docker compose -f docker-compose.cloud.yml -f docker-compose.cloud.proxy.yml up -d
docker inspect ice-cloud --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'
```

The last line prints the commit the running container was built from; compare it with the
commit you expect. Wait for the right CI run: `gh run list --branch main` shows one run per
push, and pulling before the run for your commit finishes just re-pulls the previous image.

When `docker/Caddyfile` or `docker/docker-compose.cloud.proxy.yml` change in the repository,
copy them to `/opt/ice-cloud/docker/` and run the same `up -d`. Upgrade the cloud before the
edges when the edge-cloud contract changes (README, Alerts).

## 6. Rebuilding from scratch

1. Create an Ubuntu 24.04 host, add your SSH public key, point the DNS A record at it with
   proxying off.
2. Swap, Docker CE from Docker's apt repository (`docker-ce docker-ce-cli containerd.io
   docker-buildx-plugin docker-compose-plugin`), then every item in section 4.
3. `mkdir -p /opt/ice-cloud/docker /opt/ice-cloud/cloud_data`, copy the four files from the
   table in section 2 (write `.env` from `.env.example`, `chmod 600`), and
   `chown -R 1000:1000 /opt/ice-cloud/cloud_data`.
4. `docker compose ... pull`, then `up -d`. Caddy requests the certificate on first start;
   `curl -I https://<domain>/login` should answer 200 with the headers above within a minute.
5. Sign in once with the admin account so it exists, then claim machines (section 8).

Without a copy of `cloud_data`, every machine is new to the rebuilt cloud: owners, techs,
claims and alert history are in `cloud.db`, and each edge re-registers automatically but
must be claimed again.

## 7. Verifying

```
curl -sI https://vmc.ohr3023.org/login | grep -iE '^(strict|x-frame|x-content|referrer|server)'
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://vmc.ohr3023.org/api/v1/machines/register   # 401
ssh root@vmc.ohr3023.org 'docker logs --since 5m ice-cloud | grep /api/v1 | tail'
ssh root@vmc.ohr3023.org 'free -m; fail2ban-client status sshd'
```

The access log shows one `POST .../report` per machine per report interval (60 s for the
simulated fleet). An unclaimed machine also shows `PUT /api/v1/ui-bundles/... 401` once and
then about hourly; that is the documented refusal, not a fault.

## 8. The simulated fleet

Three simulated machines report to this cloud from the LAN Docker host `hpz440`
(`/home/gte/ice-colder`, compose project `ice_fleet`, `docs/running-a-virtual-fleet.md`).
They were claimed for the admin account on 2026-10-21. To claim a new machine, sign in,
open `/claim`, and enter the fingerprint and setup code from its container log
(`docker compose -f docker/docker-compose.fleet.yml logs | grep -iE 'setup code|fingerprint'`).

A claim can also be made server-side, with the same code check and compare-and-swap the
route performs, by running a short script against `ice_cloud.store.sqlite.SqliteRepository`
inside the container (`docker exec -i ice-cloud python - <args> < script.py`): resolve the
short fingerprint, `verify_secret(code, machine.claim_code_hash)`, then
`repo.claim_machine(fingerprint, account.id, now, expected_hash=...)`. This is for an
operator who already holds the codes; it does not bypass the hash check.

## 9. Not done yet

Both need the DigitalOcean control panel:

- **Droplet backups.** The compose `backup` service writes daily `cloud.db` archives to
  `cloud_data/backups` (see `docs/backups.md`, including restore). Also enable DigitalOcean
  droplet backups (about $0.80/month) so the archives themselves live off the host.
- **Cloud firewall.** A provider firewall allowing 22/80/443 in front of ufw, with 22
  limited to known source addresses, is a second layer that survives a host misconfiguration.

## 10. Lessons recorded

- `docker compose -f docker/<file>.yml` takes `docker/` as the project directory and does
  not read the repository root `.env`; pass `--env-file .env` or export the variables.
- An `<a>` that leaves the site (the Google sign-in link) must carry `hx-boost="false"`.
  Inside the shell's `hx-boost="true"` body, htmx fetches the link by XHR, follows the 303
  to `accounts.google.com`, fails CORS and silently stays on the page; the server log shows
  the `/auth/google/start` request and never a callback. `tests/cloud/test_auth.py` pins it.
- Watch the CI run for *your* commit (`gh run list --branch main`) before pulling; the
  `latest` tag moves only when that run's `image` job finishes.
