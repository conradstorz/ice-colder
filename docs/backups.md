# Backups

Two compose `backup` services run `scripts/backup_data.py` every 6 h (and once at
start). Each run writes one verified `.tar.gz` and prunes old ones. Retention is N archives
taken at most once per `--min-age-hours` (20 in the compose files). The loop checks
every 6 h and writes at most once per 20 h, so a container restart does not add an
archive, a failed run is retried within 6 h, and with the compose defaults N archives
means about N days. Archives are mode 0600 (they hold secrets such as the machine's
private key); keep copies private.

| Where | Archives | Contents | Retention |
|---|---|---|---|
| Edge, root `docker-compose.yml` | `./backups/edge-<UTC stamp>.tar.gz` | `events.db`, `access.json`, `cloud_identity.json`, `config.json`, `dispensers.toml`, `MANIFEST.json` | newest 14 |
| Cloud, `docker/docker-compose.cloud.yml` | `cloud_data/backups/cloud-<UTC stamp>.tar.gz` | `cloud.db`, `MANIFEST.json` | newest 30 |

SQLite files are copied with the sqlite3 online backup API, never a file copy (a
WAL-mode database copied byte-for-byte can be inconsistent), and each snapshot must pass
`PRAGMA integrity_check` or no archive is written and the run exits non-zero.
`MANIFEST.json` records every member's size and sha256. A missing `--file` (for example
`cloud_identity.json` on a machine that never enabled the uplink) is logged and skipped.

## Telling that a backup failed

Each `backup` service has a healthcheck that fails when the newest archive is older than
26 hours. `docker ps` shows the service `unhealthy`; read `docker logs ice-colder-backup`
(edge) or `docker logs ice-cloud-backup` (cloud) for the `FAILED:` line.

## Deliberately not backed up

`data/session.json` is crash evidence for `PAY-104`. Restoring it would raise a stale
`PAY-104` on a machine that has no uncertain sale. The script only archives paths it is
told, so this is enforced by omission. Logs (`LOGS/`) are not backed up either.

## Off-host copies are your job

Archives sit next to the data they protect, so they survive a corrupted database but not
a dead SD card or droplet. Copy `backups/` (edge) and `cloud_data/backups/` (cloud) to
another machine with rsync, rclone or similar, on a schedule you control. This repo does
not do it. For the cloud, also enable DigitalOcean droplet backups.

## Verify an archive

```
docker compose exec backup python scripts/backup_data.py --verify /app/backups/edge-20261021T040000Z.tar.gz
docker compose -f docker/docker-compose.cloud.yml exec backup python scripts/backup_data.py --verify /app/cloud_data/backups/cloud-20261021T040000Z.tar.gz
```

(first line edge, second cloud)

or on any host with Python 3.12 (the script is standard library only):

```
python scripts/backup_data.py --verify edge-20261021T040000Z.tar.gz
```

It lists the members, checks each sha256 against the manifest, runs `integrity_check` on
every `.db`, and exits 0 only if all pass. Verify an archive before you rely on it, and
after copying it off-host.

## Restore

Stop the service that owns the files first so nothing writes while you restore, and
stop the `backup` service too (it would otherwise open the database mid-restore and,
as root on the cloud without the overlay, create `-shm`/`-wal` files uid 1000 cannot write). Extract
into a scratch directory, never straight over live data:

```
mkdir restore && tar -xzf edge-<stamp>.tar.gz -C restore
```

### Edge: full restore to a new SD card

1. Install Docker and clone the repo on the new card; copy `.env` back. Do not start
   `backup` until the files are in place.
2. `mkdir -p data`, then copy `access.json`, `cloud_identity.json`, `config.json`,
   `dispensers.toml` and `events.db` from `restore/` into `data/`.
3. Do not create `session.json`.
4. `docker compose up -d`.

Restoring `cloud_identity.json` keeps the machine's cloud fingerprint, so it stays
claimed. Without it the machine registers as a new unclaimed one.

### Edge: `events.db` only

1. `docker compose stop vmc backup`
2. Move the damaged `data/events.db` aside, and delete any `data/events.db-wal` and
   `data/events.db-shm`.
3. Copy `restore/events.db` to `data/events.db`.
4. `docker compose start vmc backup`

Sales after the archive's timestamp are lost; the sale journal fallback file (if any) is
replayed at start.

### Cloud: `cloud.db`

1. `docker compose -f docker/docker-compose.cloud.yml [-f docker/docker-compose.cloud.proxy.yml] stop cloud backup`
2. In `cloud_data/`, move aside `cloud.db`, `cloud.db-wal` and `cloud.db-shm`.
3. Copy `restore/cloud.db` to `cloud_data/cloud.db`. Under the proxy overlay the file must
   be owned by uid 1000 (`chown 1000:1000`).
4. Start `cloud` and `backup` again.

Report snapshots (`machines/`) and UI bundles (`ui/`) are not in the archive; machines
re-send both on their next uplink pass.
