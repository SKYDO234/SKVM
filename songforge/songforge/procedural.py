"""A dependency-free "generate the backing track" backend.

This is intentionally *not* a neural network: it is a small rule-based
arranger/synthesiser.  It exists because

* it makes the whole pipeline runnable (and testable) on a CPU with no model
  downloads, so you can hear the arrangement/mixing half of the problem;
* it is a useful fallback when no GPU is available;
* it shows exactly which musical decisions the neural backend has to make
  (form, chord rhythm, voicing, register separation, dynamics).

Every AI backend implements the same interface, so swapping this out for
MusicGen / ACE-Step / your own fine-tune needs no other code changes.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from .analyze import Conditioning, scale_pcs
from .audio import DEFAULT_SR, EPS, db_to_gain, filter_signal, reverb

# --------------------------------------------------------------------------- #
# harmony
# --------------------------------------------------------------------------- #
PROGRESSIONS = {
    "major": [
        [0, 7, 9, 5],    # I  V  vi IV
        [0, 5, 9, 7],    # I  IV vi V
        [0, 9, 5, 7],    # I  vi IV V
        [5, 7, 0, 9],    # IV V  I  vi
        [0, 4, 5, 7],    # I  iii IV V
    ],
    "minor": [
        [0, 8, 3, 10],   # i  VI III VII
        [0, 5, 8, 7],    # i  iv VI v
        [0, 10, 8, 0],   # i  VII VI i
        [0, 3, 8, 10],   # i  III VI VII
    ],
}

# semitone offsets of a triad above the chord root
TRIADS = {"maj": [0, 4, 7], "min": [0, 3, 7]}


def choose_progression(cond: Conditioning) -> list[int]:
    """Pick the library progression whose roots best explain the vocal chroma."""
    library = PROGRESSIONS.get(cond.key_mode, PROGRESSIONS["major"])
    hints = [int(h[0]) % 12 for h in cond.chord_hints if isinstance(h, (list, tuple))]
    if not hints:
        return library[0]
    best, best_score = library[0], -1.0
    for prog in library:
        roots = [(cond.key_root + p) % 12 for p in prog]
        score = sum(1.0 for h in hints if h in roots) / max(1, len(hints))
        if score > best_score:
            best, best_score = prog, score
    return best


def diatonic_triad(root_pc: int, key_root: int, key_mode: str) -> str:
    """Major/minor quality for a chord root inside the detected key."""
    degree = (root_pc - key_root) % 12
    if key_mode == "minor":
        return "min" if degree in (0, 2, 4, 5, 7) else "maj"
    return "min" if degree in (2, 4, 9) else "maj"


def bar_chords(cond: Conditioning) -> list[tuple[int, list[int]]]:
    """Return ``[(bass_midi_root, triad_pitches)]`` for every bar of the take.

    Chord hints measured from the vocal are only trusted when they are diatonic
    to the detected key; a melody-heavy chroma often suggests nonsense chords.
    """
    beat = 60.0 / max(cond.bpm, 30.0)
    bar = 4.0 * beat
    n_bars = max(1, int(np.ceil(cond.duration / bar)) + 1)
    prog = choose_progression(cond)
    scale = set(scale_pcs(cond.key_root, cond.key_mode))
    hints = cond.chord_hints or []
    out = []
    for b in range(n_bars):
        root_pc, quality = None, None
        if b < len(hints):
            cand = int(hints[b][0]) % 12
            if cand in scale:
                root_pc, quality = cand, hints[b][1]
        if root_pc is None:
            root_pc = (cond.key_root + prog[b % len(prog)]) % 12
            quality = diatonic_triad(root_pc, cond.key_root, cond.key_mode)
        if quality not in TRIADS:
            quality = diatonic_triad(root_pc, cond.key_root, cond.key_mode)

        root_midi = 36 + (root_pc % 12)                  # C2..B2 (65..123 Hz)
        triad = [root_midi + 12 + iv for iv in TRIADS[quality]]   # C3..B4
        out.append((int(root_midi), [int(p) for p in triad]))
    return out


# --------------------------------------------------------------------------- #
# synthesis primitives
# --------------------------------------------------------------------------- #
def _note_hz(midi: float) -> float:
    return 440.0 * 2.0 ** ((midi - 69.0) / 12.0)


def _adsr(n: int, sr: int, attack: float = 0.005, decay: float = 0.08,
          sustain: float = 0.75, release: float = 0.25) -> np.ndarray:
    n = max(n, 8)
    a = max(1, int(attack * sr))
    r = max(1, int(release * sr))
    d = max(1, int(decay * sr))
    a, d, r = min(a, n), min(d, n), min(r, n)
    env = np.zeros(n, dtype=np.float32)
    pos = 0
    seg = np.linspace(0.0, 1.0, a, dtype=np.float32) ** 0.6
    env[pos:pos + len(seg)] = seg
    pos += len(seg)
    seg = np.linspace(1.0, sustain, d, dtype=np.float32)
    env[pos:pos + len(seg)] = seg[:max(0, n - pos)]
    pos += len(seg)
    if pos < n - r:
        env[pos:n - r] = sustain
    seg = np.linspace(env[max(0, n - r - 1)] if n - r > 0 else sustain, 0.0, r,
                      dtype=np.float32)
    env[n - r:] = seg[:r]
    return env


def _saw(freq: float, n: int, sr: int, n_harm: int = 10, detune: float = 0.0,
         phase: float = 0.0) -> np.ndarray:
    t = np.arange(n, dtype=np.float32) / sr
    out = np.zeros(n, dtype=np.float32)
    f0 = freq * (1.0 + detune)
    for k in range(1, n_harm + 1):
        f = f0 * k
        if f > sr * 0.45:
            break
        out += np.sin(2.0 * np.pi * f * t + phase * k) / k
    return out * (2.0 / np.pi)


def _noise(n: int, seed: int) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal(n).astype(np.float32)


def synth_kick(sr: int, dur: float = 0.42, seed: int = 1) -> np.ndarray:
    n = int(dur * sr)
    t = np.arange(n) / sr
    sweep = 42.0 + 110.0 * np.exp(-t * 32.0)
    phase = 2.0 * np.pi * np.cumsum(sweep) / sr
    body = np.sin(phase) * np.exp(-t * 6.5)
    click = _noise(n, seed) * np.exp(-t * 180.0) * 0.25
    return np.tanh((body + click) * 1.6).astype(np.float32)


def synth_snare(sr: int, dur: float = 0.26, seed: int = 2) -> np.ndarray:
    n = int(dur * sr)
    t = np.arange(n) / sr
    noise = _noise(n, seed) * np.exp(-t * 20.0)
    tone = np.sin(2.0 * np.pi * 185.0 * t) * np.exp(-t * 28.0) * 0.5
    body = np.diff(np.diff(noise), prepend=0.0, append=0.0) * 0.5   # thin it out
    return ((body + tone) * 0.7).astype(np.float32)


def synth_clap(sr: int, seed: int = 3) -> np.ndarray:
    n = int(0.25 * sr)
    out = np.zeros(n, dtype=np.float32)
    for i, offset in enumerate((0.0, 0.011, 0.022)):
        part = synth_snare(sr, 0.12, seed + i) * (1.0 - 0.25 * i)
        start = int(offset * sr)
        end = min(n, start + len(part))
        out[start:end] += part[:end - start]
    return out


def synth_hat(sr: int, dur: float = 0.06, open_: bool = False, seed: int = 4) -> np.ndarray:
    n = int((0.32 if open_ else dur) * sr)
    t = np.arange(n) / sr
    noise = _noise(n, seed)
    bright = np.diff(noise, prepend=0.0)          # high-pass
    bright = np.diff(bright, prepend=0.0)
    env = np.exp(-t * (9.0 if open_ else 55.0))
    return (bright * env * 0.28).astype(np.float32)


def synth_bass(freq: float, dur: float, sr: int, bright: float = 0.35) -> np.ndarray:
    n = int(dur * sr)
    env = _adsr(n, sr, 0.006, 0.12, 0.7, 0.09)
    tone = np.sin(2.0 * np.pi * freq * np.arange(n) / sr)
    sub = np.sin(2.0 * np.pi * freq / 2.0 * np.arange(n) / sr) * 0.25
    grit = _saw(freq, n, sr, n_harm=5) * bright * 0.5
    return ((tone + sub + grit) * env * 0.55).astype(np.float32)


def synth_pluck(freq: float, dur: float, sr: int, bright: float = 0.6,
                seed: int = 5) -> np.ndarray:
    n = int(dur * sr)
    t = np.arange(n) / sr
    env = np.exp(-t * (3.0 + 5.0 * (1.0 - bright)))
    tone = _saw(freq, n, sr, n_harm=int(6 + 10 * bright))
    return (tone * env * 0.22).astype(np.float32)


def synth_pad(midi_notes: list[int], dur: float, sr: int, attack: float = 0.25,
              release: float = 0.4, brightness: float = 0.5) -> np.ndarray:
    n = int(dur * sr)
    env = np.ones(n, dtype=np.float32)
    a = max(1, int(attack * sr))
    r = max(1, int(release * sr))
    env[:a] = np.linspace(0.0, 1.0, a, dtype=np.float32)
    env[-r:] *= np.linspace(1.0, 0.0, r, dtype=np.float32)
    out = np.zeros((n, 2), dtype=np.float32)
    for i, midi in enumerate(midi_notes):
        freq = _note_hz(midi)
        left = _saw(freq, n, sr, n_harm=8, detune=-0.004 - 0.001 * i)
        right = _saw(freq, n, sr, n_harm=8, detune=+0.004 + 0.001 * i)
        pan = 0.5 + 0.22 * ((i % 3) - 1)
        out[:, 0] += left * (1.0 - pan)
        out[:, 1] += right * pan
    out *= env[:, None] * (0.16 * (0.6 + 0.8 * brightness))
    return out


# --------------------------------------------------------------------------- #
# rhythm patterns (16 steps per bar)
# --------------------------------------------------------------------------- #
def _steps(pattern: str) -> list[int]:
    return [i for i, ch in enumerate(pattern) if ch != "."]


PATTERNS = {
    "house":   dict(kick="X...X...X...X...", snare="....X.......X...",
                    hat="..X...X...X...X.", open_hat="....X.......X...", bass="off8",
                    chord="stabs", arp=False),
    "backbeat": dict(kick="X.......X.X.....", snare="....X.......X...",
                     hat="X.X.X.X.X.X.X.X.", open_hat="..............X.", bass="roots",
                     chord="pad", arp=False),
    "lofi":    dict(kick="X.........X.....", snare="....X.......X...",
                    hat="..X...X...X...X.", open_hat="", bass="long",
                    chord="pad", arp=False),
    "trap":    dict(kick="X.....X...X.....", snare="....X.......X...",
                    hat="X.XXX.XXX.XXX.XX", open_hat="", bass="long",
                    chord="pad", arp=False),
    "rock":    dict(kick="X...X.X.X...X...", snare="....X.......X...",
                    hat="X.X.X.X.X.X.X.X.", open_hat="", bass="eighths",
                    chord="pad", arp=False),
    "ballad":  dict(kick="X.......X.......", snare="....X.......X...",
                    hat="........X.......", open_hat="", bass="long",
                    chord="pad", arp=False),
    "ambient": dict(kick="", snare="", hat="", open_hat="", bass="long",
                    chord="pad", arp=True),
}

STYLE_WORDS = {
    "house": ("house", "edm", "dance", "techno", "club", "four on the floor", "electronic"),
    "lofi": ("lofi", "lo-fi", "chill", "jazzhop", "study", "boom bap"),
    "trap": ("trap", "hip hop", "hiphop", "drill", "rap"),
    "rock": ("rock", "punk", "grunge", "metal", "band"),
    "ballad": ("ballad", "piano", "acoustic", "slow", "sad"),
    "ambient": ("ambient", "cinematic", "pad", "drone", "texture", "orchestral"),
}


def detect_style(prompt: str) -> str:
    text = (prompt or "").lower()
    for style, words in STYLE_WORDS.items():
        if any(w in text for w in words):
            return style
    return "backbeat"


# --------------------------------------------------------------------------- #
# backend
# --------------------------------------------------------------------------- #
class ProceduralBackend:
    """Rule-based arranger -> multi-bus synthesised backing track."""

    name = "procedural"
    max_window: Optional[float] = None

    def __init__(self, style: str = "auto", seed: int = 0):
        self.style = style
        self.seed = seed

    # -- public API -------------------------------------------------------- #
    def generate(self, cond: Conditioning, vocals: Optional[np.ndarray] = None,
                 duration: Optional[float] = None) -> np.ndarray:
        sr = cond.sr or DEFAULT_SR
        duration = float(duration or cond.duration or 30.0)
        style = detect_style(cond.prompt) if self.style in ("auto", "", None) else self.style
        pat = PATTERNS.get(style, PATTERNS["backbeat"])
        rng = np.random.default_rng(self.seed or cond.seed)

        bpm = float(np.clip(cond.bpm, 55.0, 190.0))
        beat = 60.0 / bpm
        step = beat / 4.0
        bar = beat * 4.0
        # start the grid on the vocal's detected pulse so kicks land with it
        offset = float(np.clip(cond.beat_phase or 0.0, 0.0, beat * 0.999))
        n_bars = max(1, int(np.ceil(max(duration - offset, 0.0) / bar)))
        total = int(np.ceil(offset * sr + n_bars * bar * sr)) + sr // 2

        drums = np.zeros((total, 2), dtype=np.float32)
        bass_bus = np.zeros((total, 2), dtype=np.float32)
        harm_bus = np.zeros((total, 2), dtype=np.float32)
        arp_bus = np.zeros((total, 2), dtype=np.float32)

        chords = bar_chords(cond)
        melody = cond.melody or []
        swing = 0.0 if style in ("house", "rock") else (0.16 if style == "lofi" else 0.06)

        kick_times: list[float] = []

        for b in range(n_bars):
            start = offset + b * bar
            section = _section_at(cond, start + bar / 2.0)
            energy = 1.0 if section in ("chorus",) else (0.75 if section == "verse" else 0.5)
            root_midi, triad = chords[min(b, len(chords) - 1)]

            # ---- drums --------------------------------------------------- #
            if section != "intro" and section != "outro" or (b % 4 == 3 and section == "intro"):
                for s in _steps(pat["kick"]):
                    t = start + s * step + rng.uniform(-0.004, 0.004)
                    _place(drums, synth_kick(sr, seed=self.seed + b * 17 + s), t, sr, 0.9)
                    kick_times.append(t)
                if section in ("verse", "chorus", "prechorus"):
                    for s in _steps(pat["snare"]):
                        t = start + s * step
                        _place(drums, synth_snare(sr, seed=self.seed + b * 7 + s), t, sr, 0.55)
                    if style in ("house", "trap"):
                        for s in _steps(pat["open_hat"]):
                            _place(drums, synth_hat(sr, open_=True, seed=self.seed + b + s),
                                   start + s * step, sr, 0.30)
                hat_vel = 0.24 if section == "chorus" else 0.18
                for s in _steps(pat["hat"]):
                    t = start + s * step + (swing * step if s % 2 else 0.0)
                    _place(drums, synth_hat(sr, seed=self.seed + b * 3 + s), t, sr, hat_vel)

            # ---- bass ---------------------------------------------------- #
            if section != "intro" or b % 2 == 1:
                if pat["bass"] == "off8":
                    for k in range(8):
                        t = start + (k + 0.5) * (beat / 2.0)
                        midi = root_midi + (12 if k in (3, 7) else 0)
                        _place(bass_bus, synth_bass(_note_hz(midi), beat * 0.45, sr), t, sr, 0.7)
                elif pat["bass"] == "eighths":
                    for k in range(8):
                        midi = root_midi + (7 if k == 6 else 0)
                        _place(bass_bus, synth_bass(_note_hz(midi), beat * 0.45, sr),
                               start + k * beat / 2, sr, 0.6)
                elif pat["bass"] == "roots":
                    for t_off, midi in ((0.0, root_midi), (2.5 * beat, root_midi),
                                        (3.5 * beat, root_midi + 7)):
                        _place(bass_bus, synth_bass(_note_hz(midi), beat * 0.9, sr), start + t_off, sr, 0.62)
                else:  # long
                    _place(bass_bus, synth_bass(_note_hz(root_midi), bar * 0.94, sr, bright=0.2),
                           start, sr, 0.62)

            # ---- harmony ------------------------------------------------- #
            voicing = _voice_triad(triad, melody, start, bar)
            pad = synth_pad(voicing, bar * 1.02, sr, attack=0.2,
                            brightness=0.4 + 0.35 * energy)
            _place(harm_bus, pad, start, sr, 0.75 if section != "chorus" else 0.95)
            if pat["chord"] == "stabs" and section in ("verse", "chorus", "prechorus"):
                for s in (2, 6, 10, 14):
                    stab = synth_pluck(_note_hz(voicing[s % len(voicing)]), beat * 0.35, sr, 0.7)
                    chord_stab = np.stack([stab, np.roll(stab, 37)], axis=1)
                    _place(harm_bus, chord_stab, start + s * step, sr, 0.30)

            # ---- arpeggio ------------------------------------------------ #
            if section == "chorus" or (style == "ambient" and section != "intro"):
                order = [0, 1, 2, 1]
                for k in range(8):
                    midi = voicing[order[k % 4]] + 12
                    t = start + k * beat / 2.0
                    _place(arp_bus, synth_pluck(_note_hz(midi), beat * 0.4, sr, 0.85),
                           t, sr, 0.20)

            # ---- fill into the next section ------------------------------ #
            if section in ("verse", "prechorus") and _section_at(cond, start + bar + 0.01) == "chorus":
                for k in range(4):
                    _place(drums, synth_snare(sr, seed=self.seed + 99 + k),
                           start + bar - (4 - k) * step, sr, 0.22 + 0.06 * k)

        # ---- bus processing ---------------------------------------------- #
        if kick_times:
            pumping = _pump_envelope(total, sr, kick_times, depth_db=2.5)
            harm_bus *= pumping[:, None]
            bass_bus *= pumping[:, None]

        harm_bus = filter_signal(harm_bus, sr, "lowpass", 3800.0, q=0.707)
        arp_bus = filter_signal(arp_bus, sr, "lowpass", 6000.0, q=0.707)
        bass_bus = filter_signal(bass_bus, sr, "lowpass", 1100.0, q=0.7)
        bass_bus = filter_signal(bass_bus, sr, "highpass", 35.0, q=0.7)
        harm_bus = filter_signal(harm_bus, sr, "highpass", 160.0, q=0.7)
        arp_bus = filter_signal(arp_bus, sr, "highpass", 300.0, q=0.7)
        drums = filter_signal(drums, sr, "highpass", 30.0, q=0.7)

        if style in ("ballad", "ambient", "lofi"):
            harm_bus = reverb(harm_bus, sr, decay_s=2.2, mix=0.30, predelay_ms=25)
            arp_bus = reverb(arp_bus, sr, decay_s=1.8, mix=0.35, predelay_ms=18)
        else:
            harm_bus = reverb(harm_bus, sr, decay_s=1.2, mix=0.16, predelay_ms=12)
            arp_bus = reverb(arp_bus, sr, decay_s=1.1, mix=0.22, predelay_ms=10)
        drums = reverb(drums, sr, decay_s=0.55, mix=0.07, predelay_ms=6)

        mix = drums * db_to_gain(-1.0) + bass_bus + harm_bus + arp_bus
        mix = np.tanh(mix * 1.1) * 0.85                      # gentle saturation
        mix = mix[: int(duration * sr) + sr // 2]
        top = float(np.max(np.abs(mix))) + EPS
        return (mix * min(1.0, 0.7 / top)).astype(np.float32)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _place(buffer: np.ndarray, sig: np.ndarray, start_sec: float, sr: int,
           gain: float) -> None:
    start = int(start_sec * sr)
    if start < 0:
        sig = sig[-start:]
        start = 0
    if start >= len(buffer):
        return
    n = min(len(sig), len(buffer) - start)
    chunk = sig[:n]
    if chunk.ndim == 1:
        buffer[start:start + n, 0] += chunk * gain
        buffer[start:start + n, 1] += chunk * gain
    else:
        buffer[start:start + n] += chunk[:n, :2] * gain


def _section_at(cond: Conditioning, t: float) -> str:
    for s in cond.sections:
        if s.start <= t < s.end:
            return s.label
    return "verse"


def _voice_triad(triad: list[int], melody, start: float, bar: float) -> list[int]:
    """Move the whole triad into a register that leaves room for the vocal.

    The chord keeps its internal intervals (no note-wise octave jumps, which
    sound like broken chords), it is only transposed by octaves as a block.
    """
    root = min(triad)
    voiced = sorted(root + (n - root) for n in triad)
    while max(voiced) > 72:
        voiced = [v - 12 for v in voiced]
    while min(voiced) < 48:
        voiced = [v + 12 for v in voiced]
    notes = [n for n in melody if start - 0.2 <= n.start < start + bar + 0.2]
    if notes:
        low, high = min(n.midi for n in notes) - 2, max(n.midi for n in notes)
        overlaps = sum(low <= v <= high for v in voiced)
        if overlaps >= 2 and max(voiced) <= 72:
            voiced = [v + 12 for v in voiced]
        elif overlaps >= 2:
            voiced = [v - 12 for v in voiced]
    return [int(v) for v in voiced]


def _pump_envelope(total: int, sr: int, kick_times: list[float],
                   depth_db: float = 2.5) -> np.ndarray:
    env = np.ones(total, dtype=np.float32)
    n = int(0.28 * sr)
    shape = (1.0 - np.exp(-np.arange(n) / (0.012 * sr))) * np.exp(-np.arange(n) / (0.11 * sr))
    shape = db_to_gain(-depth_db) + (1.0 - db_to_gain(-depth_db)) * (1.0 - shape)
    for t in kick_times:
        i = int(t * sr)
        if 0 <= i < total:
            m = min(n, total - i)
            env[i:i + m] = np.minimum(env[i:i + m], shape[:m])
    return env
