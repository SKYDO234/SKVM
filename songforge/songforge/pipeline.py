"""End-to-end orchestration: song file (or vocal) -> finished song."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .analyze import analyze_vocal
from .audio import (DEFAULT_SR, fade, pad_to, peak_db, read_audio, to_stereo,
                    write_audio, write_json)
from .backends import create_backend
from .mix import MixSettings, mix_song, reference_report
from .render import describe, render_melody
from .separate import demucs_available, separate_song

Progress = Callable[[str], None]


@dataclass
class PipelineConfig:
    input_path: str
    out_dir: str = "out"
    input_kind: str = "song"          # song | vocals
    backend: str = "procedural"
    model: Optional[str] = None
    command: Optional[str] = None
    style: str = "auto"
    prompt: str = ""
    seed: int = 0
    max_seconds: Optional[float] = None
    duration: Optional[float] = None
    sr: int = DEFAULT_SR
    fmt: str = "wav"
    device: Optional[str] = None
    keep_stems: bool = True
    stems_dir: Optional[str] = None
    mix: MixSettings = field(default_factory=MixSettings)
    master: bool = True
    demucs_model: str = "htdemucs_ft"


@dataclass
class PipelineResult:
    out_dir: str
    vocals_path: str
    music_path: str
    master_path: str
    melody_path: Optional[str]
    conditioning_path: str
    report_path: str
    report: dict


def _ext(fmt: str) -> str:
    return "." + fmt.lstrip(".")


def run_pipeline(cfg: PipelineConfig, progress: Progress = print) -> PipelineResult:
    t0 = time.time()
    out_dir = Path(cfg.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = _ext(cfg.fmt)
    rng_seed = int(cfg.seed) or 0
    np.random.seed(rng_seed % (2 ** 32))

    # ------------------------------------------------------------------ 1/5
    progress("[1/5] loading input")
    audio, file_sr = read_audio(cfg.input_path, sr=cfg.sr)
    if cfg.max_seconds:
        audio = audio[: int(cfg.max_seconds * cfg.sr)]
    if cfg.duration:
        audio = audio[: int(cfg.duration * cfg.sr)]
    duration = len(audio) / cfg.sr
    progress(f"      {Path(cfg.input_path).name}: {duration:.1f}s @ {cfg.sr} Hz, "
             f"{audio.shape[1]} ch")

    # ------------------------------------------------------------------ 2/5
    vocal = None
    accompaniment = None
    if cfg.input_kind == "song":
        stems_dir = cfg.stems_dir or str(out_dir / "stems")
        progress("[2/5] separating vocals from the mix (Demucs)")
        if not demucs_available():
            raise SystemExit(
                "Demucs is not installed, so a full song cannot be split into stems.\n"
                "  pip install demucs           # or: pip install 'songforge[separate]'\n"
                "or pass a clean vocal stem instead:\n"
                f"  songforge run {cfg.input_path} --input-kind vocals")
        stems = separate_song(cfg.input_path, stems_dir,
                              model=cfg.demucs_model, device=cfg.device or "cpu",
                              two_stems="vocals", progress=progress)
        vocal, vsr = read_audio(stems["vocals"], sr=cfg.sr)
        if "accompaniment" in stems:
            accompaniment, _ = read_audio(stems["accompaniment"], sr=cfg.sr)
        vocal = vocal[: int(duration * cfg.sr)]
        if not cfg.keep_stems and not cfg.stems_dir:
            progress("      (keeping stems; they are needed for the report)")
    else:
        progress("[2/5] input is already a vocal stem - skipping separation")
        vocal = audio

    vocal = to_stereo(vocal)
    vocal_len = len(vocal) / cfg.sr
    progress(f"      vocal: {vocal_len:.1f}s, peak {peak_db(vocal):.1f} dBFS")

    # ------------------------------------------------------------------ 3/5
    progress("[3/5] analysing vocal (tempo, key, melody, form)")
    cond = analyze_vocal(vocal, cfg.sr, prompt=cfg.prompt, seed=rng_seed)
    if cfg.duration:
        cond.duration = min(cond.duration, cfg.duration)
        cond.sections = [s for s in cond.sections if s.start < cond.duration]
    progress("      " + describe(cond))
    cond_path = out_dir / "conditioning.json"
    write_json(str(cond_path), cond.to_dict())
    melody_guide = render_melody(cond, cfg.sr, cond.duration)
    melody_path = out_dir / f"melody_guide{ext}"
    write_audio(str(melody_path), melody_guide, cfg.sr)

    # ------------------------------------------------------------------ 4/5
    progress(f"[4/5] generating instrumental with backend '{cfg.backend}'")
    backend = create_backend(cfg.backend, model=cfg.model, command=cfg.command,
                             device=cfg.device, style=cfg.style, seed=rng_seed)
    music = backend.generate(cond, vocals=vocal, duration=cond.duration)
    music = to_stereo(np.asarray(music, dtype=np.float32))
    music_len = int(round(cond.duration * cfg.sr))
    music = pad_to(music, music_len + int(1.5 * cfg.sr), tail_fade_ms=60.0, sr=cfg.sr)
    music = fade(music, cfg.sr, 8.0, 60.0)
    progress(f"      instrumental: {len(music) / cfg.sr:.1f}s")

    # ------------------------------------------------------------------ 5/5
    progress("[5/5] mixing vocal + instrumental and mastering")
    master, report = mix_song(vocal, music, cfg.sr, settings=cfg.mix, master=cfg.master)
    progress("\n" + reference_report(vocal, music, master, cfg.sr))

    vocals_path = out_dir / f"vocals{ext}"
    music_path = out_dir / f"instrumental{ext}"
    master_path = out_dir / f"song{ext}"
    write_audio(str(vocals_path), vocal, cfg.sr)
    write_audio(str(music_path), music, cfg.sr)
    write_audio(str(master_path), master, cfg.sr)

    report.update({
        "backend": cfg.backend,
        "backend_model": cfg.model or "",
        "input": str(Path(cfg.input_path).resolve()),
        "input_kind": cfg.input_kind,
        "seed": rng_seed,
        "sr": cfg.sr,
        "conditioning": cond.to_dict(),
        "files": {
            "song": str(master_path), "instrumental": str(music_path),
            "vocals": str(vocals_path), "melody_guide": str(melody_path),
            "conditioning": str(cond_path),
        },
        "elapsed_s": round(time.time() - t0, 2),
    })
    report_path = out_dir / "report.json"
    write_json(str(report_path), report)
    progress(f"\ndone in {report['elapsed_s']:.1f}s -> {master_path}")
    return PipelineResult(
        out_dir=str(out_dir), vocals_path=str(vocals_path), music_path=str(music_path),
        master_path=str(master_path), melody_path=str(melody_path),
        conditioning_path=str(cond_path), report_path=str(report_path), report=report)


def summarize_report(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        rep = json.load(fh)
    c = rep.get("conditioning", {})
    lines = [
        f"backend      : {rep.get('backend')}",
        f"input        : {rep.get('input')}",
        f"tempo / key  : {c.get('bpm', 0):.1f} BPM, {c.get('key_name', '?')}",
        f"melody       : {len(c.get('melody', []))} notes, "
        f"{len(c.get('sections', []))} sections",
        f"master       : {rep['out']['lufs']:.1f} LUFS, peak {rep['out']['peak_db']:.1f} dBFS, "
        f"{rep['out']['duration_s']:.1f}s",
        f"files        : {os.path.basename(rep['files']['song'])}, "
        f"{os.path.basename(rep['files']['instrumental'])}, "
        f"{os.path.basename(rep['files']['vocals'])}",
    ]
    return "\n".join(lines)


__all__ = ["PipelineConfig", "PipelineResult", "run_pipeline", "summarize_report",
           "MixSettings"]
