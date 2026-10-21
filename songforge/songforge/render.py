"""Utilities shared by the backends: melody rendering and text prompts."""

from __future__ import annotations

import numpy as np

from .analyze import Conditioning
from .audio import EPS, add_at


def render_melody(cond: Conditioning, sr: int, duration: float,
                  timbre: str = "soft") -> np.ndarray:
    """Render the transcribed melody as a clean monophonic guide track.

    Neural models that take a "melody" input (MusicGen-Melody, ACE-Step's audio
    prompt, ...) usually derive chroma/prosody from the audio.  Feeding them a
    purpose-built melody guide is far more reliable than feeding a full vocal
    with consonants, breaths and processing.
    """
    n = int(duration * sr)
    out = np.zeros(n, dtype=np.float32)
    t = np.arange(int(0.5 * sr), dtype=np.float32) / sr
    for note in cond.melody:
        f = 440.0 * 2.0 ** ((note.midi - 69) / 12.0)
        dur = max(note.dur, 0.06)
        seg_n = min(int(dur * sr), n)
        if seg_n < 8:
            continue
        tt = np.arange(seg_n, dtype=np.float32) / sr
        vib = 1.0 + 0.004 * np.sin(2.0 * np.pi * 5.2 * tt)
        phase = 2.0 * np.pi * np.cumsum(vib) * f / sr
        if timbre == "soft":
            sig = np.sin(phase) + 0.22 * np.sin(2 * phase) + 0.08 * np.sin(3 * phase)
        else:
            sig = np.sin(phase) + 0.4 * np.sin(2 * phase) + 0.2 * np.sin(3 * phase)
        env = np.ones(seg_n, dtype=np.float32)
        k = max(2, int(0.012 * sr))
        env[:k] = np.linspace(0, 1, k, dtype=np.float32)
        k2 = min(seg_n, max(2, int(0.05 * sr)))
        env[-k2:] *= np.linspace(1, 0, k2, dtype=np.float32)
        add_at(out, (sig[:seg_n] * env).astype(np.float32), int(note.start * sr),
               float(note.velocity))
    del t
    peak = float(np.max(np.abs(out))) + EPS
    return (out / peak * 0.85).astype(np.float32)


def beat_grid(cond: Conditioning) -> tuple[float, float]:
    beat = 60.0 / max(cond.bpm, 30.0)
    return beat, cond.beat_phase or 0.0


def section_label_at(cond: Conditioning, t: float) -> str:
    for s in cond.sections:
        if s.start <= t < s.end:
            return s.label
    return "verse"


DEFAULT_PROMPT = {
    "intro": "sparse introduction, filtered pads, no drums",
    "verse": "restrained arrangement, groove, supportive harmony",
    "prechorus": "building tension, rising energy",
    "chorus": "full energetic arrangement, bright and wide",
    "bridge": "contrasting texture, stripped back",
    "outro": "fading outro, sparse, resolving",
}


def build_prompt(cond: Conditioning, label: str = "", style: str = "",
                 extra: str = "") -> str:
    """Compose a text prompt for a text-conditioned music model."""
    parts = []
    base = (cond.prompt or "").strip()
    if base:
        parts.append(base)
    if style and style not in ("auto", ""):
        parts.append(style + " style")
    if label:
        parts.append(DEFAULT_PROMPT.get(label, label))
    if extra:
        parts.append(extra)
    parts.append(f"in {cond.key_name}, {cond.bpm:.1f} bpm, instrumental backing track, no lead vocals")
    return ", ".join(p for p in parts if p)


def describe(cond: Conditioning) -> str:
    beats, _ = beat_grid(cond)
    used = [n for n in cond.melody]
    pitch_range = (min((n.midi for n in used), default=0), max((n.midi for n in used), default=0))
    return (
        f"{cond.duration:.1f}s | {cond.bpm:.1f} BPM | {cond.key_name} "
        f"(confidence {cond.key_confidence:.2f}) | {len(used)} melody notes "
        f"(MIDI {pitch_range[0]}..{pitch_range[1]}) | "
        f"{len(cond.sections)} sections | bar = {4 * beats:.2f}s"
    )
