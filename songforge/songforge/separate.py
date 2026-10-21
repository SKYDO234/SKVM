"""Vocal / instrumental separation via Demucs (optional dependency).

If you already have a clean vocal stem, skip this stage entirely.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

STEMS = ("vocals", "drums", "bass", "other")


def demucs_available() -> bool:
    return importlib.util.find_spec("demucs") is not None or shutil.which("demucs") is not None


def demucs_version() -> Optional[str]:
    if importlib.util.find_spec("demucs") is None:
        return None
    try:
        from demucs import __version__  # type: ignore
        return str(__version__)
    except Exception:
        return "unknown"


def separate_song(input_path: str, out_dir: str, model: str = "htdemucs_ft",
                  device: str = "cpu", two_stems: Optional[str] = "vocals",
                  shifts: int = 1, jobs: int = 1,
                  progress: Callable[[str], None] = print) -> dict:
    """Run Demucs on ``input_path``; return a mapping stem -> wav path.

    ``two_stems='vocals'`` produces ``vocals`` + ``accompaniment`` (fast, what the
    pipeline needs).  ``two_stems=None`` gives all four stems (vocals, drums,
    bass, other) which is what you want when building a training set.
    """
    if not demucs_available():
        raise RuntimeError(
            "Demucs is not installed.  Install it with `pip install demucs` "
            "(or run the pipeline with an already-isolated vocal stem: "
            "`--input-kind vocals`).")
    out_path = Path(out_dir).resolve()
    out_path.mkdir(parents=True, exist_ok=True)
    cmd = [shutil.which("demucs") or "python", "-m", "demucs",
           "-n", model, "-d", device, "--shifts", str(shifts),
           "-j", str(jobs), "-o", str(out_path)]
    if cmd[0] != "python":
        cmd = [cmd[0], "-n", model, "-d", device, "--shifts", str(shifts),
               "-j", str(jobs), "-o", str(out_path)]
    if two_stems:
        cmd += ["--two-stems", two_stems]
    cmd.append(str(Path(input_path).resolve()))
    progress(f"[separate] {' '.join(cmd)}")
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    track = Path(input_path).stem
    produced = out_path / model / track
    stems = {}
    for wav in sorted(produced.glob("*.wav")):
        name = wav.stem
        if name == "no_vocals":
            name = "accompaniment"
        stems[name] = str(wav)
    if not stems:
        raise RuntimeError(f"Demucs produced no stems under {produced}")
    progress(f"[separate] stems: {', '.join(sorted(stems))}")
    return stems


def list_stems(model: str) -> tuple:
    return STEMS if model.startswith("htdemucs") else STEMS


def cleanup(out_dir: str) -> None:
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir, ignore_errors=True)
