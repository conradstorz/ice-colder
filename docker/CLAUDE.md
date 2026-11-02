# Docker, CI/CD and backups

Scoped `CLAUDE.md`, loaded when working under `docker/`. Moved from the root `CLAUDE.md`; project-wide rules stay there.

### Docker

`Dockerfile` builds from `python:3.12-slim`, installs dependencies with
`uv sync --frozen --no-dev` from `pyproject.toml`/`uv.lock`, and runs
`python -m ice_edge` directly (the project venv is first on `PATH`, so SIGTERM reaches Python with no `uv run` wrapper; port 26123), with uv pinned to 0.7.13 in both Dockerfiles. `docker-compose.yml` orchestrates the VMC,
the three ESP32 simulators, and a mosquitto broker, all with
`restart: unless-stopped`; every service in the root and prod compose files caps its container log at 10 MB x 3 files (`x-logging` anchor), `vmc` and the cloud service have `stop_grace_period: 30s`, and no `mem_limit` is set on purpose (a hard limit could OOM-kill the VMC mid-sale). There is no `requirements.txt` — `pyproject.toml` is
the single dependency source of truth. Inside compose, config lives at
`data/config.json` (bind-mounted `./data:/app/data`, writable for the `vmc`
service and read-only for the simulators), pointed to via `ICE_COLDER_CONFIG`
in each service's `environment` — not bind-mounted directly as
`config.json`, since that file is gitignored and doesn't exist on a fresh
clone.

The edge image has a `HEALTHCHECK` on `GET /healthz` (`routes/home.py`: unauthenticated, no session or cookie, exempt from the setup-mode redirect and corrupt-access-file page in the `access_gate` middleware, plain `ok`), which only reports that the web app is serving; nothing restarts on `unhealthy`. A real machine runs `docker/docker-compose.edge.yml` (vmc + backup, layered on `docker-compose.prod.yml`, image pinned by `ICE_COLDER_IMAGE_TAG`, no Watchtower label); the root compose is the simulation stack. Runbook with rollback: `docs/deploying-a-machine.md`.

CI/CD: `.github/workflows/ci.yml` runs ruff and pytest on every push/PR and,
on `main`, publishes the image to `ghcr.io/conradstorz/ice-colder` (`latest`
and `sha-<commit>`). Compose services reference that image (with `build: .`
kept for local `--build`) and carry the Watchtower enable label, so the
simulation host updates itself; `docker compose pull` then `up -d` forces it.
A `compose-config` CI job runs `docker compose config` against every compose
file (root, `docker/docker-compose.prod.yml`, the two cloud files, and the
fleet file with a dummy `ICE_CLOUD_URL`) with `.env.example` to catch
YAML/interpolation errors before `image` builds.

