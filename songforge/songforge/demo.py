"""A synthetic "vocal" used by the demo command and the test suite.

It is a clean monophonic melody with vibrato, breath noise and consonant
transients - deliberately *much* simpler than a real singer, but enough to
exercise every stage of the pipeline without shipping copyrighted audio.
"""

from __future__ import annotations

import numpy as np

from .audio import DEFAULT_SR, add_at, fade, reverb, to_stereo

# a 16-bar melody in C major, (midi, beats)
_PHRASE = [
    (67, 1), (69, 1), (72, 1.5), (71, 0.5),
    (69, 1), (67, 1), (64, 2),
    (65, 1), (67, 1), (69, 1.5), (67, 0.5),
    (64, 1), (62, 1), (60, 2),
    (67, 1), (72, 1), (76, 1.5), (74, 0.5),
    (72, 1), (71, 1), (69, 2),
    (72, 1), (74, 1), (76, 1.5), (74, 0.5),
    (72, 1), (69, 1), (67, 2),
]


def _syllable(freq: float, dur: float, sr: int, rng, bright: float = 1.0) -> np.ndarray:
    n = int(dur * sr)
    t = np.arange(n, dtype=np.float32) / sr
    vib = 1.0 + 0.006 * np.sin(2 * np.pi * 5.4 * t + rng.uniform(0, 6.28))
    phase = 2 * np.pi * np.cumsum(vib) * freq / sr
    # vowel-ish spectrum: strong fundamental + formants near 700/1200/2600 Hz
    formants = [(700.0, 1.0, 90.0), (1220.0, 0.55, 110.0), (2600.0, 0.22, 160.0)]
    sig = np.zeros(n, dtype=np.float32)
    for k in range(1, 14):
        f = freq * k
        if f > sr * 0.45:
            break
        amp = 0.25 / k
        for center, gain, bw in formants:
            amp += gain / k * np.exp(-0.5 * ((f - center) / (bw * 2.2)) ** 2)
        if f < 180:
            amp *= 1.6
        sig += (amp * np.sin(k * phase)).astype(np.float32)
    sig /= (np.max(np.abs(sig)) + 1e-9)
    sig *= bright

    env = np.ones(n, dtype=np.float32)
    a = max(2, int(0.025 * sr))
    r = max(2, int(0.05 * sr))
    env[:a] = np.linspace(0, 1, a) ** 0.7
    env[-r:] *= np.linspace(1, 0, r)
    # consonant burst at the start (gives the onset detector something to bite)
    cons = int(0.02 * sr)
    if cons < n:
        sig[:cons] += rng.standard_normal(cons).astype(np.float32) * 0.25
    return (sig * env).astype(np.float32)


def make_demo_vocal(bpm: float = 104.0, sr: int = DEFAULT_SR, seed: int = 3,
                    repeats: int = 2) -> np.ndarray:
    rng = np.random.default_rng(seed)
    beat = 60.0 / bpm
    length = int((_total_beats(_PHRASE) * repeats + 4) * beat * sr) + sr
    out = np.zeros(length, dtype=np.float32)
    pos = 2 * beat                          # two beats of silence, then sing
    for rep in range(repeats):
        for midi, beats in _PHRASE:
            dur = beats * beat * 0.94
            freq = 440.0 * 2 ** ((midi - 69) / 12.0)
            sig = _syllable(freq, dur, sr, rng, bright=1.0 - 0.15 * rep)
            add_at(out, sig, int(pos * sr), 0.7)
            pos += beats * beat
        pos += 2 * beat
    out = fade(out, sr, 60.0, 400.0)
    out = reverb(out[:, None], sr, decay_s=1.1, mix=0.09, predelay_ms=20)[:, 0]
    out = out / (np.max(np.abs(out)) + 1e-9) * 0.6
    return to_stereo(out)


def _total_beats(phrase) -> float:
    return float(sum(b for _, b in phrase))


def make_demo_song(bpm: float = 104.0, sr: int = DEFAULT_SR, seed: int = 3) -> np.ndarray:
    """A rough 'original recording': demo vocal + a simple backing, mixed."""
    vocal = make_demo_vocal(bpm=bpm, sr=sr, seed=seed)
    from .mix import mix_song
    from .procedural import ProceduralBackend
    from .analyze import analyze_vocal
    cond = analyze_vocal(vocal, sr, prompt="lo-fi chill backing track")
    music = ProceduralBackend(seed=seed).generate(cond, vocals=vocal)
    song, _ = mix_song(vocal, music, sr, master=False)
    return song
