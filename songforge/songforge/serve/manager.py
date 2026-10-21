"""Job manager for the hosted demo: upload -> queue -> pipeline -> files.

Deliberately dependency-free (threads + a dict) so a single small container can
serve a handful of concurrent users.  Swap ``JobManager`` for Celery/RQ once you
need more than one machine - the interface (``submit`` / ``get`` / ``cleanup``)
is intentionally tiny.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..audio import read_audio
from ..pipeline import PipelineConfig, run_pipeline
from ..procedural import PATTERNS

ALLOWED_EXT = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aiff", ".aif"}
STYLES = ["auto"] + sorted(PATTERNS)
AUDIO_KINDS = {"song": "song", "instrumental": "instrumental", "vocals": "vocals"}


@dataclass
class Job:
    id: str
    filename: str
    status: str = "queued"          # queued | running | done | error
    style: str = "auto"
    prompt: str = ""
    created: float = field(default_factory=time.time)
    started: Optional[float] = None
    finished: Optional[float] = None
    progress: list = field(default_factory=list)
    error: Optional[str] = None
    report: Optional[dict] = None
    duration_s: Optional[float] = None
    remote: str = ""

    @property
    def dir(self) -> Path:
        return Path(self._root) / "jobs" / self.id

    def to_dict(self) -> dict:
        base = f"/api/jobs/{self.id}"
        files = {}
        if self.status == "done":
            files = {kind: f"{base}/audio/{kind}" for kind in AUDIO_KINDS}
        return {
            "id": self.id,
            "filename": self.filename,
            "status": self.status,
            "style": self.style,
            "prompt": self.prompt,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            "elapsed": round((self.finished or time.time()) - (self.started or self.created), 1),
            "duration_s": self.duration_s,
            "progress": self.progress[-60:],
            "error": self.error,
            "files": files,
            "report_url": f"{base}/report" if self.status == "done" else None,
            "summary": self.report.get("out") if self.report else None,
        }


class JobManager:
    def __init__(self, root: str = "instance", max_workers: int = 1,
                 max_seconds: float = 60.0, max_upload_mb: float = 25.0,
                 backend: str = "procedural", external_command: str = "",
                 ttl_hours: float = 6.0, max_queued: int = 40,
                 master: bool = True):
        self.root = Path(root)
        (self.root / "jobs").mkdir(parents=True, exist_ok=True)
        self.max_workers = max(1, int(max_workers))
        self.max_seconds = float(max_seconds)
        self.max_upload_mb = float(max_upload_mb)
        self.backend = backend
        self.external_command = external_command
        self.ttl_seconds = float(ttl_hours) * 3600.0
        self.max_queued = int(max_queued)
        self.master = master
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._stop = threading.Event()
        self._janitor = threading.Thread(target=self._janitor_loop, daemon=True)
        self._janitor.start()

    # ------------------------------------------------------------------ api
    def submit(self, filename: str, data: bytes, style: str = "auto",
               prompt: str = "", remote: str = "") -> Job:
        name = Path(filename or "upload.wav").name
        ext = Path(name).suffix.lower()
        if ext not in ALLOWED_EXT:
            raise ValueError(f"unsupported file type {ext!r}; use {', '.join(sorted(ALLOWED_EXT))}")
        if len(data) > self.max_upload_mb * 1024 * 1024:
            raise ValueError(f"file larger than {self.max_upload_mb:.0f} MB")
        if len(data) < 1024:
            raise ValueError("file is empty")
        style = style if style in STYLES else "auto"
        with self._lock:
            active = sum(1 for j in self.jobs.values() if j.status in ("queued", "running"))
            if active >= self.max_queued:
                raise RuntimeError("the queue is full right now, try again in a minute")
            if sum(1 for j in self.jobs.values()
                   if j.remote == remote and j.status in ("queued", "running")) >= 2:
                raise RuntimeError("you already have two songs in the queue")

            job = Job(id=uuid.uuid4().hex[:12], filename=name, style=style,
                      prompt=prompt[:200], remote=remote)
            job._root = str(self.root)          # type: ignore[attr-defined]
            job.dir.mkdir(parents=True, exist_ok=True)
            (job.dir / f"input{ext}").write_bytes(data)
            self.jobs[job.id] = job
            thread = threading.Thread(target=self._work, args=(job,), daemon=True)
            self._threads[job.id] = thread
            thread.start()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self.jobs.get(job_id)

    def recent(self, limit: int = 20) -> list[dict]:
        ordered = sorted(self.jobs.values(), key=lambda j: j.created, reverse=True)
        return [{"id": j.id, "filename": j.filename, "status": j.status,
                 "created": j.created, "style": j.style} for j in ordered[:limit]]

    def delete(self, job_id: str) -> bool:
        with self._lock:
            job = self.jobs.pop(job_id, None)
        if not job:
            return False
        shutil.rmtree(job.dir, ignore_errors=True)
        return True

    # -------------------------------------------------------------- worker
    def _log(self, job: Job, line: str) -> None:
        for part in str(line).splitlines():
            if part.strip():
                job.progress.append(f"[{time.strftime('%H:%M:%S')}] {part.strip()}")

    def _work(self, job: Job) -> None:
        job.status = "running"
        job.started = time.time()
        try:
            input_path = next(job.dir.glob("input.*"))
            job._root = str(self.root)  # type: ignore[attr-defined]
            probe, sr = read_audio(str(input_path))
            duration = len(probe) / sr
            job.duration_s = round(duration, 2)
            if duration > self.max_seconds + 0.5:
                self._log(job, f"input is {duration:.1f}s - trimming to the first "
                               f"{self.max_seconds:.0f}s (server limit)")
            if duration < 2.0:
                raise ValueError("that file is too short to work with (< 2 s)")
            del probe

            cfg = PipelineConfig(
                input_path=str(input_path),
                out_dir=str(job.dir / "out"),
                input_kind="vocals",           # a hosted demo should not run Demucs per request
                backend=self.backend,
                command=self.external_command or None,
                style=job.style,
                prompt=job.prompt,
                seed=int(job.id[:6], 16) % 100000,
                max_seconds=self.max_seconds,
                sr=sr,
                fmt="wav",
                master=self.master,
            )
            result = run_pipeline(cfg, progress=lambda line: self._log(job, line))
            job.report = result.report
            job.status = "done"
        except Exception as exc:                      # keep the server alive, report to user
            job.status = "error"
            job.error = str(exc)
            self._log(job, f"error: {exc}")
            traceback.print_exc()
        finally:
            job.finished = time.time()

    # ------------------------------------------------------------- janitor
    def _janitor_loop(self) -> None:
        while not self._stop.wait(600):
            cutoff = time.time() - self.ttl_seconds
            with self._lock:
                stale = [j for j in self.jobs.values()
                         if j.status in ("done", "error") and (j.finished or j.created) < cutoff]
            for job in stale:
                self.delete(job.id)

    def stop(self) -> None:
        self._stop.set()


class RateLimiter:
    """Tiny per-IP sliding window (in-memory; reset on restart)."""

    def __init__(self, per_hour: int = 10, per_day: int = 40):
        self.per_hour = per_hour
        self.per_day = per_day
        self.hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.time()
        with self._lock:
            stamps = [t for t in self.hits.get(key, []) if now - t < 86400]
            if self.per_day and len(stamps) >= self.per_day:
                raise RuntimeError("daily limit reached for this address")
            if self.per_hour and sum(1 for t in stamps if now - t < 3600) >= self.per_hour:
                raise RuntimeError("hourly limit reached for this address")
            stamps.append(now)
            self.hits[key] = stamps


__all__ = ["JobManager", "Job", "RateLimiter", "STYLES", "ALLOWED_EXT",
           "AUDIO_KINDS", "os"]
