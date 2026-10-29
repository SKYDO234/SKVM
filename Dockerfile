# SKVM PANEL — container image (panel itself).
# The panel provisions VPS containers through the runtime endpoint it is
# given (host Docker socket, DinD service, or Podman socket) — see
# docker-compose.yml (socket mode) and docker-compose.dind.yml.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SKVM_DATA_DIR=/data \
    SKVM_DB=/data/skvm.db \
    SKVM_PORT=5555

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates iproute2 procps \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .
RUN chmod +x run.sh docker-entrypoint.sh \
    && mkdir -p /data \
    && python -m compileall -q app.py job_runner.py node.py core/

EXPOSE 5555
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD curl -fsS http://127.0.0.1:5555/healthz || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
