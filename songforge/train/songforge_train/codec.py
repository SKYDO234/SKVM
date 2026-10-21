"""Mel -> waveform: Griffin-Lim now, HiFi-GAN when you have one.

Griffin-Lim is slow-ish and slightly metallic, but it is 30 lines, dependency
free (torch only) and good enough to *hear whether the model learned anything*.
For release quality, train or download a HiFi-GAN vocoder and pass it as
``--vocoder hifigan --vocoder-ckpt path.pt``; see ``HiFiGANVocoder`` stub.
"""

from __future__ import annotations

import math

import numpy as np
import torch

from .dataset import HOP, N_FFT, SR, mel_filterbank


def _mel_pinv(device="cpu", dtype=torch.float32) -> torch.Tensor:
    fb = mel_filterbank(SR, N_FFT, 80)
    pinv = np.linalg.pinv(fb)                     # (n_bins, n_mels)
    return torch.from_numpy(pinv.astype(np.float32)).to(device=device, dtype=dtype)


def mel_to_linear(mel: torch.Tensor) -> torch.Tensor:
    """Approximate linear magnitude spectrogram from a normalised log-mel.

    ``mel``: (B, 80, T) in [-1, 1]  ->  (B, n_bins, T) magnitudes
    """
    db = (mel + 1.0) * 40.0 - 80.0
    mag = torch.clamp(10.0 ** (db / 20.0), min=0.0)
    pinv = _mel_pinv(mel.device, mel.dtype)
    return torch.clamp(mag.transpose(1, 2) @ pinv.T, min=0.0).transpose(1, 2)


def _istft(spec: torch.Tensor, window: torch.Tensor, n_fft: int, hop: int,
           n_samples: int) -> torch.Tensor:
    """istft + exact length fix (torch.istft refuses to pad past its natural length)."""
    audio = torch.istft(spec, n_fft=n_fft, hop_length=hop, window=window, center=True)
    if audio.shape[-1] < n_samples:
        audio = torch.nn.functional.pad(audio, (0, n_samples - audio.shape[-1]))
    return audio[..., :n_samples]


@torch.no_grad()
def griffin_lim(mel: torch.Tensor, n_iter: int = 64, momentum: float = 0.99,
                hop: int = HOP, n_fft: int = N_FFT) -> torch.Tensor:
    """Phase reconstruction. Returns (B, samples) audio at ``SR``."""
    mag = mel_to_linear(mel)
    b, n_bins, t = mag.shape
    window = torch.hann_window(n_fft, device=mel.device, dtype=mag.dtype)
    n_samples = (t - 1) * hop          # exactly what center=True istft needs
    spec = mag * torch.exp(2j * math.pi * torch.rand(b, n_bins, t, device=mag.device))
    rebuilt = _istft(spec, window, n_fft, hop, n_samples)
    prev = torch.zeros_like(rebuilt)
    for _ in range(n_iter):
        stft = torch.stft(rebuilt, n_fft=n_fft, hop_length=hop, window=window,
                          center=True, return_complex=True)[..., :t]
        phase = stft / torch.clamp(stft.abs(), min=1e-8)
        target = mag * phase
        audio = _istft(target, window, n_fft, hop, n_samples)
        # fast Griffin-Lim momentum term
        rebuilt = audio + momentum * (audio - prev)
        prev = audio
    peak = rebuilt.abs().amax(dim=1, keepdim=True).clamp(min=1e-6)
    return (rebuilt / peak * 0.9).clamp(-1.0, 1.0)


class HiFiGANVocoder:
    """Adapter for a trained HiFi-GAN generator (expects mel in [-1, 1])."""

    def __init__(self, ckpt_path: str, device: str = "cpu"):
        self.device = device
        self.generator = self._load(ckpt_path).to(device).eval()

    def _load(self, ckpt_path: str):
        try:
            from .hifigan import Generator  # optional, bring your own architecture
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(
                "HiFi-GAN weights need a matching generator definition; drop your "
                "architecture in songforge_train/hifigan.py and re-run.") from exc
        state = torch.load(ckpt_path, map_location="cpu")
        state = state.get("generator", state)
        model = Generator()
        model.load_state_dict(state, strict=False)
        return model

    @torch.no_grad()
    def __call__(self, mel: torch.Tensor, **_: object) -> torch.Tensor:
        return self.generator(mel.to(self.device)).squeeze(1)


def vocode(mel: torch.Tensor, kind: str = "griffin-lim", n_iter: int = 64,
           device: str = "cpu", vocoder_ckpt: str | None = None) -> torch.Tensor:
    if kind in ("griffin-lim", "griffin_lim", "gl"):
        return griffin_lim(mel, n_iter=n_iter)
    if kind == "hifigan":
        if not vocoder_ckpt:
            raise SystemExit("--vocoder hifigan needs --vocoder-ckpt")
        return HiFiGANVocoder(vocoder_ckpt, device)(mel)
    raise SystemExit(f"unknown vocoder {kind!r}")


def mel_frames_to_seconds(n_frames: int, fps: float = SR / HOP) -> float:
    return n_frames / fps


def seconds_to_mel_frames(seconds: float, fps: float = SR / HOP) -> int:
    return int(round(seconds * fps))
