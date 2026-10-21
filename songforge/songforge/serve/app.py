"""HTTP API + web UI so other people can use the pipeline from a browser.

    pip install "songforge[server]"
    songforge serve --host 0.0.0.0 --port 8000

Endpoints
---------
GET  /                              the web app
GET  /api/health                    limits, backend, version
GET  /api/styles                    available styles
POST /api/jobs                      multipart: file, style?, prompt?  -> job
GET  /api/jobs/{id}                 status, log tail, file urls
GET  /api/jobs/{id}/audio/{kind}    kind = song | instrumental | vocals
GET  /api/jobs/{id}/report          report.json
DELETE /api/jobs/{id}               delete the job and its files
GET  /api/jobs                      recent jobs

Everything that costs money or can be abused is bounded by environment
variables (see ``ServerConfig``) - upload size, audio length, queue depth,
per-address rate limits, optional API key, file TTL.  The backend is chosen by
the operator, never by the request, so a public URL cannot be turned into a
generic remote-code-execution endpoint.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:                                                    # optional dependency
    from fastapi import (Depends, FastAPI, File, Form, Header, HTTPException, Request,
                         UploadFile)
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
except Exception as exc:                                # pragma: no cover
    raise SystemExit(
        "the web server needs FastAPI:\n  pip install 'songforge[server]'\n"
        f"(import error: {exc})")

from .. import __version__
from .manager import AUDIO_KINDS, STYLES, JobManager, RateLimiter

STATIC_DIR = Path(__file__).resolve().parent / "static"
AUDIO_FILES = {"song": "song.wav", "instrumental": "instrumental.wav", "vocals": "vocals.wav"}


@dataclass
class ServerConfig:
    instance_dir: str = "instance"
    host: str = "0.0.0.0"
    port: int = 8000
    backend: str = "procedural"
    external_command: str = ""
    max_workers: int = 1
    max_seconds: float = 60.0
    max_upload_mb: float = 25.0
    ttl_hours: float = 6.0
    rate_per_hour: int = 10
    rate_per_day: int = 40
    api_key: str = ""
    title: str = "songforge"
    notice: str = ""
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "ServerConfig":
        def num(name, default, cast=float):
            try:
                return cast(os.environ.get(name, default))
            except (TypeError, ValueError):
                return cast(default)

        return cls(
            instance_dir=os.environ.get("SONGFORGE_INSTANCE", "instance"),
            host=os.environ.get("SONGFORGE_HOST", "0.0.0.0"),
            port=num("SONGFORGE_PORT", 8000, int),
            backend=os.environ.get("SONGFORGE_BACKEND", "procedural"),
            external_command=os.environ.get("SONGFORGE_EXTERNAL_COMMAND", ""),
            max_workers=num("SONGFORGE_WORKERS", 1, int),
            max_seconds=num("SONGFORGE_MAX_SECONDS", 60.0),
            max_upload_mb=num("SONGFORGE_MAX_UPLOAD_MB", 25.0),
            ttl_hours=num("SONGFORGE_TTL_HOURS", 6.0),
            rate_per_hour=num("SONGFORGE_RATE_PER_HOUR", 10, int),
            rate_per_day=num("SONGFORGE_RATE_PER_DAY", 40, int),
            api_key=os.environ.get("SONGFORGE_API_KEY", ""),
            title=os.environ.get("SONGFORGE_TITLE", "songforge"),
            notice=os.environ.get("SONGFORGE_NOTICE", ""),
        )


def _client_ip(request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return getattr(request.client, "host", "unknown") or "unknown"


def create_app(cfg: Optional[ServerConfig] = None) -> "FastAPI":
    cfg = cfg or ServerConfig.from_env()
    manager = JobManager(
        root=cfg.instance_dir, max_workers=cfg.max_workers, max_seconds=cfg.max_seconds,
        max_upload_mb=cfg.max_upload_mb, backend=cfg.backend,
        external_command=cfg.external_command, ttl_hours=cfg.ttl_hours)
    limiter = RateLimiter(cfg.rate_per_hour, cfg.rate_per_day)

    app = FastAPI(title=cfg.title, version=__version__,
                  description="Vocal in, finished song out.")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"])

    def require_key(x_api_key: Optional[str] = Header(default=None),
                    key: Optional[str] = None) -> None:
        if not cfg.api_key:
            return
        if (x_api_key or key) != cfg.api_key:
            raise HTTPException(status_code=401, detail="missing or wrong API key")

    app.state.manager = manager
    app.state.config = cfg

    # ------------------------------------------------------------------ meta
    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "version": __version__,
            "backend": cfg.backend,
            "limits": {
                "max_seconds": cfg.max_seconds,
                "max_upload_mb": cfg.max_upload_mb,
                "styles": STYLES,
                "rate_per_hour": cfg.rate_per_hour,
                "ttl_hours": cfg.ttl_hours,
                "api_key_required": bool(cfg.api_key),
            },
            "title": cfg.title,
            "notice": cfg.notice,
        }

    @app.get("/api/styles")
    def styles():
        return {"styles": STYLES}

    # ------------------------------------------------------------------ jobs
    @app.post("/api/jobs", dependencies=[Depends(require_key)])
    async def create_job(request: Request,
                         file: UploadFile = File(...),
                         style: str = Form("auto"),
                         prompt: str = Form("")):
        ip = _client_ip(request)
        try:
            limiter.check(ip)
        except RuntimeError as exc:
            raise HTTPException(status_code=429, detail=str(exc))
        limit = int(cfg.max_upload_mb * 1024 * 1024)
        data = await file.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(status_code=413,
                                detail=f"file larger than {cfg.max_upload_mb:.0f} MB")
        try:
            job = manager.submit(file.filename or "upload.wav", data, style=style,
                                 prompt=prompt, remote=ip)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=429, detail=str(exc))
        return JSONResponse(job.to_dict(), status_code=202)

    @app.get("/api/jobs")
    def list_jobs(limit: int = 20):
        return {"jobs": manager.recent(max(1, min(limit, 100)))}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str):
        job = manager.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="no such job")
        return job.to_dict()

    @app.get("/api/jobs/{job_id}/audio/{kind}")
    def job_audio(job_id: str, kind: str):
        job = manager.get(job_id)
        if not job or job.status != "done":
            raise HTTPException(status_code=404, detail="not ready")
        if kind not in AUDIO_KINDS:
            raise HTTPException(status_code=404, detail="unknown stream")
        path = job.dir / "out" / AUDIO_FILES[kind]
        if not path.exists():
            raise HTTPException(status_code=404, detail="file missing")
        return FileResponse(str(path), media_type="audio/wav",
                            filename=f"{Path(job.filename).stem}-{kind}.wav")

    @app.get("/api/jobs/{job_id}/report")
    def job_report(job_id: str):
        job = manager.get(job_id)
        if not job or not job.report:
            raise HTTPException(status_code=404, detail="not ready")
        return job.report

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str, x_api_key: Optional[str] = Header(default=None),
                   key: Optional[str] = None):
        if cfg.api_key and (x_api_key or key) != cfg.api_key:
            raise HTTPException(status_code=401, detail="missing or wrong API key")
        if not manager.delete(job_id):
            raise HTTPException(status_code=404, detail="no such job")
        return {"deleted": job_id}

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):  # pragma: no cover
        return JSONResponse({"detail": "internal error"}, status_code=500)

    # -------------------------------------------------------------------- UI
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def index():
        html = STATIC_DIR / "index.html"
        if not html.exists():                            # pragma: no cover
            return JSONResponse({"detail": "UI not installed; API is at /api/health"})
        return FileResponse(str(html), media_type="text/html")

    return app


app = create_app()
