# Running a virtual fleet

How to launch any number of virtual vending machines, each a complete VMC with simulated
boards in one container, and point them at the cloud fleet portal. This is the walkthrough;
`docker/machine/README.md` is the reference for the image and its knobs, and the design is
`docs/superpowers/specs/2026-10-19-simulated-machine-container-design.md`.

## What one virtual machine is

One container, `ice-colder-machine`, running five processes under a small launcher:

| Process | What it does |
|---|---|
| mosquitto | a private MQTT broker on the container's own loopback; nothing outside can reach it |
| `ice_sims.vending_machine` | the simulated IM600XL vending board: buttons, sensors, dispense mechanics, random customers |
| `ice_sims.ice_maker` | the simulated ice maker monitor |
| `ice_sims.mdb_gateway` | the simulated MDB payment board |
| `ice_edge` | the real VMC: sale state machine, dashboard on port 26123, and the cloud uplink |

The launcher seeds a `config.json` with three sample products (`SAMPLE-ICE`, `SAMPLE-WATER-SM`,
`SAMPLE-WATER-LG`) wired to the board's buttons, so the simulated customers start buying
within the first minute. On first boot the VMC generates an Ed25519 key in its data volume;
the key's fingerprint is the machine's identity in the cloud. The only connection leaving the
container is HTTPS to the cloud URL you give it.

## Prerequisites

- Docker with Compose v2. On the workstation the `docker` CLI already points at the LAN
  server through the `hpz440` context, so the containers run there; the commands below are
  the same either way.
- A reachable cloud portal (`ice_cloud`). It runs on the droplet behind an HTTPS reverse proxy
  at the root of its host (no path prefix; see `packages/cloud/README.md`). The virtual
  machines need nothing but its base URL: requests are signed with each machine's own key.
- A Google account that can sign in to that portal, if you want to claim machines rather
  than only watch them register.
- A checkout of this repository at the revision whose edge code you want the fleet to run.

## 1. Build the base image

The fleet image layers mosquitto and the launcher on the edge image. The published
`ghcr.io/conradstorz/ice-colder:latest` is built from `main`; if the edge code you need (for
example the uplink registration and claim-code flow) is on a branch that has not merged, build
the base from your checkout and name it:

```
docker build -t ice-colder:dev .
```

This takes a few minutes the first time. Skip it when `latest` already has what you need.

## 2. Launch N machines

From the repository root, with the cloud's base URL in `ICE_CLOUD_URL`:

```
export ICE_CLOUD_URL=https://fleet.example.com
export ICE_FLEET_BASE_IMAGE=ice-colder:dev        # omit to use the published latest
docker compose -f docker/docker-compose.fleet.yml up -d --build --scale machine=5
```

Compose refuses to start without `ICE_CLOUD_URL`. Note that `docker compose -f docker/<file>.yml` takes `docker/` as its project directory and does **not** read the repository root `.env`, so a value there is ignored unless you also pass `--env-file .env`; exporting the variable, as above, always works. Five containers come up, named
`ice_fleet-machine-1` to `ice_fleet-machine-5`, each with its own anonymous data volume and
therefore its own identity. Every machine's `machine_id` (the label the cloud shows next to
the fingerprint) defaults to its container hostname, a 12-character id, so scaling needs no
per-machine configuration.

Optional environment, applied to every replica:

| Variable | Meaning | Default |
|---|---|---|
| `ICE_COLDER_REPORT_INTERVAL` | seconds between reports to the cloud, 10 to 3600 | 60 |
| `ICE_FLEET_BASE_IMAGE` | the edge image to layer on | `ghcr.io/conradstorz/ice-colder:latest` |

A short interval (say 15) makes the cloud's online/offline transitions quicker to watch.

## 3. Check that they booted

```
docker compose -f docker/docker-compose.fleet.yml ps
docker compose -f docker/docker-compose.fleet.yml logs --tail 60 machine
```

Within about twenty seconds each container's log should show, in order: mosquitto
`Opening ipv4 listen socket on port 1883`; `[vending]`, `[ice_maker]` and `[mdb]`
`Connected to 127.0.0.1:1883`; the edge's `Entering main event loop`; and
`Dashboard is in setup mode`, followed by the setup code. A line such as `[launcher] vending
exited with code 1` means a child process died; the launcher brings it back after five
seconds, but the cause is worth reading.

If the cloud is unreachable, the uplink logs a warning per attempt
(`Cloud uplink POST ... failed`) and the machine keeps running and selling. Nothing in the
launcher depends on the cloud.

## 4. Find each machine's claim code and fingerprint

A machine appears in the cloud as soon as it registers, but it belongs to nobody until it is
claimed with the code minted at the machine. For a fresh machine that code is the setup code,
the same one that unlocks the dashboard's setup wizard.

Both are in the log. The edge logs the fingerprint once at uplink start (`Cloud uplink
enabled, reporting to ...; machine fingerprint xxxx-xxxx-xxxx-xxxx`) and the setup code when
the dashboard enters setup mode, so one grep collects both for every replica:

```
docker compose -f docker/docker-compose.fleet.yml logs | grep -iE "setup code|fingerprint"
```

The fingerprint is also on the machine's own `/setup` page. Each replica's dashboard is
published on an ephemeral host port; find it with the replica's index:

