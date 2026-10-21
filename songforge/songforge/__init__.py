"""songforge - vocal-first song generation pipeline.

    vocals in  ->  analysis  ->  music model  ->  mix + master  ->  song out
"""

from __future__ import annotations

__version__ = "0.1.0"

from .analyze import Conditioning, NoteEvent, Section, analyze_vocal
from .backends import available_backends, create_backend
from .mix import MixSettings, mix_song
from .pipeline import PipelineConfig, PipelineResult, run_pipeline

__all__ = [
    "Conditioning", "NoteEvent", "Section", "analyze_vocal",
    "available_backends", "create_backend",
    "MixSettings", "mix_song",
    "PipelineConfig", "PipelineResult", "run_pipeline",
    "__version__",
]
