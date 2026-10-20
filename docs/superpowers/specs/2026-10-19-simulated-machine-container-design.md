# Simulated machine container — design

Date: 2026-10-19. Status: approved in brainstorming. Branch `fleet/simulated-machine`,
based on `cloud/identity-tenancy` (the uplink registration and claim-code flow it relies
on live there, not yet on `main`).

## 1. Goal

One Docker image that imitates a complete, deployed vending machine: the VMC
(`ice_edge`), the three simulated boards (`ice_sims`), and a private MQTT broker, all in
one container, reporting to the cloud fleet portal (`ice_cloud`) over its signed uplink.
`docker compose up --scale machine=N` then stands up N independent machines, each with
its own identity, so the cloud server under construction can be tested against a fleet
without any hardware.

Non-goals, each explicitly out of scope: scenario knobs (named traffic or fault
profiles), scripted scenario files, auto-claiming machines in the cloud, fixed
host-mapped dashboard ports, Watchtower labels, a published fleet image, an arm64 build.
The sims' existing random customer and fault behaviour is the only scenario; variety
across the fleet comes from N independent random streams, and offline detection is
tested by stopping a container.

## 2. Decisions

| # | Question | Decision |
|---|---|---|
| D1 | Unit of deployment | One container per machine: VMC, three sims and a broker under one supervisor. Not the production one-process-per-board shape; this is a test fixture. |
| D2 | Per-instance identity and config | Env-driven. The launcher writes `config.json` into an anonymous per-container volume at `/app/data`; the edge generates its Ed25519 key there. `machine_id` defaults to the container hostname, so `--scale` needs no per-instance YAML. Identity lasts as long as the anonymous volume. |
| D3 | Broker and cloud | The cloud is remote (a droplet), given by URL. Mosquitto is embedded in the image, anonymous, bound to loopback inside the container. The container's only outbound connection is the cloud URL. |
| D4 | Scenarios | None beyond what the sims already do. |
| D5 | Claiming | No extra mechanism. Each machine logs its setup code (which is also its cloud claim code); the operator claims from `docker compose logs`. Unclaimed machines are themselves a useful test of the cloud's unclaimed views. |
| D6 | Supervisor | A stdlib-only Python launcher (`asyncio` subprocesses) run on the image's own venv. Not supervisord, not a shell `wait -n` loop. |

## 3. Files

All new, nothing existing edited except one CI step (section 7), so the work stays
isolated from other branches in flight.

```
docker/machine/
  Dockerfile                  FROM ${BASE_IMAGE:-ghcr.io/conradstorz/ice-colder:latest};
                              apt-get install mosquitto; COPY launcher.py mosquitto.conf;
                              CMD ["python", "/app/machine/launcher.py"]
  mosquitto.conf              listener 1883 127.0.0.1; allow_anonymous true; persistence false
  launcher.py                 the supervisor (section 4)
  README.md                   how to run N machines against a remote cloud; how to read claim codes
docker/docker-compose.fleet.yml
  one service `machine` (section 5)
tests/docker/__init__.py
tests/docker/test_machine_launcher.py   unit tests for the seeding half of the launcher (section 6)
docs/superpowers/specs/2026-10-19-simulated-machine-container-design.md   this file
```

The fleet Dockerfile's build context is `docker/machine/` itself, so the root
`.dockerignore` (which excludes every `Dockerfile*` and `docker-compose*`) does not
apply. The launcher is copied to `/app/machine/launcher.py`, outside `packages/`, so the
package-isolation rules (`lint-imports`, `check_package_isolation.py`) never see it.

## 4. Launcher (`docker/machine/launcher.py`)

Runs as PID 1's child under the image CMD. Stdlib `asyncio`, `os`, `signal`, `socket`,
`json`, `pathlib`, plus `ice_common.config_model.ConfigModel` and
`ice_edge.config.config_store.save_config` from the base image for seeding. Every child
inherits stdout and stderr, so `docker logs` shows the broker, the three sims, the edge
and the setup code in one stream.

### 4.1 Environment

| Variable | Required | Meaning |
|---|---|---|
| `ICE_CLOUD_URL` | yes | Base URL of the cloud server. Missing or empty: exit 1 with a one-line message. Never a silent machine with `cloud.enabled=false`. |
| `ICE_COLDER_MACHINE_ID` | no | `config.machine_id`. Default: the container hostname (`socket.gethostname()`). |
| `ICE_COLDER_REPORT_INTERVAL` | no | `config.cloud.report_interval_seconds`, 10–3600. Default 60. Out of range: exit 1. |
| `ICE_COLDER_DATA` | no | Data directory. Default `/app/data`. Exists so the unit tests and a local dry run can point at a temp dir. It relocates only `config.json` and `dispensers.toml`; `ice_common.paths.DATA_DIR` is fixed, so `cloud_identity.json`, `access.json`, `session.json`, the sales journal and `events.db` stay there regardless. |

