# syntax=docker/dockerfile:1.7
# SKVM panel image.
#
#   VARIANT=slim    unprivileged, talks to no hypervisor (simulated fleet or a
#                   remote node agent) - the default `:latest` tag
#   VARIANT=docker  adds the docker CLI + runs as root so the /var/run/docker.sock
#                   mount can actually create containers - `:docker` tag
#
# The image deliberately reuses autosetup.sh (see /entrypoint.sh): the same
# bootstrap that provisions Codespaces, CodeSandbox and rigbox configures this
# container, so there is only one install path to keep working.
#
#   docker build -t skvm .                                    # slim
#   docker build --build-arg VARIANT=docker -t skvm:docker .  # hypervisor host
ARG PYTHON_VERSION=3.11
ARG VARIANT=slim
# Official image that ships just the Docker CLI (Debian-based, so its binary
# runs in the slim runtime too). Override to pin a different release.
ARG DOCKER_CLI_IMAGE=docker:27-cli

FROM ${DOCKER_CLI_IMAGE} AS dockercli

# ------------------------------------------------------------------ deps ----
FROM python:${PYTHON_VERSION}-slim AS deps
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
COPY requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip setuptools wheel \
 && pip install -r /tmp/requirements.txt \
 && pip check

# --------------------------------------------------------- runtime (slim) ---
FROM python:${PYTHON_VERSION}-slim AS runtime-slim
ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SKVM_VENV=/opt/venv \
    SKVM_INSTANCE_DIR=/data \
    PORT=8080
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 10000 skvm \
 && useradd -u 10000 -g 10000 -m -d /home/skvm -s /bin/bash skvm
COPY --from=deps /opt/venv /opt/venv
WORKDIR /app
COPY --chmod=755 docker/entrypoint.sh /entrypoint.sh
COPY . .
RUN mkdir -p /data && chown -R 10000:10000 /data /app
EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=75s --retries=4 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null || exit 1
USER 10000:10000
ENTRYPOINT ["/entrypoint.sh"]

# ------------------------------------------------------ runtime (+docker) ---
FROM runtime-slim AS runtime-docker
# The socket is group-writable on the host, so this variant stays root and is
# effectively root on the host - a lab/single-host choice, not a shared box.
# Only the docker CLI is added: the panel drives the daemon over its API
# (docker-py), it does not shell out to `docker compose`.
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
RUN docker --version
USER 0:0

# ------------------------------------------------------------------ final ---
FROM runtime-${VARIANT} AS final
ARG VARIANT
LABEL org.opencontainers.image.title="SKVM" \
      org.opencontainers.image.description="Glassmorphic self-hosted VPS control plane (Flask) - VARIANT=${VARIANT}" \
      org.opencontainers.image.source="https://github.com/SKYDO234/SKVM" \
      org.opencontainers.image.licenses="MIT" \
      io.skvm.variant="${VARIANT}"
