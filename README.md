# ice-colder
A comprehensive vending machine controller project

## Running

**Local:**
```
uv sync
uv run python main.py
```

**Docker (local dev, builds your checkout):**
```
docker compose up --build
```
Without `--build`, compose uses the published `ghcr.io/conradstorz/ice-colder`
image instead of your working tree. Config lives at `data/config.json`
(created automatically on first run).

A local `--build` shows `unknown` as the VMC build on the health tab unless you
pass the identity args CI uses:
```
docker compose build --build-arg VCS_REF=$(git rev-parse HEAD) --build-arg BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
```

Dashboard: http://localhost:26123
Owner status screen (read-only, phone-friendly): http://localhost:26123/screen

**Continuous deployment (simulation host):** every push to `main` runs CI
(`.github/workflows/ci.yml`: ruff, pytest, image build) and publishes
`ghcr.io/conradstorz/ice-colder:latest` plus a `sha-<commit>` tag. The compose
services carry the `com.centurylinklabs.watchtower.enable=true` label, so a host
running Watchtower picks the new image up on its schedule (hpz440: daily 04:00).
To update right away:
```
docker compose pull
docker compose up -d
```
The real machine should pin a `sha-<commit>` tag instead of `latest`.

### Credentials

Copy `.env.example` to `.env` before bringing up either compose stack (the
root `docker-compose.yml` or `docker/docker-compose.prod.yml`):
```
cp .env.example .env
```
Then set:
- `MQTT_PASSWORD` — at least 12 characters; `mosquitto-init` refuses the
  example placeholder. The broker password shared by the VMC and the
  simulators, which all authenticate as the same `MQTT_USERNAME`. A
  one-shot `mosquitto-init` service writes it into the broker's password file
  before `mosquitto` starts.
- `HA_MQTT_PASSWORD` — optional. Set it (12+ characters) to give Home Assistant
  its own broker account, named by `HA_MQTT_USERNAME` (default
  `homeassistant`), instead of sharing the VMC's credential. Left empty, no
  such account is created. `mosquitto-init` rewrites the password file from
  `.env` on every start, so accounts added by hand with `mosquitto_passwd` are
  discarded on the next `docker compose up` — put them here instead.
- `MQTT_BIND_ADDR` — the LAN interface address the broker listens on, so port
  1883 is never reachable through a stray port-forward or a second NIC.
- `ICE_COLDER_TRUSTED_PROXIES` — the Docker network(s) Traefik reaches the
  VMC from (comma-separated CIDRs); `X-Forwarded-For` is trusted only from
  these when the dashboard's login limiter picks a client IP to rate-limit.

Before exposing a host through Traefik, confirm three things outside this
repo: the router forwards only 80/443 (never 1883); Traefik's `websecure`
entrypoint has TLS; and Traefik runs without `forwardedHeaders.trustedIPs`
or `insecure` set, so it discards any `X-Forwarded-For` a client supplies
(the login limiter's trusted-proxy rule depends on that).

The dashboard itself refuses to start bound to a public interface with an
admin password under 12 characters or a known default; set
`ICE_COLDER_ALLOW_WEAK_PASSWORD=1` only on a private test host, never in the
committed compose stacks. On first run with no admin password configured, the
VMC generates one and prints it once — capture it then, it is not logged
again.