**Simulated machine image (`docker/machine/`, spec
`docs/superpowers/specs/2026-10-19-simulated-machine-container-design.md`,
walkthrough `docs/running-a-virtual-fleet.md`, reference `docker/machine/README.md`).**
One container that imitates a whole deployed machine, for testing the cloud
portal against a fleet without hardware. `docker/machine/Dockerfile` layers
mosquitto and a stdlib Python launcher (`docker/machine/launcher.py`) on the
edge image (`ARG BASE_IMAGE`, default `ghcr.io/conradstorz/ice-colder:latest`,
set from `ICE_FLEET_BASE_IMAGE` by compose; build context is `docker/machine`,
so the root `.dockerignore` does not apply). The launcher supervises mosquitto
(anonymous, bound to the container's loopback only, `docker/machine/mosquitto.conf`),
the three `ice_sims` boards and `ice_edge`: a dead sim or edge is respawned after
5 s (a failed respawn is logged and retried), a mosquitto exit is fatal and stops
everything with exit 1, and SIGTERM/SIGINT stop every child within
`stop_grace_period`.
`docker/docker-compose.fleet.yml` (project `ice_fleet`, one service `machine`,
`--scale machine=N`) requires `ICE_CLOUD_URL` and accepts `ICE_COLDER_MACHINE_ID`
(default: the container hostname) and `ICE_COLDER_REPORT_INTERVAL` (10-3600,
default 60). On first start the launcher seeds `config.json` (defaults plus
three sample products wired to the vending board's buttons) and
`dispensers.toml` (from `ice_edge.vend.dispensers_doc.render_example()`) into an
anonymous `/app/data` volume; on every start it re-applies `machine_id`,
`cloud.enabled`/`url`/`report_interval_seconds` and `mqtt.broker_host=127.0.0.1`
(clearing `mqtt.username`/`password`) and never touches `cloud_identity.json`
or `access.json`, so a machine's fingerprint and owner survive
`docker stop`/`start` and in-place recreates but not `compose down` or a
scale-down (fresh anonymous volumes, orphans left for `docker volume prune`).
Non-goals by design: no scenario knobs or files (the sims' random behaviour is
the scenario), no auto-claim (claim at the cloud's `/claim` with the setup code
and fingerprint the container logs), ephemeral dashboard ports only, no
Watchtower label, no published fleet image, amd64 only. Tests:
`tests/docker/test_machine_launcher.py` loads the launcher by file path and
overrides `event_loop_policy` with the proactor policy on Windows, because
`tests/firmware/conftest.py` installs the selector policy session-wide and that
loop cannot spawn subprocesses. CI: the `image` job's "Smoke-test the simulated
machine image" step builds `docker/machine` on the just-built smoke image with
`docker buildx build --builder default --load` (the setup-buildx container
builder cannot see daemon-local images) and polls the container log for up to
60 s for the six boot lines (mosquitto listening, the three sims connected, the
edge's main loop, setup mode); `compose-config` parses the fleet file.

Both the root `docker-compose.yml` and `docker/docker-compose.prod.yml`
require a `.env` file (`cp .env.example .env`) and run a one-shot
`mosquitto-init` service that writes the broker's password file from it
before `mosquitto` starts; `docker/docker-compose.yml` stays an anonymous
broker for local development only. `mosquitto-init` rewrites that file from
`.env` on every start (`mosquitto_passwd -c`), so accounts added by hand are
discarded on the next `up`; the optional `HA_MQTT_USERNAME`/`HA_MQTT_PASSWORD`
pair adds a second account for Home Assistant and is skipped when the password
is empty. `MQTT_USERNAME`/`MQTT_PASSWORD` from
`.env` are passed into the VMC and simulators and read by
`ice_edge.config.env_overrides.apply_env_overrides` (re-exported by `ice_edge.app.startup_config`), which returns an `EnvOverrides`
(a `model_copy` of `config.mqtt` with env values applied, plus the resolved
trusted-proxies list) without mutating the live `ConfigModel` — so an env-only
`MQTT_PASSWORD` can never be written back to `config.json` by a later
`save_config`. `ICE_COLDER_TRUSTED_PROXIES` is resolved the same way and
applied to the dashboard's login back-off via
`routes.backoff.set_trusted_proxies(...)`, called after
`routes.set_config_object(...)` so the env value wins.

**Backups.** `scripts/backup_data.py` (stdlib only, no `ice_*` imports; copied into both images) writes `<label>-<UTC stamp>.tar.gz` holding sqlite3 online-backup snapshots (integrity-checked) of each `--sqlite` file, the `--file` paths it is given, and a sha256 `MANIFEST.json`; `--verify ARCHIVE` re-checks one, and `--keep N` prunes per label. A `backup` compose service runs it daily: `docker-compose.yml` archives `events.db`, `access.json`, `cloud_identity.json`, `config.json` and `dispensers.toml` into `./backups` (never `session.json`, which would resurrect a stale PAY-104), and `docker/docker-compose.cloud.yml` archives `cloud.db` into `cloud_data/backups`. Shipping archives off-host is the operator's job; see `docs/backups.md`.
