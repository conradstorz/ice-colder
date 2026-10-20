# Simulated machine

One Docker image that imitates a complete, deployed vending machine: the VMC (`ice_edge`), the three simulated boards (`ice_sims`) and a private MQTT broker, all in one container, reporting to the cloud fleet portal (`ice_cloud`) over its signed uplink. `--scale machine=N` stands up N independent machines, each with its own identity, so the cloud server can be tested against a fleet without hardware. Spec: `docs/superpowers/specs/2026-10-19-simulated-machine-container-design.md`. Step-by-step walkthrough (build, launch, claim, experiments, cleanup): `docs/running-a-virtual-fleet.md`.

## Run

From the repository root, with `ICE_CLOUD_URL` set to the cloud server's base URL (required: compose refuses to start without it, and a bare `docker run` exits 1):

```
docker compose -f docker/docker-compose.fleet.yml up -d --build --scale machine=5
docker compose -f docker/docker-compose.fleet.yml logs | grep -i "setup code"
docker stop ice_fleet-machine-3
```

Optional environment: `ICE_COLDER_MACHINE_ID` (default: the container hostname), `ICE_COLDER_REPORT_INTERVAL` (seconds, 10-3600, default 60). Each replica's dashboard is on an ephemeral host port: `docker compose -f docker/docker-compose.fleet.yml port machine 26123`.

## Claiming

Nothing claims a machine for you. Each machine logs its setup code, which is also its cloud claim code. Find the codes with the `grep` above, then enter one at the cloud server's `/claim` page. Unclaimed machines are themselves a useful test of the cloud's unclaimed views.

## What a restart keeps and what it overwrites

Data lives in an anonymous volume at `/app/data`, so identity lasts as long as that volume. On every start the launcher re-applies these fields to `config.json` and overwrites any edit to them: `machine_id`, `cloud.enabled`, `cloud.url`, `cloud.report_interval_seconds`, `mqtt.broker_host` (plus `mqtt.username` and `mqtt.password`, cleared). That is the price of env-driven config. Everything else in `config.json` is untouched, so a catalog edit made in the dashboard survives a restart. `dispensers.toml` is written only when absent. `cloud_identity.json` and `access.json` are never touched: a restart keeps the same fingerprint and the same owner.

Identity survives `docker stop`/`start`/`restart` and in-place recreates (compose reuses an anonymous volume only when it recreates an existing container in place, e.g. `up` after a config or image change, or `up --build`). It does not survive `docker compose down`: the next `up` gives every replica a fresh volume, so a new fingerprint and a new unclaimed machine, and the old volumes are orphaned. Scaling down and then up does the same to the removed replicas. `docker volume prune` removes the orphans.

`machine_id` (the label the cloud shows) defaults to the container hostname, so an in-place recreate keeps the fingerprint but changes the label; set `ICE_COLDER_MACHINE_ID` in an override file for a stable label.

`ICE_COLDER_DATA` exists only as a test and dry-run hook and is not a full relocation: it moves just `config.json` and `dispensers.toml`, while `cloud_identity.json`, `access.json`, `session.json`, the sales journal and `events.db` stay under the fixed `data/` directory of the app.

## Offline test

`docker stop ice_fleet-machine-3` stops one replica; the cloud should mark it Offline once its last report is older than three report intervals. `docker start ice_fleet-machine-3` brings it back with the same identity. Replica container names come from `docker ps`: compose v2 names them `ice_fleet-machine-<n>`, after the compose project name `ice_fleet`.

## Building against a local base image

By default the machines layer on `ghcr.io/conradstorz/ice-colder:latest`, which lacks the uplink changes until they merge. Build the base locally and point the fleet at it:

```
docker build -t ice-colder:dev .
ICE_FLEET_BASE_IMAGE=ice-colder:dev docker compose -f docker/docker-compose.fleet.yml up -d --build --scale machine=5
```

## Limits

- No scenario knobs: the sims' random customer and fault behaviour is the only scenario.
- No scripted scenario files.
- No auto-claiming of machines in the cloud.
- No fixed host-mapped dashboard ports (ephemeral only).
- No Watchtower labels.
- No published fleet image; build it locally.
- No arm64 build.
