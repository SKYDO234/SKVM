"""Audio I/O and DSP primitives.

Only numpy is required.  ``soundfile`` and ``ffmpeg`` are used when available for
compressed formats; plain WAV works with nothing but the standard library.

Every signal here is a float32 array shaped ``(samples, channels)``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import wave
from typing import Optional

import numpy as np

DEFAULT_SR = 44100
EPS = 1e-12


# --------------------------------------------------------------------------- #
# basic conversions
# --------------------------------------------------------------------------- #
def db_to_gain(db: float) -> float:
    return float(10.0 ** (np.asarray(db, dtype=np.float64) / 20.0))


def gain_to_db(gain: float, floor: float = 1e-7) -> float:
    return float(20.0 * np.log10(max(float(gain), floor)))


def to_mono(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    return x.mean(axis=1) if x.ndim > 1 else x


def to_stereo(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    if x.ndim == 1:
        x = x[:, None]
    if x.shape[1] == 1:
        return np.repeat(x, 2, axis=1)
    return x[:, :2]


def peak(x: np.ndarray) -> float:
    return float(np.max(np.abs(x))) if x.size else 0.0


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(np.asarray(x, dtype=np.float64))))) if x.size else 0.0


def peak_db(x: np.ndarray) -> float:
    return gain_to_db(peak(x))


def fade(x: np.ndarray, sr: int, fade_in_ms: float = 0.0, fade_out_ms: float = 0.0) -> np.ndarray:
    """Apply raised-cosine fades in place-safe copy."""
    y = np.array(x, dtype=np.float32, copy=True)
    n = y.shape[0]
    for ms, at_end in ((fade_in_ms, False), (fade_out_ms, True)):
        k = min(int(sr * ms / 1000.0), n)
        if k <= 1:
            continue
        ramp = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, k, dtype=np.float32))
        if at_end:
            y[-k:] *= ramp[::-1, None] if y.ndim > 1 else ramp[::-1]
        else:
            y[:k] *= ramp[:, None] if y.ndim > 1 else ramp
    return y


def pad_to(x: np.ndarray, n: int, tail_fade_ms: float = 30.0, sr: int = DEFAULT_SR) -> np.ndarray:
    """Pad with silence (fading the existing tail if we are cutting)."""
    x = np.asarray(x, dtype=np.float32)
    if len(x) >= n:
        return fade(x[:n], sr, 0.0, tail_fade_ms)
    pad = np.zeros((n - len(x),) + x.shape[1:], dtype=np.float32)
    return np.concatenate([x, pad], axis=0)


def add_at(buf: np.ndarray, sig: np.ndarray, start: int, gain: float = 1.0) -> None:
    """Mix ``sig`` into ``buf`` at sample ``start``, clipped to the buffer."""
    if start < 0:
        sig = sig[-start:]
        start = 0
    if start >= len(buf) or len(sig) == 0:
        return
    n = min(len(sig), len(buf) - start)
    src = sig[:n]
    if src.ndim == 1 and buf.ndim > 1:
        src = src[:, None]
    buf[start:start + n] += src * gain


# --------------------------------------------------------------------------- #
# resampling
# --------------------------------------------------------------------------- #
def _fft_resample_channel(x: np.ndarray, n_out: int) -> np.ndarray:
    n_in = len(x)
    if n_out == n_in:
        return x.astype(np.float32)
    spec = np.fft.rfft(x.astype(np.float64))
    if n_out < n_in:
        keep = n_out // 2 + 1
        spec = spec[:keep].copy()
        if n_out % 2 == 0:
            spec[-1] = spec[-1].real
    else:
        padded = np.zeros(n_out // 2 + 1, dtype=np.complex128)
        padded[:len(spec)] = spec
        spec = padded
    y = np.fft.irfft(spec, n=n_out) * (n_out / n_in)
    return y.astype(np.float32)


def resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """Band-limited resampling via FFT (offline, high quality)."""
    if sr_in == sr_out or x.size == 0:
        return np.asarray(x, dtype=np.float32)
    x = np.asarray(x, dtype=np.float32)
    mono = x.ndim == 1
    if mono:
        x = x[:, None]
    n_out = max(1, int(round(len(x) * sr_out / sr_in)))
    out = np.empty((n_out, x.shape[1]), dtype=np.float32)
    for ch in range(x.shape[1]):
        out[:, ch] = _fft_resample_channel(x[:, ch], n_out)
    return out[:, 0] if mono else out


# --------------------------------------------------------------------------- #
# filtering (frequency-domain biquad application - zero phase, offline)
# --------------------------------------------------------------------------- #
def _apply_response(x: np.ndarray, response: np.ndarray) -> np.ndarray:
    """Filter a whole channel by a complex response defined on rfft bins."""
    n = len(x)
    return np.fft.irfft(np.fft.rfft(x.astype(np.float64)) * response, n=n).astype(np.float32)


def biquad_response(b, a, n: int) -> np.ndarray:
    """Frequency response of a biquad at ``n`` sample points."""
    w = 2.0 * np.pi * np.arange(n // 2 + 1) / n
    z1 = np.exp(-1j * w)
    z2 = z1 * z1
    return (b[0] + b[1] * z1 + b[2] * z2) / (a[0] + a[1] * z1 + a[2] * z2)


def peaking_coeffs(f0: float, gain_db: float, q: float, sr: int):
    """RBJ audio-EQ-cookbook peaking EQ."""
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * f0 / sr
    alpha = np.sin(w0) / (2.0 * q)
    b = [1 + alpha * A, -2 * np.cos(w0), 1 - alpha * A]
    a = [1 + alpha / A, -2 * np.cos(w0), 1 - alpha / A]
    return b, a


def highpass_coeffs(f0: float, q: float, sr: int):
    w0 = 2.0 * np.pi * f0 / sr
    alpha = np.sin(w0) / (2.0 * q)
    c = np.cos(w0)
    b = [(1 + c) / 2, -(1 + c), (1 + c) / 2]
    a = [1 + alpha, -2 * c, 1 - alpha]
    return b, a


def lowpass_coeffs(f0: float, q: float, sr: int):
    w0 = 2.0 * np.pi * f0 / sr
    alpha = np.sin(w0) / (2.0 * q)
    c = np.cos(w0)
    b = [(1 - c) / 2, 1 - c, (1 - c) / 2]
    a = [1 + alpha, -2 * c, 1 - alpha]
    return b, a


def filter_signal(x, sr: int, kind: str, f0: float, gain_db: float = 0.0,
                  q: float = 0.707) -> np.ndarray:
    """Zero-phase biquad filtering of a signal (mono or stereo)."""
    if kind == "peak":
        b, a = peaking_coeffs(f0, gain_db, q, sr)
    elif kind == "highpass":
        b, a = highpass_coeffs(f0, q, sr)
    elif kind == "lowpass":
        b, a = lowpass_coeffs(f0, q, sr)
    else:
        raise ValueError(f"unknown filter {kind!r}")
    x = np.asarray(x, dtype=np.float32)
    mono = x.ndim == 1
    if mono:
        x = x[:, None]
    n = len(x)
    h = biquad_response(b, a, n)
    out = np.stack([_apply_response(x[:, c], h) for c in range(x.shape[1])], axis=1)
    return out[:, 0] if mono else out


# --------------------------------------------------------------------------- #
# loudness (ITU-R BS.1770-4, gated integrated LUFS)
# --------------------------------------------------------------------------- #
def _k_weighting_response(sr: int, n: int) -> np.ndarray:
    """BS.1770 K-weighting as an analytic frequency response."""
    # stage 1: high shelf
    f0, G, Q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    K = np.tan(np.pi * f0 / sr)
    Vh = 10.0 ** (G / 20.0)
    Vb = Vh ** 0.4996667741545416
    a0 = 1.0 + K / Q + K * K
    b1 = [(Vh + Vb * K / Q + K * K) / a0, 2.0 * (K * K - Vh) / a0, (Vh - Vb * K / Q + K * K) / a0]
    a1 = [1.0, 2.0 * (K * K - 1.0) / a0, (1.0 - K / Q + K * K) / a0]
    # stage 2: high pass
    f0, Q = 38.13547087602444, 0.5003270373238773
    K = np.tan(np.pi * f0 / sr)
    a2 = [1.0, 2.0 * (K * K - 1.0) / (1.0 + K / Q + K * K),
          (1.0 - K / Q + K * K) / (1.0 + K / Q + K * K)]
    b2 = [1.0, -2.0, 1.0]
    return biquad_response(b1, a1, n) * biquad_response(b2, a2, n)


def lufs(x: np.ndarray, sr: int = DEFAULT_SR) -> float:
    """Gated integrated loudness in LUFS."""
    mono = to_mono(x).astype(np.float64)
    if mono.size < int(0.4 * sr):
        return gain_to_db(np.sqrt(np.mean(mono ** 2)), floor=1e-12) - 0.691
    weighted = np.fft.irfft(
        np.fft.rfft(mono) * _k_weighting_response(sr, len(mono)), n=len(mono))
    block, hop = int(0.4 * sr), int(0.1 * sr)
    csum = np.concatenate([[0.0], np.cumsum(weighted ** 2)])
    starts = np.arange(0, len(weighted) - block + 1, hop)
    if starts.size == 0:
        starts = np.array([0])
        block = min(block, len(weighted))
    ms = (csum[starts + block] - csum[starts]) / block
    ms = np.maximum(ms, 1e-20)
    loud = -0.691 + 10.0 * np.log10(ms)
    gated = loud > -70.0
    if not np.any(gated):
        return float(loud.max())
    rel = -0.691 + 10.0 * np.log10(np.mean(ms[gated])) - 10.0
    gated &= loud > rel
    if not np.any(gated):
        gated = loud > -70.0
    return float(-0.691 + 10.0 * np.log10(np.mean(ms[gated])))


def normalize_lufs(x: np.ndarray, sr: int, target_lufs: float,
                   max_gain_db: float = 24.0) -> np.ndarray:
    current = lufs(x, sr)
    if not np.isfinite(current):
        return x
    gain_db = float(np.clip(target_lufs - current, -max_gain_db, max_gain_db))
    return (x * db_to_gain(gain_db)).astype(np.float32)


# --------------------------------------------------------------------------- #
# dynamics / effects
# --------------------------------------------------------------------------- #
def _fft_convolve(x: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    n = len(x) + len(kernel) - 1
    size = 1 << (n - 1).bit_length()
    out = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(kernel, size), n=size)
    return out[:len(x)].astype(np.float32)


def _exp_kernel(tau_s: float, sr: int) -> np.ndarray:
    n = max(2, int(tau_s * sr * 4))
    t = np.arange(n) / sr
    k = np.exp(-t / max(tau_s, 1e-3))
    return (k / k.sum()).astype(np.float32)


def envelope(x: np.ndarray, sr: int, attack_ms: float = 8.0,
             release_ms: float = 120.0) -> np.ndarray:
    """Causal-ish peak envelope using fast-attack / slow-release smoothing."""
    mono = np.abs(to_mono(x))
    fast = _fft_convolve(mono, _exp_kernel(attack_ms / 1000.0, sr))
    slow = _fft_convolve(mono, _exp_kernel(release_ms / 1000.0, sr))
    env = np.maximum(fast, slow)
    top = float(np.percentile(env, 99.0)) if env.size else 0.0
    return env / top if top > EPS else env


def duck(music: np.ndarray, sidechain: np.ndarray, sr: int, depth_db: float = 3.5,
         attack_ms: float = 10.0, release_ms: float = 260.0) -> np.ndarray:
    """Sidechain-compress the instrumental under the vocal (broadband)."""
    if depth_db <= 0:
        return music
    env = envelope(sidechain, sr, attack_ms, release_ms)
    # only duck where the vocal is actually present
    gate = np.clip((env - 0.05) / 0.35, 0.0, 1.0)
    gain_db = -depth_db * gate
    gain = np.power(10.0, gain_db / 20.0).astype(np.float32)
    gain = _fft_convolve(gain, _exp_kernel(0.01, sr))  # de-zipper
    if music.ndim == 1:
        return music * gain
    return music * gain[:, None]


def reverb(x: np.ndarray, sr: int, decay_s: float = 1.6, mix: float = 0.18,
           predelay_ms: float = 18.0, tone_hz: float = 6500.0, seed: int = 7) -> np.ndarray:
    """Convolution reverb with a synthetic, decorrelated stereo IR."""
    if mix <= 0:
        return x
    x = to_stereo(x)
    rng = np.random.default_rng(seed)
    n = max(8, int(decay_s * sr))
    t = np.arange(n) / sr
    ir = rng.standard_normal((n, 2)).astype(np.float32)
    ir *= np.exp(-6.9078 * t / decay_s)[:, None]  # -60 dB at decay_s
    # cheap spectral tilt: two one-pole lowpass passes
    for _ in range(2):
        ir = np.apply_along_axis(lambda c: np.convolve(c, [0.5, 0.5], mode="same"), 0, ir)
    ir /= np.sqrt(np.sum(ir ** 2, axis=0, keepdims=True)) + EPS
    pre = np.zeros((int(predelay_ms / 1000.0 * sr), 2), dtype=np.float32)
    ir = np.concatenate([pre, ir], axis=0)
    wet = np.stack([_fft_convolve(x[:, c].astype(np.float32), ir[:, c].astype(np.float32))
                    for c in range(2)], axis=1)
    return ((1.0 - mix) * x + mix * wet).astype(np.float32)


def deess(x: np.ndarray, sr: int, amount: float = 0.45, low: float = 5000.0,
          high: float = 9500.0) -> np.ndarray:
    """Split-band de-esser: pull down the 5-9.5 kHz band where it spikes."""
    if amount <= 0:
        return x
    x = to_stereo(x)
    low_band = filter_signal(x, sr, "lowpass", low, q=0.707)
    band = filter_signal(x, sr, "lowpass", high, q=0.707) - low_band
    env = envelope(band, sr, attack_ms=2.0, release_ms=45.0)
    gate = np.clip((env - 0.25) / 0.5, 0.0, 1.0)
    reduction = (1.0 - amount * gate).astype(np.float32)[:, None]
    return (low_band + band * reduction).astype(np.float32)


def limiter(x: np.ndarray, sr: int, ceiling_db: float = -1.0,
            lookahead_ms: float = 6.0, release_ms: float = 90.0) -> np.ndarray:
    """Look-ahead peak limiter.

    Gain is computed at control rate (one point per look-ahead block) and
    linearly interpolated back to audio rate, so no per-sample Python loop.
    """
    x = np.asarray(x, dtype=np.float32)
    if x.size == 0:
        return x
    ceiling = db_to_gain(ceiling_db)
    block = max(1, int(lookahead_ms / 1000.0 * sr))
    mono = np.max(np.abs(x), axis=1) if x.ndim > 1 else np.abs(x)
    n_blocks = int(np.ceil(len(mono) / block))
    padded = np.pad(mono, (0, n_blocks * block - len(mono)))
    peaks = padded.reshape(n_blocks, block).max(axis=1)
    need = np.minimum(1.0, ceiling / np.maximum(peaks, EPS)).astype(np.float64)

    # look ahead: take the minimum gain of the next two blocks
    look = need.copy()
    look[:-1] = np.minimum(look[:-1], need[1:])
    # release smoothing at control rate
    ctrl_rate = sr / block
    coeff = np.exp(-1.0 / max(release_ms / 1000.0 * ctrl_rate, 1e-3))
    smooth = np.empty_like(look)
    prev = 1.0
    for i, target in enumerate(look):
        if target < prev:
            prev = target
        else:
            prev = prev * coeff + target * (1.0 - coeff)
        smooth[i] = prev
    gain = np.interp(np.arange(len(mono)), np.arange(n_blocks) * block,
                     smooth, left=1.0, right=smooth[-1]).astype(np.float32)
    out = x * (gain[:, None] if x.ndim > 1 else gain)
    # last-resort safety clip (never let float overs leave the box)
    return np.clip(out, -ceiling, ceiling)


# --------------------------------------------------------------------------- #
# time / pitch
# --------------------------------------------------------------------------- #
def time_stretch(x: np.ndarray, sr: int, rate: float, frame: int = 2048,
                 hop: int = 512, search: int = 224) -> np.ndarray:
    """WSOLA time stretch without changing pitch.

    ``rate`` is a speed factor (as in librosa): ``rate=1.25`` makes the audio
    25 % shorter; ``rate=0.8`` makes it 25 % longer.
    """
    x = np.asarray(x, dtype=np.float32)
    if abs(rate - 1.0) < 1e-3 or len(x) < 3 * frame:
        return x.copy()
    mono = x.ndim == 1
    if mono:
        x = x[:, None]
    n_in, ch = x.shape
    n_out = max(frame, int(round(n_in / rate)))
    ha = hop * rate          # analysis hop
    win = np.hanning(frame).astype(np.float32)
    out = np.zeros((n_out + 2 * frame, ch), dtype=np.float32)
    norm = np.zeros(n_out + 2 * frame, dtype=np.float32)
    nominal = 0.0
    pos = 0
    prev_tail = None
    while pos + frame < n_out:
        base = int(round(nominal))
        if base + frame + search >= n_in:
            base = max(0, n_in - frame - search - 1)
        if prev_tail is None:
            offset = 0
        else:
            lo = max(0, base - search)
            hi = min(n_in - frame, base + search)
            if hi <= lo:
                offset = 0
            else:
                seg = x[lo:hi + frame - search, 0]
                corr = np.correlate(seg, prev_tail, mode="valid")
                offset = lo + int(np.argmax(corr)) - base
        src = int(np.clip(base + offset, 0, n_in - frame - 1))
        chunk = x[src:src + frame] * win[:, None]
        out[pos:pos + frame] += chunk
        norm[pos:pos + frame] += win
        prev_tail = x[src + hop:src + hop + search, 0] if src + hop + search <= n_in else None
        if prev_tail is not None and len(prev_tail) < search:
            prev_tail = None
        pos += hop
        nominal += ha
    out = out[:n_out]
    norm = norm[:n_out]
    valid = norm > 1e-4
    out[valid] /= norm[valid, None]
    return out[:, 0] if mono else out


def pitch_shift(x: np.ndarray, sr: int, semitones: float) -> np.ndarray:
    """Pitch shift by resampling, then WSOLA-compensating the duration.

    Reading the signal at ``sr * r`` raises every partial by ``r``; the
    time-stretch then restores the original length without touching pitch.
    """
    if abs(semitones) < 1e-3:
        return np.asarray(x, dtype=np.float32)
    r = 2.0 ** (semitones / 12.0)
    y = resample(x, int(round(sr * r)), sr)
    return time_stretch(y, sr, rate=1.0 / r)


# --------------------------------------------------------------------------- #
# file I/O
# --------------------------------------------------------------------------- #
def _read_wav_stdlib(path: str):
    with wave.open(path, "rb") as w:
        channels = w.getnchannels()
        width = w.getsampwidth()
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    if width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        val = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        val = np.where(val >= 1 << 23, val - (1 << 24), val)
        data = val.astype(np.float32) / float(1 << 23)
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise RuntimeError(f"unsupported WAV sample width: {width} bytes")
    return data.reshape(-1, channels), sr


def _ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


def _read_via_ffmpeg(path: str):
    ff = _ffmpeg()
    if not ff:
        raise RuntimeError(
            f"cannot read {path!r}: install `soundfile`+libsndfile, or ffmpeg, "
            "or convert the file to WAV first")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name
    try:
        subprocess.run([ff, "-v", "error", "-y", "-i", path, "-ac", "2",
                        "-c:a", "pcm_s16le", wav_path], check=True)
        return _read_wav_stdlib(wav_path)
    finally:
        os.unlink(wav_path)


def read_audio(path: str, sr: Optional[int] = None):
    """Load any readable audio file as ``(samples, channels)`` float32."""
    data = sr_in = None
    try:
        import soundfile as sf  # type: ignore
        data, sr_in = sf.read(path, dtype="float32", always_2d=True)
    except Exception:
        try:
            data, sr_in = _read_wav_stdlib(path)
        except Exception:
            data, sr_in = _read_via_ffmpeg(path)
    data = np.asarray(data, dtype=np.float32)
    if sr is not None and sr != sr_in:
        data = resample(data, sr_in, sr)
        sr_in = sr
    return data, sr_in


def write_audio(path: str, x: np.ndarray, sr: int = DEFAULT_SR) -> str:
    """Write WAV natively; other formats via ffmpeg (when installed)."""
    x = np.asarray(x, dtype=np.float32)
    if x.ndim == 1:
        x = x[:, None]
    x = np.clip(x, -1.0, 1.0)
    ext = os.path.splitext(path)[1].lower()
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    if ext in (".wav", ".wave"):
        pcm = np.round(x * 32767.0).astype("<i2")
        with wave.open(path, "wb") as w:
            w.setnchannels(pcm.shape[1])
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm.tobytes())
        return path
    ff = _ffmpeg()
    if not ff:
        raise RuntimeError(f"writing {ext} needs ffmpeg; write a .wav instead")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name
    try:
        write_audio(wav_path, x, sr)
        codec = {"mp3": "libmp3lame", "flac": "flac", "ogg": "libvorbis",
                 ".m4a": "aac"}.get(ext.lstrip("."), None)
        cmd = [ff, "-v", "error", "-y", "-i", wav_path]
        if codec:
            cmd += ["-c:a", codec]
        if ext == ".mp3":
            cmd += ["-b:a", "320k"]
        cmd += [path]
        subprocess.run(cmd, check=True)
    finally:
        os.unlink(wav_path)
    return path


def write_json(path: str, payload) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=False)
    return path
