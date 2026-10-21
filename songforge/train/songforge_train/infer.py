"""Turn a vocal into an instrumental with *your* trained checkpoint.

    python -m songforge_train.infer --checkpoint runs/small/best.pt \\
        --vocal input/vocals.wav --out out/instrumental.wav --steps 4

Then either listen to the instrumental, or let it be mixed with the vocal:

    songforge mix input/vocals.wav out/instrumental.wav -o out/song.wav

or in one go (``--song``), which also analyses timing/loudness and masters it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()

# Allow both `python -m songforge_train.infer` and
# `python train/songforge_train/infer.py` (the latter is what the
# songforge `external` backend invokes).
if __package__ in (None, ""):
    sys.path.insert(0, str(_HERE.parents[1]))
    __package__ = "songforge_train"

# make `songforge` importable when running from a checkout (parents[2] == songforge/)
sys.path.insert(0, str(_HERE.parents[2]))

import numpy as np  # noqa: E402

try:
    import torch  # noqa: E402
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"inference needs PyTorch: {exc}")

from songforge.audio import DEFAULT_SR, read_audio, resample, to_mono, write_audio  # noqa: E402

from .codec import mel_frames_to_seconds, vocode  # noqa: E402
from .dataset import FPS, N_MELS, SR, SECTION_LABELS, vocal_features  # noqa: E402
from .model import VocalToMusicUNet, sample  # noqa: E402


def build_cond(features: dict, lo: int, hi: int, n_frames: int) -> np.ndarray:
    """Assemble the (COND_CHANNELS, n_frames) conditioning block for a window."""
    sl = slice(lo, hi)
    chroma = features["chroma"][sl]
    beat = features["beat"][sl].astype(np.float32)
    energy = features["energy"][sl]
    section = features["section"][sl].astype(np.int64)
    key = np.zeros((len(chroma), 12), dtype=np.float32)
    key[:, int(features["key_root"]) % 12] = 1.0
    mode = np.zeros((len(chroma), 2), dtype=np.float32)
    mode[:, int(features["key_mode"])] = 1.0
    bpm = np.full((len(chroma), 1), (float(features["bpm"]) - 70.0) / 100.0, np.float32)
    sec = np.zeros((len(chroma), len(SECTION_LABELS)), dtype=np.float32)
    sec[np.arange(len(chroma)), np.clip(section, 0, len(SECTION_LABELS) - 1)] = 1.0
    cond = np.concatenate([chroma, beat.astype(np.float32), energy, sec, key, mode, bpm], axis=1)
    if len(cond) < n_frames:
        cond = np.pad(cond, ((0, n_frames - len(cond)), (0, 0)))
    return np.ascontiguousarray(cond.T)


@torch.no_grad()
def generate_mel(model, features: dict, steps: int = 4, window_seconds: float = 12.0,
                 overlap_seconds: float = 1.0, device: str = "cpu",
                 sigma_start: float = 1.0, sigma_end: float = 0.08,
                 generator: torch.Generator | None = None) -> torch.Tensor:
    """Sliding-window generation with crossfaded overlap-add in the mel domain."""
    vocal = torch.from_numpy(features["vocal_mel"].T).unsqueeze(0).to(device)  # (1, 80, T)
    total = vocal.shape[-1]
    win = max(64, int(window_seconds * FPS))
    hop = max(32, int((window_seconds - overlap_seconds) * FPS))
    out = torch.zeros(1, N_MELS, total, device=device)
    norm = torch.zeros(total, device=device)

    starts = list(range(0, max(1, total - win + hop), hop))
    if not starts:
        starts = [0]
    for i, start in enumerate(starts):
        end = min(total, start + win)
        seg = vocal[:, :, start:end]
        n = seg.shape[-1]
        if n < win:
            seg = torch.nn.functional.pad(seg, (0, win - n))
        cond = torch.from_numpy(build_cond(features, start, end, win)).unsqueeze(0).to(device)
        mel = sample(model, seg, cond, sigma_start=sigma_start, sigma_end=sigma_end,
                     steps=steps, generator=generator)
        mel = mel[:, :, :n]
        fade = torch.ones(n, device=device)
        k = min(n, int(overlap_seconds * FPS))
        if k > 1 and i > 0:
            fade[:k] = torch.linspace(0, 1, k, device=device)
        if k > 1 and i < len(starts) - 1:
            fade[-k:] = torch.linspace(1, 0, k, device=device)
        out[:, :, start:end] += mel * fade
        norm[start:end] += fade
        print(f"  window {i + 1}/{len(starts)}  {start / FPS:6.1f}s - {end / FPS:6.1f}s")
    out /= norm.clamp(min=1e-4)
    return out.clamp(-1.0, 1.0)


def load_model(checkpoint: str, device: str = "cpu", use_ema: bool = True):
    ckpt = torch.load(checkpoint, map_location="cpu")
    cfg = ckpt.get("config", {})
    model = VocalToMusicUNet(channels=tuple(cfg.get("channels", [64, 128, 256, 384])),
                             blocks=int(cfg.get("blocks", 2)),
                             emb_dim=int(cfg.get("emb_dim", 256)))
    state = ckpt.get("ema") if use_ema and ckpt.get("ema") else ckpt["model"]
    model.load_state_dict(state)
    model.to(device).eval()
    return model, cfg, int(ckpt.get("step", 0))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Vocal -> instrumental with a trained checkpoint")
    ap.add_argument("--checkpoint", required=True)
    ap.add_argument("--vocal", required=True, help="clean vocal stem")
    ap.add_argument("--out", required=True, help="instrumental wav to write")
    ap.add_argument("--song", default=None,
                    help="also write a mixed+mastered song here (uses songforge.mix)")
    ap.add_argument("--steps", type=int, default=4, help="refinement steps")
    ap.add_argument("--sigma-end", type=float, default=0.08)
    ap.add_argument("--window", type=float, default=12.0)
    ap.add_argument("--overlap", type=float, default=1.0)
    ap.add_argument("--vocoder", default="griffin-lim", choices=["griffin-lim", "hifigan"])
    ap.add_argument("--vocoder-ckpt", default=None)
    ap.add_argument("--gl-iter", type=int, default=64)
    ap.add_argument("--device", default=None)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--max-seconds", type=float, default=None)
    ap.add_argument("--prompt", default="", help="style hint, stored with the features")
    ap.add_argument("--no-ema", action="store_true")
    ap.add_argument("--use-ema", action="store_true",
                    help="(default) prefer EMA weights when present")
    args = ap.parse_args(argv)

    device = args.device or ("cuda" if torch.cuda.is_available() else "cpu")
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    gen = torch.Generator(device=device).manual_seed(args.seed)

    vocal, sr = read_audio(args.vocal, sr=DEFAULT_SR)
    if args.max_seconds:
        vocal = vocal[: int(args.max_seconds * sr)]
    print(f"vocal: {len(vocal) / sr:.1f}s @ {sr} Hz")

    features = vocal_features(to_mono(resample(vocal, sr, SR)), SR, prompt=args.prompt)
    print(f"features: {features['vocal_mel'].shape[0]} mel frames "
          f"({mel_frames_to_seconds(features['vocal_mel'].shape[0]):.1f}s), "
          f"{float(features['bpm']):.1f} bpm")

    model, cfg, step = load_model(args.checkpoint, device, use_ema=not args.no_ema)
    print(f"model: {args.checkpoint} (step {step}, "
          f"{sum(p.numel() for p in model.parameters()) / 1e6:.1f}M params)")

    mel = generate_mel(model, features, steps=args.steps, window_seconds=args.window,
                       overlap_seconds=args.overlap, device=device,
                       sigma_end=args.sigma_end, generator=gen)
    print("vocoding ...")
    audio = vocode(mel, kind=args.vocoder, n_iter=args.gl_iter, device=device,
                   vocoder_ckpt=args.vocoder_ckpt)
    audio = audio.cpu().numpy().reshape(-1)
    audio44 = resample(audio, SR, DEFAULT_SR)
    write_audio(args.out, audio44, DEFAULT_SR)
    print(f"wrote {args.out}")

    if args.song:
        from songforge.mix import MixSettings, mix_song
        mixed, report = mix_song(vocal, np.stack([audio44, audio44], axis=1), DEFAULT_SR,
                                 settings=MixSettings())
        write_audio(args.song, mixed, DEFAULT_SR)
        print(f"wrote {args.song} ({report['out']['lufs']:.1f} LUFS)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