```
docker compose -f docker/docker-compose.fleet.yml port --index 3 machine 26123
```

Open `http://<docker host>:<that port>/setup` (the Docker host is the LAN server when using
the `hpz440` context). The page shows the short fingerprint, `xxxx-xxxx-xxxx-xxxx`, and asks
for the setup code; you do not need to complete the wizard to claim the machine. A platform
admin can also read every registered fingerprint on the cloud's `/admin` page, claimed or not.

## 5. Claim machines in the cloud

Sign in to the portal with Google, open `/claim`, and enter a machine's short fingerprint and
its 8-digit code. The machine is now yours and appears under `/fleet`; select it to see its
home, health and faults pages rendered from its latest report. Repeat for as many machines as
you want to own. Leaving some unclaimed is useful too: it exercises the cloud's unclaimed
views and the admin page.

A claim code lasts seven days. Once a machine's dashboard setup wizard is completed at the
machine, the setup code is consumed; mint a fresh claim code afterwards from the dashboard's
Users, Ownership page ("Link to cloud").

## 6. Run the experiments the fleet exists for

**Offline detection.** Stop one replica and watch the cloud mark it Offline once its last
report is older than three report intervals, then bring it back with the same identity:

```
docker stop ice_fleet-machine-3
docker start ice_fleet-machine-3
```

**Scale up.** Add machines without touching the existing ones:

```
docker compose -f docker/docker-compose.fleet.yml up -d --scale machine=8
```

The three new replicas register as new, unclaimed machines.

**Deploy a new edge build to the fleet.** Rebuild the base, then recreate in place:

```
docker build -t ice-colder:dev .
docker compose -f docker/docker-compose.fleet.yml up -d --build --scale machine=8
```

An in-place recreate keeps each replica's data volume, so fingerprints and claims survive.
The hostname changes with the new container, so the `machine_id` label the cloud shows
changes too; identity is the fingerprint, so nothing is lost.

**Many fleets.** Use a different compose project name to run a second, independent fleet
against another cloud (or the same one):

```
ICE_CLOUD_URL=https://staging.example.com docker compose -p ice_fleet_staging \
  -f docker/docker-compose.fleet.yml up -d --scale machine=3
```

## 7. Naming a specific machine

The hostname default is what makes `--scale` zero-config, so the fleet file sets no
`machine_id`. To give one machine a stable, readable label, run it as its own compose
project with an override file:

```yaml
# fleet-named.yml
services:
  machine:
    environment:
      - ICE_COLDER_MACHINE_ID=kiosk-lobby
```

```
docker compose -p kiosk_lobby -f docker/docker-compose.fleet.yml -f fleet-named.yml up -d
```

Scaling such a project above one replica would give every replica the same label, which the
cloud tolerates (identity is the fingerprint) but you will not enjoy.

## 8. Stop and clean up

Stopping keeps identities; removing containers loses them:

| Command | Effect on identity |
|---|---|
| `docker stop` / `docker start` / `docker restart` a replica | kept |
| `docker compose ... up -d --build` (in-place recreate) | kept |
| `docker compose ... up -d --scale machine=3` (scale down from 5) | replicas 4 and 5 lose theirs |
| `docker compose -f docker/docker-compose.fleet.yml down` | every replica loses its identity on the next `up`; the old volumes are orphaned |

After a `down`, the next `up` creates fresh volumes, so the cloud sees five brand-new
unclaimed machines and the five you claimed go permanently Offline. Clean up the orphaned
volumes with `docker volume prune`, and unbind the dead machines from the cloud's `/admin`
page if their rows are in the way.

To stop the fleet without losing anything:

```
docker compose -f docker/docker-compose.fleet.yml stop
docker compose -f docker/docker-compose.fleet.yml start
```

## 9. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| compose exits immediately: `set ICE_CLOUD_URL to the cloud server's base URL` | export `ICE_CLOUD_URL` before `up`, or pass `--env-file .env`; the `-f docker/...` form does not read the root `.env` by itself |
| a container exits with `[launcher] error: ICE_COLDER_REPORT_INTERVAL ...` | the value must be an integer from 10 to 3600 |
| `pull access denied for ice-colder` during build | `ICE_FLEET_BASE_IMAGE` names an image that does not exist on the Docker host; build it first (step 1) |
| machines boot but never appear in the cloud | the uplink warnings in the log say why: wrong URL, TLS, a path prefix at the proxy (signatures then fail with 401), or the base image predates the registration flow |
| `Cloud uplink ... 401` after the machine was visible | the cloud forgot the machine (database reset or unbind); the machine re-registers on its own as a new unclaimed entry |
| the setup code in the log is refused at `/claim` | it was already consumed by completing the setup wizard, or it is older than seven days; mint a new link code from the dashboard |
| the dashboard port changes after a recreate | ephemeral ports are reassigned; ask `docker compose port --index N machine 26123` again |
| a child keeps restarting (`[launcher] edge exited with code 1` every five seconds) | read the lines above it; a bad `config.json` edit made in the dashboard is the usual cause, and `docker compose down` followed by `up` gives a clean (new-identity) machine |

## What the fleet does not do

The simulated customers and faults are random; there is no scenario file and no per-machine
traffic profile. Nothing claims a machine for you. Dashboard ports are ephemeral only. The
fleet image is built locally, never published, and only for amd64.
