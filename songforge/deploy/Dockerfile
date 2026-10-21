# songforge web app - single container, CPU only (procedural backend).
# Build from the songforge/ directory:  docker build -f deploy/Dockerfile -t songforge .
FROM python:3.11-slim

# ffmpeg = mp3/m4a in+out, libsndfile = flac/ogg, curl = healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libsndfile1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md requirements.txt ./
COPY songforge ./songforge
RUN pip install --no-cache-dir ".[server,io]" \
    && python -c "import songforge, fastapi; print('songforge', songforge.__version__)"

# run unprivileged
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data && chown app:app /data
USER app

ENV SONGFORGE_INSTANCE=/data \
    SONGFORGE_HOST=0.0.0.0 \
    SONGFORGE_PORT=8000 \
    SONGFORGE_MAX_SECONDS=60 \
    SONGFORGE_MAX_UPLOAD_MB=25 \
    SONGFORGE_TTL_HOURS=6 \
    SONGFORGE_WORKERS=1

VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["songforge", "serve", "--host", "0.0.0.0", "--port", "8000"]
