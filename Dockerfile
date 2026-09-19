FROM python:3.12-slim

WORKDIR /app

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies (no dev group in production)
RUN uv sync --frozen --no-dev

# Build identity, passed by CI (see .github/workflows/ci.yml); "unknown" for
# an ad-hoc local build. Read by services/build_info.py. Placed after uv sync
# so a new commit sha never invalidates the dependency layer.
ARG VCS_REF=unknown
ARG BUILD_TIME=unknown
ENV ICE_COLDER_COMMIT=$VCS_REF \
    ICE_COLDER_BUILD_TIME=$BUILD_TIME

# Copy application code
COPY config/ config/
COPY contracts/ contracts/
COPY controller/ controller/
COPY hardware/ hardware/
COPY services/ services/
COPY simulators/ simulators/
COPY web_interface/ web_interface/
COPY main.py ./

# Create log directory
RUN mkdir -p LOGS

EXPOSE 26123

CMD ["uv", "run", "python", "main.py"]
