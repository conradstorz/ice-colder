# Simulated machine image: the ice-colder image plus an embedded mosquitto broker
# and the launcher that runs the VMC and the three board simulators.
# The build context is docker/machine/, so the root .dockerignore (which excludes
# Dockerfile* and docker-compose*) does not apply here.
ARG BASE_IMAGE=ghcr.io/conradstorz/ice-colder:latest
FROM ${BASE_IMAGE}

RUN apt-get update \
    && apt-get install -y --no-install-recommends mosquitto \
    && rm -rf /var/lib/apt/lists/*

COPY launcher.py mosquitto.conf /app/machine/

CMD ["python", "/app/machine/launcher.py"]
