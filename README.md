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

### Reusable async helpers

The startup lifecycle helpers can be imported without loading the VMC,
configuration, or web application:

```python
from services.task_lifecycle import run_until_primary_exits
from services.task_supervisor import supervise

async def run():
    await run_until_primary_exits(
        server.serve(),
        supervise("worker", worker.run),
    )
```

Here `server` and `worker` are components supplied by the calling application.
`supervise` accepts a factory that creates a fresh awaitable each time; it logs
crashes or unexpected returns and restarts after five seconds (configurable with
the keyword-only `restart_delay`). Cancellation is propagated, not retried.
`run_until_primary_exits` cancels and awaits background tasks when the primary
finishes, fails, or is cancelled, then returns its result or propagates the
exception. Background tasks should handle their failures, usually via
`supervise`. The lifecycle module uses only the standard library; the supervisor
also requires Loguru and leaves logging configuration to the caller.

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
  these when the dashboard's login back-off picks a client IP to rate-limit.

Before exposing a host through Traefik, confirm three things outside this
repo: the router forwards only 80/443 (never 1883); Traefik's `websecure`
entrypoint has TLS; and Traefik runs without `forwardedHeaders.trustedIPs`
or `insecure` set, so it discards any `X-Forwarded-For` a client supplies
(the login back-off's trusted-proxy rule depends on that).

### Using the Dashboard

The operator's dashboard on the 7-inch tablet has no tabs. **Home** is a status strip and a grid of tiles — tap any tile to navigate. Every level (Health, Products, Inventory, Reports, Controls, Tests, Users, Settings) is a real URL you can bookmark. **Back** goes to the parent level, never through browser history. At the top right, the health pill is always live; tap it to jump to the Faults list. **Lock** on every screen returns to the PIN entry page. The tablet needs no internet — Tailwind and HTMX are vendored locally.

### Authentication and Access Control

There is no default credential. On first boot, the dashboard enters **setup mode**:
an 8-digit setup code is printed in the startup log at warning level
(`docker compose logs vmc`) and displayed on the customer display. A setup wizard
creates the owner account (name, email, 4–8 digit PIN), then shows 20 pre-generated
**emergency codes** once — write them down. `data/access.json` stores only a scrypt
hash of each code, not the plaintext, so it is not a recoverable backup of them.

Four roles exist: `owner` (one per machine), `secretary` (owner's delegates),
`tech` (maintenance), `loader` (stock). Credential is a PIN of 4–8 digits.

A browser the machine has not seen before requires a second factor once: either a
6-digit code emailed when `communication.email_gateway` is configured, or an
emergency code from the pool, which works entirely offline. After that, a PIN alone
signs in on that device.

**Back up `data/access.json`** — it holds every user, PIN hash, trusted device, and
emergency-code hash and is the single point of lockout. It lives inside the
bind-mounted `./data` directory (visible in the Users screen with a count of
unused emergency codes). An owner who loses both their PIN and every emergency
code has **no software recovery** — the only path back is a factory restore:
delete `data/access.json` and rerun the wizard, which discards all users and
devices.
