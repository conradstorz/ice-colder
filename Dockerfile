FROM python:3.12-slim

WORKDIR /app

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first for layer caching: the lockfile, the workspace
# root and every member manifest (the dependency layer only changes when one of
# these does).
COPY pyproject.toml uv.lock ./
COPY packages/contracts/pyproject.toml packages/contracts/pyproject.toml
COPY packages/common/pyproject.toml packages/common/pyproject.toml
COPY packages/ui/pyproject.toml packages/ui/pyproject.toml
COPY packages/edge/pyproject.toml packages/edge/pyproject.toml
COPY packages/cloud/pyproject.toml packages/cloud/pyproject.toml
COPY packages/sims/pyproject.toml packages/sims/pyproject.toml

# Install third-party dependencies only (no dev group in production). The env
# vars make every later `uv run` honour the same choice instead of re-syncing
# at container start. The image holds ice-edge and ice-sims, never ice-cloud.
ENV UV_FROZEN=1 \
    UV_NO_DEV=1
RUN uv sync --no-install-workspace --package ice-edge
RUN uv sync --no-install-workspace --inexact --package ice-sims

# Build identity, passed by CI (see .github/workflows/ci.yml); "unknown" for
# an ad-hoc local build. Read by packages/common/src/ice_common/build_info.py. Placed after uv sync
# so a new commit sha never invalidates the dependency layer.
ARG VCS_REF=unknown
ARG BUILD_TIME=unknown
ENV ICE_COLDER_COMMIT=$VCS_REF \
    ICE_COLDER_BUILD_TIME=$BUILD_TIME

# Copy application code
COPY packages/ packages/
COPY controller/ controller/
COPY services/ services/
COPY simulators/ simulators/
COPY web_interface/ web_interface/
COPY main.py ./

# Install the workspace members themselves, then make the project venv the
# default interpreter: bare `python` must be /app/.venv's, not the slim
# image's system Python. UV_NO_SYNC stops `uv run` re-syncing at start.
RUN uv sync --package ice-edge
RUN uv sync --inexact --package ice-sims
ENV UV_NO_SYNC=1 \
    PATH="/app/.venv/bin:$PATH"

# Create log directory
RUN mkdir -p LOGS

EXPOSE 26123

CMD ["uv", "run", "python", "main.py"]
