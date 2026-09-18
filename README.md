# ice-colder
A comprehensive vending machine controller project

## Running

**Local:**
```
uv sync
uv run python main.py
```

**Docker:**
```
docker compose up
```
Config lives at `data/config.json` (created automatically on first run).
Dashboard: http://localhost:26123

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