The launcher sets, for its children, `ICE_COLDER_CONFIG=<data>/config.json` and
`ICE_COLDER_DISPENSERS=<data>/dispensers.toml`. It sets no `MQTT_*` variables: the
broker host comes from the seeded config (`mqtt.broker_host=127.0.0.1`) and the sims are
passed `--broker 127.0.0.1` explicitly, with no credentials, matching the anonymous
loopback listener.

### 4.2 Seed (idempotent, every start)

`seed(data_dir, env) -> ConfigModel`, a pure function over the directory and a mapping,
so it is unit-testable without Docker.

1. If `<data>/config.json` is a directory, or exists and fails to parse: exit 1 with a
   clear message (the edge would exit anyway; failing first keeps the log readable).
2. If `<data>/config.json` is missing: build `ConfigModel()` defaults and set
   `physical.products` to the three products the generated example dispensers file names,
   so the fleet actually sells from the first minute:

   | sku | slot | kind | button | price |
   |---|---|---|---|---|
   | `SAMPLE-ICE` | 0 | `ice` | `button_ice` | 3.00 |
   | `SAMPLE-WATER-SM` | 1 | `water` | `button_water_1gal` | 1.00 |
   | `SAMPLE-WATER-LG` | 2 | `water` | `button_water_5gal` | 2.00 |

   Names are the sku title-cased; every other field keeps its default.
3. If `<data>/dispensers.toml` is missing: write the output of
   `python -m ice_edge.vend.dispensers --example` (the schema-generated reference the
   repo commits as `dispensers.example.toml`; the image holds the generator, not the
   file, and the two are byte-identical by `tests/edge/vend/test_dispensers_example.py`).
4. Whether or not the file existed, re-apply the env-derived fields and save with
   `save_config` (atomic, keeps `config.json.bak`): `machine_id`, `cloud.enabled=True`,
   `cloud.url`, `cloud.report_interval_seconds`, `mqtt.broker_host="127.0.0.1"`,
   `mqtt.username=None`, `mqtt.password=None`. Nothing else in the file is touched, so a
   catalog edit made in the dashboard survives a restart while an edit of those fields
   is overwritten. This is documented in the README as the price of env-driven config.
5. `cloud_identity.json` and `access.json` are never read or written by the launcher.
   A restart keeps the same fingerprint and the same owner.

### 4.3 Start order and supervision

1. Start `mosquitto -c /app/machine/mosquitto.conf`. Poll a TCP connect to
   `127.0.0.1:1883` every 0.25 s; after 10 s without success, exit 1.
2. Start the four application children, each `python -m <module>` with the env of 4.1:
   `ice_sims.vending_machine --broker 127.0.0.1`, `ice_sims.ice_maker --broker 127.0.0.1`,
   `ice_sims.mdb_gateway --broker 127.0.0.1`, `ice_edge`.
3. Supervise: when an application child exits, log its name and exit code, wait 5 s,
   restart it. When mosquitto exits, that is fatal: SIGTERM the rest, exit 1, and let
   Docker's `restart: unless-stopped` recycle the whole machine (the edge and sims would
   otherwise sit in reconnect loops against a broker that lost every retained document).
4. On SIGTERM or SIGINT: SIGTERM every child, wait up to 10 s for all of them, SIGKILL
   any survivor, exit 0. Docker's default `stop_grace_period` is 10 s, so the compose
   file sets it to 15 s to leave room.

The launcher logs its own lines with a `[launcher]` prefix via `print(..., flush=True)`;
its supervision loop has no loguru dependency of its own and logs with print. The seeding
step imports `ice_edge` (which pulls in loguru transitively), so a broken edge install fails
at seed time with a visible traceback before anything is started.

## 5. Compose (`docker/docker-compose.fleet.yml`)

```yaml
name: ice_fleet
services:
  machine:
    build:
      context: machine
      args:
        BASE_IMAGE: ${ICE_FLEET_BASE_IMAGE:-ghcr.io/conradstorz/ice-colder:latest}
    image: ice-colder-machine:local
    environment:
      - ICE_CLOUD_URL=${ICE_CLOUD_URL:?set ICE_CLOUD_URL to the cloud server's base URL}
      - ICE_COLDER_REPORT_INTERVAL=${ICE_COLDER_REPORT_INTERVAL:-60}
    ports:
      - "26123"          # ephemeral host port per replica; `docker compose port machine 26123`
    volumes:
      - /app/data         # anonymous: one identity per container, kept across restarts
    stop_grace_period: 15s
    restart: unless-stopped
```

