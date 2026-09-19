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
