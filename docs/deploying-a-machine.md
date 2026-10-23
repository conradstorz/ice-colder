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
   (replace `latest`; tags are listed under the `ice-colder` package on ghcr.io.
   CI publishes `sha-<7-char short sha>` per commit on `main`, e.g. `sha-4dbd516`).
3. From `docker/`:
   `docker compose --env-file ../.env -f docker-compose.prod.yml -f docker-compose.edge.yml up -d`
4. Open `http://<machine>:26123` and finish setup. The port binds all
   interfaces and the dashboard is plain HTTP, so keep the machine on a trusted LAN.
5. Backups: `docs/backups.md`. On an edge machine every `docker compose` command
   there (`exec backup ...`, `stop`, `up`) must carry
   `-f docker-compose.prod.yml -f docker-compose.edge.yml` and be run from `docker/`
   (with `--env-file ../.env`); without them it targets the root simulation stack's project.

## Update

Do it while the machine is idle. A restart mid-sale raises `PAY-104` (the
session snapshot is treated as uncertain) and needs an operator to resolve.

0. Take or verify a backup first (`docs/backups.md`, including `--verify`).
1. Edit `ICE_COLDER_IMAGE_TAG` in `.env` to the new `sha-<commit>`.
2. From `docker/`: `docker compose --env-file ../.env -f docker-compose.prod.yml -f docker-compose.edge.yml pull`
3. Same command with `up -d` in place of `pull`.

## Rollback

Set `ICE_COLDER_IMAGE_TAG` back to the previous `sha-<commit>` and run the same
`pull` and `up -d`. Data is not always backward-compatible: `events.db` carries a
schema version (`PRAGMA user_version`). An older build that meets a newer schema raises
`DATA-103` and cannot record to it (sales go to the journal fallback). So rolling back
across a release that added a migration requires restoring the pre-update backup
(`docs/backups.md`) as part of the rollback. Note the working tag before
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
