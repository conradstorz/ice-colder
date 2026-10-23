# Cloud fleet portal (identity and tenancy: Google sign-in, signed machine API,
# SQLite). Needs ICE_CLOUD_SESSION_KEY at runtime; see packages/cloud/README.md
# and docker/docker-compose.cloud.yml. Build from the repository root:
#   docker build -f packages/cloud/Dockerfile .
# BuildKit reads packages/cloud/Dockerfile.dockerignore (next to this file) in
# preference to the root .dockerignore, which excludes packages/cloud for the
# edge image.
# Base image floats on its major tag; pin by digest when a deployment needs reproducibility.
FROM python:3.12-slim

WORKDIR /app

# uv pinned to the version that wrote uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.7.13 /uv /usr/local/bin/uv

# Dependency layer: the lockfile, the workspace root and every member manifest
# (all members must be present for the workspace to resolve against uv.lock).
COPY pyproject.toml uv.lock ./
COPY packages/contracts/pyproject.toml packages/contracts/pyproject.toml
COPY packages/common/pyproject.toml packages/common/pyproject.toml
COPY packages/ui/pyproject.toml packages/ui/pyproject.toml
COPY packages/edge/pyproject.toml packages/edge/pyproject.toml
COPY packages/cloud/pyproject.toml packages/cloud/pyproject.toml
COPY packages/sims/pyproject.toml packages/sims/pyproject.toml

ENV UV_FROZEN=1 \
    UV_NO_DEV=1
RUN uv sync --no-install-workspace --package ice-cloud

# Only the packages ice-cloud depends on; never ice_edge or ice_sims.
COPY packages/contracts packages/contracts
COPY packages/common packages/common
COPY packages/ui packages/ui
COPY packages/cloud packages/cloud
# Backup tool run by the compose `backup` service (docs/backups.md).
COPY scripts/backup_data.py scripts/backup_data.py

RUN uv sync --package ice-cloud
ENV UV_NO_SYNC=1 \
    PATH="/app/.venv/bin:$PATH" \
    ICE_CLOUD_DATA=/app/cloud_data

RUN mkdir -p /app/cloud_data

EXPOSE 26124

CMD ["uv", "run", "python", "-m", "ice_cloud"]