No `container_name` (it would break `--scale`), no `ICE_COLDER_MACHINE_ID` (the hostname
default is what makes scaling zero-config; set it in an override file for a named
machine). Run from the repository root:

```
docker compose -f docker/docker-compose.fleet.yml up -d --build --scale machine=5
docker compose -f docker/docker-compose.fleet.yml logs | grep -i "setup code"
docker stop ice_fleet-machine-3   # offline test
```

Replica container names come from `docker ps`: compose v2 names them `ice_fleet-machine-<n>`, after the compose project name `ice_fleet`.

`ICE_FLEET_BASE_IMAGE` lets a developer build the base image locally first
(`docker build -t ice-colder:dev .`) and point the fleet at it instead of pulling
`latest`, which on this branch would lack the uplink changes until they merge.

## 6. Tests (`tests/docker/test_machine_launcher.py`)

The launcher is loaded by file path with `importlib.util.spec_from_file_location`, so
nothing in `packages/` imports it. Tests cover the seeding half only; supervision is
covered by the CI smoke test. Each test uses `tmp_path` as the data directory.

- Fresh directory: `seed` creates `config.json` whose three products, together with the
  generated `dispensers.toml`, pass `ice_edge.vend.dispensers.validate_document` with no
  errors; `cloud.enabled` is true, `cloud.url` and `machine_id` match the env, and
  `mqtt.broker_host` is `127.0.0.1`.
- Second run: an existing `cloud_identity.json` and `access.json` are byte-identical
  afterwards, a product added to the catalog between runs survives, and the five
  env-derived fields are re-applied over a hand-edited value.
- Hostname fallback: with no `ICE_COLDER_MACHINE_ID`, `machine_id` equals the injected
  hostname (the function takes `hostname` as a parameter rather than calling
  `socket.gethostname()` itself).
- Missing `ICE_CLOUD_URL`: raises the launcher's `LauncherError`, whose message names the
  variable. The `main()` wrapper turns that into exit 1.
- Out-of-range `ICE_COLDER_REPORT_INTERVAL` and a `config.json` that is a directory:
  `LauncherError`.
- `dispensers.toml` is only written when absent: a pre-existing file with custom content
  is left alone.

These tests import `ice_common` and `ice_edge` directly, so they live under
`tests/docker/`, not `tests/sims/`, and the test-isolation `ast` walk is unaffected.

## 7. CI

One step appended to the existing `image` job in `.github/workflows/ci.yml`, after the
edge smoke test and reusing its `ice-colder:smoke` image as `BASE_IMAGE`:

1. `docker build --build-arg BASE_IMAGE=ice-colder:smoke -t ice-colder-machine:smoke docker/machine`.
2. `docker run -d --name machine-smoke -e ICE_CLOUD_URL=http://127.0.0.1:9 ice-colder-machine:smoke`.
3. Poll `docker logs machine-smoke` for up to 60 s until all of these appear, then
   `docker rm -f` it; any missing line after the deadline fails the step and prints the
   full log:
   - mosquitto's listener line (`Opening ipv4 listen socket on port 1883`),
   - `[vending] Connected to 127.0.0.1:1883`, `[ice_maker] Connected to ...`,
     `[mdb] Connected to ...` (the sims' own connect lines; the exact subsystem names
     are taken from `ice_sims` at implementation time),
   - `Entering main event loop with web server, MQTT client, and health monitor`,
   - `Dashboard is in setup mode`.

Port 9 is the discard port, so the uplink's registration fails fast and loudly without
anything listening, which is the behaviour the smoke test wants to see survive. This is
the only edit to a file another branch may touch; it is the last task and its own
commit.

## 8. Error handling summary

| Condition | Behaviour |
|---|---|
| `ICE_CLOUD_URL` unset | exit 1 before anything starts |
| `ICE_COLDER_REPORT_INTERVAL` out of 10–3600 | exit 1 before anything starts |
| `config.json` is a directory or unparsable | exit 1 before anything starts |
| mosquitto never listens within 10 s | exit 1 |
| mosquitto exits later | SIGTERM the rest, exit 1; Docker restarts the machine |
| an app child exits | restart after 5 s, forever |
| cloud unreachable | nothing in the launcher; the edge's uplink logs a warning per failed report and never blocks a sale |
| SIGTERM | graceful stop of every child, exit 0 |
