# Deploying a real machine

A real machine runs the edge-only stack: the MQTT broker plus the `vmc` and
`backup` services, no simulators. The image is pinned to a `sha-<commit>` tag
and is never auto-updated. (`docker-compose.yml` at the repo root is the
simulation stack and keeps `latest` plus Watchtower; it is for the sim host
only.)

## First install

1. Clone the repo on the machine and `cp .env.example .env`.
2. In `.env` set `MQTT_USERNAME`, `MQTT_PASSWORD` (12+ characters),
   `MQTT_BIND_ADDR` and `ICE_COLDER_IMAGE_TAG` to a `sha-<commit>` tag
   (tags are listed under the `ice-colder` package on ghcr.io; CI publishes one
   per commit on `main`).
3. From `docker/`:
   `docker compose --env-file ../.env -f docker-compose.prod.yml -f docker-compose.edge.yml up -d`
4. Open `http://<machine>:26123` and finish setup. Backups: `docs/backups.md`.

## Update

Do it while the machine is idle. A restart mid-sale raises `PAY-104` (the
session snapshot is treated as uncertain) and needs an operator to resolve.

1. Edit `ICE_COLDER_IMAGE_TAG` in `.env` to the new `sha-<commit>`.
2. From `docker/`: `docker compose --env-file ../.env -f docker-compose.prod.yml -f docker-compose.edge.yml pull`
3. Same command with `up -d` in place of `pull`.

## Rollback

Set `ICE_COLDER_IMAGE_TAG` back to the previous `sha-<commit>` and run the same
`pull` and `up -d`. Config and data are forward-compatible unless the release
notes of the newer version say otherwise; if they do, restore the newest
archive from `backups/` first (`docs/backups.md`). Note the working tag before
every update so there is something to return to.

## Health

`docker inspect --format '{{.State.Health.Status}}' ice-colder-vmc`

The image probes `GET /healthz` every 30 s (60 s start period, 3 retries).
`unhealthy` means the web app stopped answering although the process is alive,
so the dashboard is probably dead and the sale loop may be too. Nothing
restarts a container on `unhealthy`: look at `docker logs ice-colder-vmc` and
restart it yourself when the machine is idle. If `web.port` is changed from
26123, override `healthcheck.test` for `vmc` in the overlay.

## Why not Watchtower

Watchtower restarts a container the moment a new `latest` appears, with no
idle window and no health gate, which can interrupt a sale and leaves no
record of which version was running. It stays on the sim host, where that does
not matter.
