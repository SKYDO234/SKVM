"""Vocal analysis: tempo, key, melody, chords and song structure.

This is the stage that turns "a vocal recording" into *conditioning* the music
model can use.  Everything is plain numpy so it runs on a laptop CPU.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

import numpy as np

from .audio import EPS, to_mono

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Krumhansl-Schmuckler key profiles
_MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


@dataclass
class NoteEvent:
    start: float
    dur: float
    midi: int
    velocity: float = 0.7

    @property
    def name(self) -> str:
        return NOTE_NAMES[self.midi % 12] + str(self.midi // 12 - 1)


@dataclass
class Section:
    start: float
    end: float
    label: str
    energy: float = 0.5


@dataclass
class Conditioning:
    """Everything the generator is allowed to know about the vocal."""

    sr: int = 44100
    duration: float = 0.0
    bpm: float = 120.0
    beat_phase: float = 0.0
    key_root: int = 0
    key_mode: str = "major"
    key_confidence: float = 0.0
    melody: list = field(default_factory=list)
    sections: list = field(default_factory=list)
    chord_hints: list = field(default_factory=list)
    prompt: str = ""
    seed: int = 0

    @property
    def key_name(self) -> str:
        return f"{NOTE_NAMES[self.key_root % 12]} {self.key_mode}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["key_name"] = self.key_name
        d["melody"] = [asdict(n) if isinstance(n, NoteEvent) else n for n in self.melody]
        d["sections"] = [asdict(s) if isinstance(s, Section) else s for s in self.sections]
        d["chord_hints"] = [
            {"root": NOTE_NAMES[c[0] % 12], "quality": c[1]} if isinstance(c, (list, tuple)) else c
            for c in self.chord_hints]
        return d


# --------------------------------------------------------------------------- #
# spectral helpers
# --------------------------------------------------------------------------- #
def stft(x: np.ndarray, n_fft: int = 2048, hop: int = 512) -> np.ndarray:
    x = to_mono(x)
    if len(x) < n_fft:
        x = np.pad(x, (0, n_fft - len(x)))
    win = np.hanning(n_fft).astype(np.float32)
    n_frames = 1 + (len(x) - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n_frames)[:, None]
    return np.fft.rfft(x[idx] * win, axis=1)


def istft(spec: np.ndarray, n_fft: int = 2048, hop: int = 512, length: Optional[int] = None):
    frames = np.fft.irfft(spec, n=n_fft, axis=1)
    win = np.hanning(n_fft).astype(np.float32)
    out = np.zeros(hop * (len(frames) - 1) + n_fft, dtype=np.float32)
    norm = np.zeros_like(out)
    for i, frame in enumerate(frames):
        out[i * hop:i * hop + n_fft] += frame * win
        norm[i * hop:i * hop + n_fft] += win ** 2
    out /= np.maximum(norm, 1e-3)
    return out[:length] if length else out


def chroma(x: np.ndarray, sr: int, n_fft: int = 4096, hop: int = 1024) -> np.ndarray:
    """Frame-wise chroma (12 pitch classes), L1-normalised."""
    spec = np.abs(stft(x, n_fft, hop))
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
    mask = (freqs >= 55.0) & (freqs <= 2093.0)
    freqs = freqs[mask]
    spec = spec[:, mask]
    midi = np.round(69.0 + 12.0 * np.log2(np.maximum(freqs, 1e-9) / 440.0)).astype(int)
    pcs = np.mod(midi, 12)
    out = np.zeros((spec.shape[0], 12), dtype=np.float64)
    for pc in range(12):
        cols = pcs == pc
        if np.any(cols):
            out[:, pc] = spec[:, cols].sum(axis=1)
    out /= (out.sum(axis=1, keepdims=True) + EPS)
    return out


def onset_envelope(x: np.ndarray, sr: int, n_fft: int = 1024, hop: int = 256):
    spec = np.abs(stft(x, n_fft, hop))
    logspec = np.log1p(spec * 10.0)
    flux = np.maximum(0.0, np.diff(logspec, axis=0)).sum(axis=1)
    if flux.size == 0:
        return np.zeros(1, dtype=np.float32), sr / hop
    # subtract a slow moving average -> keeps note attacks, drops sustained energy
    win = 31
    kernel = np.ones(win) / win
    local = np.convolve(flux, kernel, mode="same")
    flux = np.maximum(flux - local, 0.0)
    flux = flux / (np.max(flux) + EPS)
    return flux.astype(np.float32), sr / hop


# --------------------------------------------------------------------------- #
# tempo / key / harmony
# --------------------------------------------------------------------------- #
def estimate_tempo(x: np.ndarray, sr: int, fmin: float = 60.0,
                   fmax: float = 190.0) -> tuple[float, float]:
    """Return ``(bpm, beat_phase_seconds)`` via onset autocorrelation."""
    env, fps = onset_envelope(x, sr)
    if len(env) < 16:
        return 120.0, 0.0
    env = env - env.mean()
    n = 1 << (2 * len(env) - 1).bit_length()
    spec = np.fft.rfft(env, n)
    ac = np.fft.irfft(spec * np.conj(spec), n)[:len(env)]
    ac /= (ac[0] + EPS)

    lags = np.arange(max(1, int(fps * 60.0 / fmax)), int(fps * 60.0 / fmin) + 1)
    if lags.size == 0:
        return 120.0, 0.0
    score = ac[lags].copy()
    bpms = 60.0 * fps / lags
    # log-normal tempo prior around 118 BPM
    score *= np.exp(-0.5 * (np.log2(bpms / 118.0) / 0.85) ** 2)
    # slight preference for the slower of two octave-equivalent candidates
    best = int(np.argmax(score))
    bpm = float(bpms[best])
    lag = float(lags[best])
    # refine with parabolic interpolation on the raw autocorrelation
    if 0 < best < len(lags) - 1:
        y0, y1, y2 = ac[lags[best - 1]], ac[lags[best]], ac[lags[best + 1]]
        denom = (y0 - 2 * y1 + y2)
        if abs(denom) > EPS:
            lag += 0.5 * (y0 - y2) / denom
    bpm = 60.0 * fps / lag

    # beat phase: fold the onset envelope at the beat period
    period = max(2, int(round(lag)))
    idx = np.arange(len(env)) % period
    folded = np.bincount(idx, weights=np.maximum(env, 0.0), minlength=period)
    phase = int(np.argmax(folded)) / fps
    return float(bpm), float(phase)


def estimate_key(x: np.ndarray, sr: int) -> tuple[int, str, float]:
    """Krumhansl-Schmuckler key finding. Returns ``(root_pc, mode, confidence)``."""
    c = chroma(x, sr).mean(axis=0)
    if c.sum() <= EPS:
        return 0, "major", 0.0
    c = c - c.mean()
    scores = []
    for root in range(12):
        for mode, profile in (("major", _MAJOR_PROFILE), ("minor", _MINOR_PROFILE)):
            p = np.roll(profile, root)
            p = p - p.mean()
            denom = np.linalg.norm(c) * np.linalg.norm(p)
            scores.append((float(np.dot(c, p) / (denom + EPS)), root, mode))
    scores.sort(reverse=True)
    best, root, mode = scores[0]
    second = scores[1][0]
    confidence = float(np.clip((best - second) / (abs(best) + EPS) * 2.0, 0.0, 1.0))
    return int(root), mode, confidence


_CHORD_TEMPLATES = {
    "maj": np.array([1.0, 0.0, 0.0, 0.0, 0.8, 0.0, 0.0, 0.8, 0.0, 0.0, 0.0, 0.0]),
    "min": np.array([1.0, 0.0, 0.0, 0.8, 0.0, 0.0, 0.0, 0.8, 0.0, 0.0, 0.0, 0.0]),
}


def _chord_scores(bar_chroma: np.ndarray) -> list[tuple[float, int, str]]:
    out = []
    norm = np.linalg.norm(bar_chroma) + EPS
    for quality, tpl in _CHORD_TEMPLATES.items():
        for root in range(12):
            t = np.roll(tpl, root)
            out.append((float(np.dot(bar_chroma, t) / (norm * np.linalg.norm(t))), root, quality))
    out.sort(reverse=True)
    return out


def estimate_chords(x: np.ndarray, sr: int, bpm: float, beat_phase: float,
                    beats_per_bar: int = 4) -> list[tuple[int, str]]:
    """One chord hint per bar (best major/minor triad match)."""
    c = chroma(x, sr)
    hop = 1024
    fps = sr / hop
    bar_frames = max(1, int(round(beats_per_bar * 60.0 / bpm * fps)))
    start = int(round(beat_phase * fps))
    hints = []
    for begin in range(start, len(c) - 1, bar_frames):
        seg = c[begin:begin + bar_frames]
        if len(seg) == 0:
            break
        scores = _chord_scores(seg.mean(axis=0))
        hints.append((scores[0][1], scores[0][2]))
    return hints


# --------------------------------------------------------------------------- #
# melody
# --------------------------------------------------------------------------- #
def pitch_track(x: np.ndarray, sr: int, fmin: float = 70.0, fmax: float = 1000.0,
                frame: int = 2048, hop: int = 256):
    """Monophonic f0 track via normalised autocorrelation.

    Good enough for a lead vocal stem; octave errors are corrected by the
    median smoother and by the key-aware quantiser downstream.
    """
    mono = to_mono(x).astype(np.float64)
    if len(mono) < frame:
        mono = np.pad(mono, (0, frame - len(mono)))
    n_frames = 1 + (len(mono) - frame) // hop
    idx = np.arange(frame)[None, :] + hop * np.arange(n_frames)[:, None]
    frames = mono[idx]
    win = np.hanning(frame)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    lag_min = max(2, int(sr / fmax))
    lag_max = min(frame - 2, int(sr / fmin))

    f0 = np.zeros(n_frames, dtype=np.float32)
    periodicity = np.zeros(n_frames, dtype=np.float32)
    chunk = 512
    for s in range(0, n_frames, chunk):
        fr = frames[s:s + chunk] * win
        spec = np.fft.rfft(fr, n=2 * frame, axis=1)
        ac = np.fft.irfft(np.abs(spec) ** 2, n=2 * frame, axis=1)[:, :frame]
        ac /= (ac[:, :1] + EPS)
        seg = ac[:, lag_min:lag_max]
        best = np.argmax(seg, axis=1)
        vals = seg[np.arange(len(best)), best]
        lags = (best + lag_min).astype(np.float64)
        # parabolic interpolation around the peak
        left = ac[np.arange(len(best)), np.maximum(best + lag_min - 1, 0)]
        right = ac[np.arange(len(best)), np.minimum(best + lag_min + 1, frame - 1)]
        denom = left - 2 * vals + right
        adjust = np.where(np.abs(denom) > EPS, 0.5 * (left - right) / (denom + EPS), 0.0)
        lags = lags + np.clip(adjust, -0.5, 0.5)
        f0[s:s + chunk] = (sr / lags).astype(np.float32)
        periodicity[s:s + chunk] = vals.astype(np.float32)

    rel_db = 20.0 * np.log10(rms + 1e-9)
    voiced = (periodicity > 0.30) & (rel_db > (rel_db.max() - 28.0))
    midi = np.full(n_frames, np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        midi[voiced] = 69.0 + 12.0 * np.log2(np.maximum(f0[voiced], 1e-6) / 440.0)
    midi = _median_filter(midi, 5)
    times = (np.arange(n_frames) * hop + frame / 2.0) / sr
    return times, midi, periodicity, rel_db


def _median_filter(x: np.ndarray, k: int) -> np.ndarray:
    if k <= 1:
        return x
    pad = k // 2
    padded = np.pad(x, (pad, pad), mode="edge")
    out = np.copy(x)
    for i in range(len(x)):
        window = padded[i:i + k]
        window = window[np.isfinite(window)]
        out[i] = np.median(window) if window.size else np.nan
    return out


def notes_from_track(times: np.ndarray, midi: np.ndarray, rms_db: np.ndarray,
                     key_root: int = 0, key_mode: str = "major",
                     snap: bool = True, min_dur: float = 0.09,
                     max_gap: float = 0.07) -> list[NoteEvent]:
    """Turn a frame-wise f0 track into quantised note events."""
    notes: list[NoteEvent] = []
    frames: list[tuple[float, float, int]] = []
    current = None
    for t, m, db in zip(times, midi, rms_db):
        if not np.isfinite(m):
            current = None
            continue
        value = float(m)
        # key-aware snapping: pull the pitch to the nearest scale degree
        if snap:
            value = _snap_to_scale(value, key_root, key_mode)
        if current is None or abs(value - current[2]) > 0.6:
            if current is not None:
                frames.append((current[0], current[1], current[2]))
            current = [t, t, value]
        else:
            current[2] = 0.7 * current[2] + 0.3 * value
            current[1] = t
    if current is not None:
        frames.append((current[0], current[1], current[2]))

    for start, end, value in frames:
        dur = end - start
        if dur < min_dur:
            continue
        note = NoteEvent(start=float(start), dur=float(dur), midi=int(round(value)),
                         velocity=0.7)
        gap = note.start - (notes[-1].start + notes[-1].dur) if notes else 0.0
        if notes and notes[-1].midi == note.midi and gap < max_gap:
            notes[-1].dur = note.start + note.dur - notes[-1].start
        else:
            notes.append(note)
    return notes


_SCALES = {
    "major": (0, 2, 4, 5, 7, 9, 11),
    "minor": (0, 2, 3, 5, 7, 8, 10),
}


def scale_pcs(key_root: int, key_mode: str) -> list[int]:
    return [int((key_root + s) % 12) for s in _SCALES.get(key_mode, _SCALES["major"])]


def _snap_to_scale(midi_value: float, key_root: int, key_mode: str) -> float:
    pcs = scale_pcs(key_root, key_mode)
    nearest = min(pcs, key=lambda pc: min(abs(pc - (midi_value % 12)),
                                         12 - abs(pc - (midi_value % 12))))
    return float(round(midi_value - ((midi_value % 12) - nearest)))


# --------------------------------------------------------------------------- #
# structure
# --------------------------------------------------------------------------- #
def estimate_sections(x: np.ndarray, sr: int, bpm: float, duration: float,
                      bar_seconds: Optional[float] = None) -> list[Section]:
    """Heuristic form detection from vocal energy per bar.

    Labels are deliberately coarse: ``intro``, ``verse``, ``chorus``, ``bridge``,
    ``outro``.  A model trained for structure would do better; for arrangement
    control this is enough to make a track that builds and releases.
    """
    bar = bar_seconds or (4 * 60.0 / max(bpm, 30.0))
    n_bars = max(1, int(np.ceil(duration / bar)))
    mono = to_mono(x)
    per_bar = []
    for b in range(n_bars):
        seg = mono[int(b * bar * sr):int((b + 1) * bar * sr)]
        per_bar.append(20.0 * np.log10(np.sqrt(np.mean(seg ** 2)) + 1e-9) if seg.size else -90.0)
    energy = np.array(per_bar)
    if energy.size >= 3:
        pad = np.pad(energy, (1, 1), mode="edge")
        energy = np.convolve(pad, np.ones(3) / 3.0, mode="valid")
    lo, hi = np.percentile(energy, [25, 75])
    span = max(hi - lo, 1e-3)
    rel = (energy - lo) / span

    labels = []
    for b in range(n_bars):
        if rel[b] < -0.35:
            labels.append("intro" if b < n_bars * 0.25 else "bridge")
        elif rel[b] > 0.85:
            labels.append("chorus")
        else:
            labels.append("verse")
    labels[0] = "intro" if rel[0] < 0.6 else labels[0]
    if n_bars > 3:
        labels[-1] = "outro" if rel[-1] < 0.6 else labels[-1]

    # if the vocal has flat energy, impose a classic alternating form
    if len(set(labels)) <= 1 and n_bars >= 8:
        labels = []
        for b in range(n_bars):
            if b < 2:
                labels.append("intro")
            elif b >= n_bars - 2:
                labels.append("outro")
            else:
                labels.append("chorus" if ((b - 2) // 4) % 2 == 1 else "verse")

    sections: list[Section] = []
    for b, label in enumerate(labels):
        start = b * bar
        end = min(duration, (b + 1) * bar)
        if sections and sections[-1].label == label:
            sections[-1].end = end
            sections[-1].energy = float((sections[-1].energy + rel[b]) / 2)
        else:
            sections.append(Section(start=float(start), end=float(end), label=label,
                                    energy=float(rel[b])))
    return sections


# --------------------------------------------------------------------------- #
# top level
# --------------------------------------------------------------------------- #
def analyze_vocal(vocal: np.ndarray, sr: int, prompt: str = "",
                  seed: int = 0) -> Conditioning:
    """Run the full analysis chain on an isolated vocal stem."""
    mono = to_mono(vocal)
    duration = len(mono) / sr
    bpm, phase = estimate_tempo(mono, sr)
    root, mode, conf = estimate_key(mono, sr)
    times, midi, _periodicity, rms_db = pitch_track(mono, sr)
    melody = notes_from_track(times, midi, rms_db, root, mode)
    # a silent/vocal-less input should not yield thousands of junk notes
    melody = [n for n in melody if n.dur >= 0.08][:4000]
    sections = estimate_sections(mono, sr, bpm, duration)
    chords = estimate_chords(mono, sr, bpm, phase)
    return Conditioning(
        sr=sr, duration=duration, bpm=bpm, beat_phase=phase,
        key_root=root, key_mode=mode, key_confidence=conf,
        melody=melody, sections=sections, chord_hints=chords,
        prompt=prompt, seed=seed,
    )
