"""The "merge both" stage: vocal + instrumental -> finished master.

Order matters:

1. clip both stems to the same length (music gets a short tail);
2. clean the vocal (rumble, sibilance) and match its loudness;
3. multiband sidechain duck so the instrumental moves out of the vocal's way
   without losing kick/bass;
4. balance, glue, target loudness, true-peak-ish limiting, fade-out.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np

from .audio import (DEFAULT_SR, db_to_gain, deess, envelope, fade, filter_signal,
                    gain_to_db, limiter, lufs, normalize_lufs, pad_to, peak, peak_db,
                    reverb, rms, to_stereo)


@dataclass
class MixSettings:
    vocal_gain_db: float = 0.0
    music_gain_db: float = -2.5
    vocal_highpass_hz: float = 90.0
    deess_amount: float = 0.45
    vocal_reverb_mix: float = 0.10
    duck_db: float = 4.0
    duck_split_hz: float = 180.0
    target_lufs: float = -14.0
    ceiling_db: float = -1.0
    fade_out_s: float = 2.0
    tail_s: float = 1.2


def _fit(x: np.ndarray, n: int, sr: int, fade_ms: float = 40.0) -> np.ndarray:
    return pad_to(to_stereo(x), n, tail_fade_ms=fade_ms, sr=sr)


def multiband_duck(music: np.ndarray, vocal: np.ndarray, sr: int,
                   depth_db: float = 4.0, split_hz: float = 180.0) -> np.ndarray:
    """Duck only the mid band, so kick and low bass keep their weight."""
    music = to_stereo(music)
    vocal = to_stereo(vocal)
    low = filter_signal(music, sr, "lowpass", split_hz, q=0.707)
    rest = music - low
    # broadband envelope of the vocal, smoothed like a real sidechain compressor
    env = envelope(vocal, sr, attack_ms=12.0, release_ms=280.0)
    gate = np.clip((env - 0.04) / 0.30, 0.0, 1.0)
    gain = np.power(10.0, (-depth_db * gate) / 20.0).astype(np.float32)
    # soften the gain curve (10 ms smoothing) to avoid zipper noise
    kernel = np.exp(-np.arange(int(0.02 * sr)) / (0.006 * sr))
    kernel /= kernel.sum()
    gain = np.convolve(gain, kernel, mode="same").astype(np.float32)
    return (music + rest * (gain[:, None] - 1.0)).astype(np.float32)


def clean_vocal(vocal: np.ndarray, sr: int, settings: MixSettings) -> np.ndarray:
    v = to_stereo(vocal)
    v = filter_signal(v, sr, "highpass", settings.vocal_highpass_hz, q=0.707)
    v = deess(v, sr, amount=settings.deess_amount)
    if settings.vocal_reverb_mix > 0:
        v = reverb(v, sr, decay_s=1.1, mix=settings.vocal_reverb_mix, predelay_ms=28)
    return v


def mix_song(vocal: np.ndarray, music: np.ndarray, sr: int = DEFAULT_SR,
             settings: Optional[MixSettings] = None,
             master: bool = True) -> tuple[np.ndarray, dict]:
    s = settings or MixSettings()
    vocal = to_stereo(vocal) if vocal is not None and len(vocal) else np.zeros((1, 2), np.float32)
    music = to_stereo(music)
    n = max(len(vocal) + int(s.tail_s * sr), int(len(music)))
    vocal = _fit(vocal, n, sr, fade_ms=8.0)
    music = _fit(music, n, sr, fade_ms=120.0)

    clean = clean_vocal(vocal, sr, s)
    clean = normalize_lufs(clean, sr, -18.0)           # neutral internal level
    music_leveled = normalize_lufs(music, sr, -16.0)

    report: dict = {
        "vocal": {"rms_db": round(gain_to_db(rms(vocal)), 2),
                  "peak_db": round(peak_db(vocal), 2),
                  "lufs": round(lufs(vocal, sr), 2)},
        "music": {"rms_db": round(gain_to_db(rms(music)), 2),
                  "peak_db": round(peak_db(music), 2),
                  "lufs": round(lufs(music, sr), 2)},
    }

    if s.duck_db > 0:
        ducked = multiband_duck(music_leveled, clean, sr, s.duck_db, s.duck_split_hz)
    else:
        ducked = music_leveled

    vocal_bus = clean * db_to_gain(s.vocal_gain_db)
    music_bus = ducked * db_to_gain(s.music_gain_db)
    pre = vocal_bus + music_bus

    if master:
        # gentle bus glue: a touch of 2:1-ish compression via soft knee
        pre = _soft_compress(pre, sr, threshold_db=-14.0, ratio=1.6)
        out = normalize_lufs(pre, sr, s.target_lufs, max_gain_db=18.0)
        out = limiter(out, sr, ceiling_db=s.ceiling_db)
        out = fade(out, sr, 0.0, s.fade_out_s * 1000.0)
    else:
        out = pre
        top = peak(out)
        if top > 0.99:
            out = out * (0.99 / top)

    report.update({
        "duck_db": s.duck_db,
        "target_lufs": s.target_lufs,
        "out": {"lufs": round(lufs(out, sr), 2),
                "peak_db": round(peak_db(out), 2),
                "duration_s": round(len(out) / sr, 2)},
        "settings": asdict(s),
    })
    return out.astype(np.float32), report


def _soft_compress(x: np.ndarray, sr: int, threshold_db: float = -14.0,
                   ratio: float = 2.0, attack_ms: float = 15.0,
                   release_ms: float = 220.0) -> np.ndarray:
    """Simple feed-forward RMS compressor with a smooth gain curve."""
    mono = np.abs(x).mean(axis=1)
    env = np.convolve(mono, np.exp(-np.arange(int(0.05 * sr)) / (0.02 * sr))
                      / max(1, int(0.05 * sr)), mode="same")
    level_db = 20.0 * np.log10(np.maximum(env, 1e-6))
    over = np.maximum(level_db - threshold_db, 0.0)
    target = level_db - over * (1.0 - 1.0 / max(ratio, 1.0))
    gain_db = target - level_db
    # attack/release smoothing on the gain reduction
    attack = np.exp(-1.0 / max(1e-6, attack_ms / 1000.0 * sr))
    release = np.exp(-1.0 / max(1e-6, release_ms / 1000.0 * sr))
    smoothed = np.empty_like(gain_db)
    state = 0.0
    for i, g in enumerate(gain_db):
        coeff = attack if g < state else release
        state = coeff * state + (1.0 - coeff) * g
        smoothed[i] = state
    return (x * np.power(10.0, smoothed / 20.0)[:, None]).astype(np.float32)


def reference_report(vocal: np.ndarray, music: np.ndarray, out: np.ndarray, sr: int) -> str:
    def line(name, sig):
        return (f"  {name:<7} peak {peak_db(sig):6.2f} dBFS | rms {gain_to_db(rms(sig)):6.2f} dB | "
                f"LUFS {lufs(sig, sr):6.2f}")

    return "\n".join([
        line("vocal", vocal), line("music", music), line("master", out),
        f"  length  {len(out) / sr:6.2f} s   samples {len(out)}",
    ])
