"""Dataset preparation for vocal-conditioned accompaniment generation.

Input layouts accepted:

1. **Paired stems** (best, what you should collect)::

       dataset_raw/
         track_0001/{vocals.wav, accompaniment.wav}   # or drums/bass/other
         track_0002/...

   ``accompaniment.wav`` is optional if ``drums.wav`` / ``bass.wav`` /
   ``other.wav`` exist - they are summed.

2. **Full songs** (convenience)::

       songs/
         track_0001.wav ...
         track_0002.wav ...

   Each song is split with Demucs (needs ``pip install demucs``).

Everything is cached to ``<cache>/<track>.npz`` as float16 mel/label tensors so
training never touches audio again.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

# repo layout: songforge/songforge (package) and songforge/train (this file)
_PKG_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PKG_ROOT))

from songforge.analyze import (NOTE_NAMES, analyze_vocal, chroma,  # noqa: E402
                               )
from songforge.audio import read_audio, to_mono, to_stereo  # noqa: E402

SR = 22050
N_FFT = 2048
HOP = 512
N_MELS = 80
FPS = SR / HOP            # 43.07 frames per second
SECTION_LABELS = ["intro", "verse", "prechorus", "chorus", "bridge", "outro"]


# --------------------------------------------------------------------------- #
# feature extraction (numpy; mirrors songforge.analyze)
# --------------------------------------------------------------------------- #
def mel_filterbank(sr: int = SR, n_fft: int = N_FFT, n_mels: int = N_MELS,
                   fmin: float = 40.0, fmax: float = None) -> np.ndarray:
    fmax = fmax or sr / 2
    def hz_to_mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)
    def mel_to_hz(m):
        return 700.0 * (10.0 ** (m / 2595.0) - 1.0)

    mels = np.linspace(hz_to_mel(fmin), hz_to_mel(fmax), n_mels + 2)
    freqs = mel_to_hz(mels)
    bins = np.floor((n_fft + 1) * freqs / sr).astype(int)
    fb = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float32)
    for m in range(1, n_mels + 1):
        left, center, right = bins[m - 1], bins[m], bins[m + 1]
        if center == left:
            center += 1
        if right == center:
            right += 1
        fb[m - 1, left:center] = np.linspace(0, 1, center - left, dtype=np.float32)
        fb[m - 1, center:right] = np.linspace(1, 0, right - center, dtype=np.float32)
    fb /= np.maximum(fb.sum(axis=1, keepdims=True), 1e-9)
    return fb


def log_mel(x: np.ndarray, sr: int, fb: np.ndarray | None = None,
            n_fft: int = N_FFT, hop: int = HOP) -> np.ndarray:
    """(T, n_mels) log-mel of a mono signal, normalised to ~[-1, 1]."""
    fb = mel_filterbank(sr, n_fft, N_MELS) if fb is None else fb
    mono = to_mono(np.asarray(x, dtype=np.float32))
    pad = n_fft // 2
    padded = np.pad(mono, (pad, pad + hop), mode="reflect" if len(mono) > pad else "constant")
    n_frames = 1 + (len(padded) - n_fft) // hop
    frames = np.lib.stride_tricks.as_strided(
        padded, shape=(n_frames, n_fft),
        strides=(padded.strides[0] * hop, padded.strides[0])).copy()
    spec = np.abs(np.fft.rfft(frames * np.hanning(n_fft).astype(np.float32), axis=1))
    mel = spec @ fb.T
    db = 20.0 * np.log10(np.maximum(mel, 1e-8))
    # normalise per track: 0 dB = loudest frame, floor at -80 dB.  Absolute level
    # is meaningless for a generative target and would leak the master gain.
    db = db - np.max(db)
    db = np.clip(db, -80.0, 0.0)
    mel_norm = (db + 80.0) / 40.0 - 1.0
    return np.clip(mel_norm, -1.0, 1.0).astype(np.float32)  # [-1, 1]


def mel_to_db(mel: np.ndarray) -> np.ndarray:
    """Inverse of the encoding in ``log_mel``: [-1, 1] -> [-80, 0] dB (relative)."""
    return (np.asarray(mel) + 1.0) * 40.0 - 80.0


# --------------------------------------------------------------------------- #
# per-track feature bundle
# --------------------------------------------------------------------------- #
def _section_ids(cond, n_frames: int) -> np.ndarray:
    ids = np.ones(n_frames, dtype=np.int64)              # default: verse
    for s in cond.sections:
        lo = int(s.start * FPS)
        hi = int(np.ceil(s.end * FPS))
        label = s.label if s.label in SECTION_LABELS else "verse"
        ids[max(0, lo):min(n_frames, hi)] = SECTION_LABELS.index(label)
    return ids


def _beat_features(cond, n_frames: int) -> np.ndarray:
    """sin/cos of the beat phase at every frame (2 values)."""
    t = np.arange(n_frames) / FPS
    beat = 60.0 / max(cond.bpm, 30.0)
    phase = 2.0 * np.pi * (t - (cond.beat_phase or 0.0)) / beat
    return np.stack([np.sin(phase), np.cos(phase)], axis=1).astype(np.float32)


def _energy_curve(vocal: np.ndarray, n_frames: int, hop: int = HOP) -> np.ndarray:
    mono = np.abs(to_mono(vocal))
    win = hop * 2
    idx = np.arange(win)[None, :] + hop * np.arange(n_frames)[:, None]
    idx = np.clip(idx, 0, len(mono) - 1)
    frame_rms = np.sqrt(np.mean(mono[idx] ** 2, axis=1))
    db = 20.0 * np.log10(np.maximum(frame_rms, 1e-6))
    db = np.clip((db + 60.0) / 60.0, 0.0, 1.0)
    return db[:, None].astype(np.float32)


def vocal_features(vocals: np.ndarray, sr: int = SR, prompt: str = "") -> dict:
    """Everything the model may see about the vocal (no target needed).

    Used both when building the training cache and at inference time, so the
    two code paths can never drift apart.
    """
    vocals = to_mono(np.asarray(vocals, dtype=np.float32))
    cond = analyze_vocal(vocals, sr, prompt=prompt)
    vocal_mel = log_mel(vocals, sr)
    n_frames = len(vocal_mel)

    chroma_frames = chroma(vocals, sr, n_fft=4096, hop=HOP)
    if len(chroma_frames) < n_frames:
        chroma_frames = np.pad(chroma_frames, ((0, n_frames - len(chroma_frames)), (0, 0)),
                               mode="edge")
    chroma_frames = chroma_frames[:n_frames].astype(np.float32)

    beat = _beat_features(cond, n_frames)
    energy = _energy_curve(vocals, n_frames)
    sections = _section_ids(cond, n_frames)
    return {
        "vocal_mel": vocal_mel.astype(np.float32),
        "chroma": chroma_frames.astype(np.float32),
        "beat": beat.astype(np.float16),
        "energy": energy.astype(np.float32),
        "section": sections.astype(np.int8),
        "bpm": np.float32(cond.bpm),
        "key_root": np.int16(cond.key_root),
        "key_mode": np.int8(0 if cond.key_mode == "major" else 1),
        "key_conf": np.float32(cond.key_confidence),
        "duration": np.float32(len(vocals) / sr),
        "sr": np.int32(sr),
        "fps": np.float32(FPS),
        "source": np.array(prompt),
    }


def analyze_track(vocals: np.ndarray, accompaniment: np.ndarray, sr: int = SR,
                  prompt: str = "") -> dict:
    """Feature bundle for one (vocal, accompaniment) training pair."""
    vocals = to_mono(np.asarray(vocals, dtype=np.float32))
    accompaniment = to_mono(np.asarray(accompaniment, dtype=np.float32))
    n = min(len(vocals), len(accompaniment))
    features = vocal_features(vocals[:n], sr, prompt=prompt)
    target_mel = log_mel(accompaniment[:n], sr)
    n_frames = min(len(features["vocal_mel"]), len(target_mel))
    for key in ("vocal_mel", "chroma", "beat", "energy", "section"):
        features[key] = features[key][:n_frames]
    features["target_mel"] = target_mel[:n_frames].astype(np.float16)
    features["duration"] = np.float32(n / sr)
    return features


# --------------------------------------------------------------------------- #
# building the cache
# --------------------------------------------------------------------------- #
def _load_pair(entry: Path, cache_dir: Path, demucs_model: str, device: str) -> tuple:
    if entry.is_dir():
        vocals_path = entry / "vocals.wav"
        if not vocals_path.exists():
            vocals_path = entry / "vocal.wav"
        if not vocals_path.exists():
            raise FileNotFoundError(f"{entry} has no vocals.wav")
        acc_path = entry / "accompaniment.wav"
        if acc_path.exists():
            acc, _ = read_audio(str(acc_path), sr=SR)
            return vocals_path, acc
        parts = [entry / f"{s}.wav" for s in ("drums", "bass", "other")
                 if (entry / f"{s}.wav").exists()]
        if not parts:
            raise FileNotFoundError(f"{entry} has neither accompaniment.wav nor drums/bass/other")
        acc = None
        for p in parts:
            a, _ = read_audio(str(p), sr=SR)
            acc = a if acc is None else _pad_add(acc, a)
        return vocals_path, acc

    # a full song -> separate first
    from songforge.separate import separate_song
    stems = separate_song(str(entry), str(cache_dir / "_demucs"), model=demucs_model,
                          device=device, two_stems="vocals")
    vocal, _ = read_audio(stems["vocals"], sr=SR)
    acc_path = stems.get("accompaniment")
    acc, _ = read_audio(acc_path, sr=SR) if acc_path else (None, None)
    return Path(stems["vocals"]), acc


def _pad_add(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    n = max(len(a), len(b))
    out = np.zeros((n, max(a.shape[1], b.shape[1])), dtype=np.float32)
    out[:len(a), :a.shape[1]] += a
    out[:len(b), :b.shape[1]] += b
    return out


def build_cache(raw_dir: str, cache_dir: str, min_seconds: float = 10.0,
                demucs_model: str = "htdemucs_ft", device: str = "cpu",
                skip_existing: bool = True) -> dict:
    raw = Path(raw_dir)
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    entries = sorted([p for p in raw.iterdir() if p.is_dir() or p.suffix.lower()
                      in (".wav", ".flac", ".mp3", ".ogg", ".m4a")])
    manifest, skipped, failed = [], 0, []
    for i, entry in enumerate(entries):
        name = entry.stem if entry.is_file() else entry.name
        out_path = cache / f"{name}.npz"
        if skip_existing and out_path.exists():
            skipped += 1
            manifest.append({"name": name, "file": out_path.name, "cached": True})
            continue
        try:
            vocals_path, acc = _load_pair(entry, cache, demucs_model, device)
            if acc is None:
                raise RuntimeError("no accompaniment")
            vocals, _ = read_audio(str(vocals_path), sr=SR)
            acc = to_stereo(acc)
            if len(acc) / SR < min_seconds:
                raise RuntimeError(f"too short ({len(acc) / SR:.1f}s)")
            bundle = analyze_track(vocals, acc, SR, prompt=name)
            np.savez_compressed(out_path, **bundle)
            manifest.append({"name": name, "file": out_path.name,
                             "frames": int(len(bundle["vocal_mel"])),
                             "duration": float(bundle["duration"]),
                             "bpm": float(bundle["bpm"]),
                             "key": f"{NOTE_NAMES[int(bundle['key_root'])]} "
                                    f"{'major' if int(bundle['key_mode']) == 0 else 'minor'}"})
            print(f"[{i + 1}/{len(entries)}] {name}: "
                  f"{float(bundle['duration']):.1f}s, {float(bundle['bpm']):.1f} bpm")
        except Exception as exc:  # keep going; report at the end
            failed.append({"name": name, "error": str(exc)})
            print(f"[{i + 1}/{len(entries)}] {name}: FAILED ({exc})")

    with open(cache / "manifest.json", "w", encoding="utf-8") as fh:
        json.dump({"tracks": manifest, "failed": failed, "skipped": skipped,
                   "sr": SR, "hop": HOP, "n_mels": N_MELS, "fps": FPS}, fh, indent=2)
    print(f"cached {len(manifest)} tracks ({skipped} already present), "
          f"{len(failed)} failed -> {cache / 'manifest.json'}")
    return {"tracks": manifest, "failed": failed}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("raw", help="folder of paired stem folders, or of full songs")
    ap.add_argument("cache", help="where to write .npz feature files")
    ap.add_argument("--min-seconds", type=float, default=10.0)
    ap.add_argument("--demucs-model", default="htdemucs_ft")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--force", action="store_true", help="recompute existing entries")
    args = ap.parse_args(argv)
    build_cache(args.raw, args.cache, args.min_seconds, args.demucs_model,
                args.device, skip_existing=not args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
