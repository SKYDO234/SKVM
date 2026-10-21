"""The model: a conditional 1-D U-Net that predicts the accompaniment mel.

Task formulation
----------------
    accompaniment_mel  ~  p( . | vocal_mel, chroma, beat, energy, section, key )

Training objective: denoising regression.  A clean target mel ``x0`` is
corrupted with Gaussian noise of a random level ``sigma`` and the network is
trained to recover ``x0``::

    loss = | f(x0 + sigma * eps, sigma, cond) - x0 |_1  (+ temporal smoothness)

At inference the same network is applied iteratively: start from noise at a
high sigma, take the prediction, re-noise it to a lower sigma and predict
again.  It is not a full diffusion sampler, but it is the same "predict the
clean signal, refine" loop and needs one U-Net instead of two networks.

Why mel and not raw audio?  Predicting 22 kHz waveforms directly needs far more
data and compute; a mel target plus a vocoder gets you 90 % of the perceived
quality for a tiny fraction of the cost.  The vocoder lives in ``codec.py``.
"""

from __future__ import annotations

import math

import torch
import torch.nn as nn
import torch.nn.functional as F

N_MELS = 80
COND_CHANNELS = 36          # 12 chroma + 2 beat + 1 energy + 6 section + 12 key + 2 mode + 1 bpm
SECTION_LABELS = ["intro", "verse", "prechorus", "chorus", "bridge", "outro"]


# --------------------------------------------------------------------------- #
# building blocks
# --------------------------------------------------------------------------- #
class FourierEmbedding(nn.Module):
    """Scalar (noise level 0..1) -> vector, the standard diffusion time embedding."""

    def __init__(self, dim: int = 128, bands: int = 8):
        super().__init__()
        self.register_buffer("freqs", 2.0 ** torch.arange(bands).float() * math.pi)
        self.net = nn.Sequential(nn.Linear(bands * 2, dim), nn.SiLU(), nn.Linear(dim, dim))

    def forward(self, t: torch.Tensor) -> torch.Tensor:
        x = t.reshape(-1, 1) * self.freqs.reshape(1, -1)
        return self.net(torch.cat([torch.sin(x), torch.cos(x)], dim=1))


def film(x: torch.Tensor, emb: torch.Tensor, out_channels: int) -> torch.Tensor:
    """Feature-wise linear modulation from a conditioning vector."""
    gamma, beta = emb.chunk(2, dim=1)
    gamma = gamma.reshape(-1, out_channels, 1)
    beta = beta.reshape(-1, out_channels, 1)
    return x * (1.0 + gamma) + beta


class ResBlock(nn.Module):
    """GroupNorm -> SiLU -> Conv x2 with FiLM conditioning and a residual path."""

    def __init__(self, in_ch: int, out_ch: int, emb_dim: int, groups: int = 8):
        super().__init__()
        groups = max(1, min(groups, in_ch))
        self.norm1 = nn.GroupNorm(groups, in_ch)
        self.conv1 = nn.Conv1d(in_ch, out_ch, 3, padding=1)
        self.norm2 = nn.GroupNorm(max(1, min(groups, out_ch)), out_ch)
        self.conv2 = nn.Conv1d(out_ch, out_ch, 3, padding=1)
        self.emb = nn.Linear(emb_dim, out_ch * 2)
        self.skip = (nn.Conv1d(in_ch, out_ch, 1) if in_ch != out_ch else nn.Identity())

    def forward(self, x: torch.Tensor, emb: torch.Tensor) -> torch.Tensor:
        h = self.conv1(F.silu(self.norm1(x)))
        h = film(h, self.emb(emb), h.shape[1])
        h = self.conv2(F.silu(self.norm2(h)))
        return h + self.skip(x)


class Down(nn.Module):
    def __init__(self, ch: int):
        super().__init__()
        self.conv = nn.Conv1d(ch, ch, 4, stride=2, padding=1)

    def forward(self, x):
        return self.conv(x)


class Up(nn.Module):
    def __init__(self, ch: int):
        super().__init__()
        self.conv = nn.ConvTranspose1d(ch, ch, 4, stride=2, padding=1)

    def forward(self, x):
        return self.conv(x)


# --------------------------------------------------------------------------- #
# the network
# --------------------------------------------------------------------------- #
class VocalToMusicUNet(nn.Module):
    """Predicts a clean accompaniment mel from a noisy one plus vocal context.

    shapes:
        noisy_mel  (B, N_MELS, T)   the target, corrupted with noise
        vocal_mel  (B, N_MELS, T)   the conditioning vocal (never corrupted)
        cond       (B, COND_CHANNELS, T)
        sigma      (B,)  in [0, 1]
        ->         (B, N_MELS, T) in [-1, 1]
    """

    def __init__(self, n_mels: int = N_MELS, cond_ch: int = COND_CHANNELS,
                 channels: tuple[int, ...] = (64, 128, 256, 384), blocks: int = 2,
                 emb_dim: int = 256):
        super().__init__()
        self.n_mels = n_mels
        self.cond_ch = cond_ch
        self.emb = FourierEmbedding(emb_dim)
        self.in_conv = nn.Conv1d(n_mels * 2 + cond_ch, channels[0], 3, padding=1)
        self.downs = nn.ModuleList()
        self.ups = nn.ModuleList()
        self.enc = nn.ModuleList()
        self.dec = nn.ModuleList()

        prev = channels[0]
        for ch in channels:
            self.enc.append(nn.ModuleList([ResBlock(prev, ch, emb_dim) for _ in range(blocks)]))
            self.downs.append(Down(ch))
            prev = ch
        for i, ch in enumerate(reversed(channels[:-1])):
            self.ups.append(Up(prev))
            self.dec.append(nn.ModuleList(
                [ResBlock(prev + channels[-2 - i], ch, emb_dim) for _ in range(blocks)]))
            prev = ch
        self.out_norm = nn.GroupNorm(max(1, min(8, prev)), prev)
        self.out_conv = nn.Conv1d(prev, n_mels, 3, padding=1)

    def forward(self, noisy_mel: torch.Tensor, vocal_mel: torch.Tensor,
                cond: torch.Tensor, sigma: torch.Tensor) -> torch.Tensor:
        emb = self.emb(sigma.reshape(-1))
        x = torch.cat([noisy_mel, vocal_mel, cond], dim=1)
        h = self.in_conv(x)
        skips = []
        for blocks, down in zip(self.enc, self.downs):
            for block in blocks:
                h = block(h, emb)
            skips.append(h)
            h = down(h)
        for up, blocks, skip in zip(self.ups, self.dec, reversed(skips[:-1])):
            h = up(h)
            if h.shape[-1] != skip.shape[-1]:
                h = F.pad(h, (0, skip.shape[-1] - h.shape[-1]))
            h = torch.cat([h, skip], dim=1)
            for block in blocks:
                h = block(h, emb)
        h = self.out_conv(F.silu(self.out_norm(h)))
        return torch.tanh(h)


def count_parameters(model: nn.Module) -> int:
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


# --------------------------------------------------------------------------- #
# noise schedule + iterative sampler (shared by train/eval/infer)
# --------------------------------------------------------------------------- #
def sample_sigma(batch: int, device, sigma_min: float = 0.05,
                 sigma_max: float = 1.0, mode: str = "log-uniform") -> torch.Tensor:
    if mode == "log-uniform":
        u = torch.rand(batch, device=device)
        return torch.exp(u * (math.log(sigma_max) - math.log(sigma_min)) + math.log(sigma_min))
    return sigma_min + torch.rand(batch, device=device) * (sigma_max - sigma_min)


@torch.no_grad()
def sample(model: nn.Module, vocal_mel: torch.Tensor, cond: torch.Tensor,
           sigma_start: float = 1.0, sigma_end: float = 0.08, steps: int = 4,
           generator=None) -> torch.Tensor:
    """Iterative refinement: predict x0, re-noise to a lower sigma, predict again."""
    b, _, t = cond.shape
    device = cond.device
    shape = (b, model.n_mels, t)
    x = torch.randn(shape, device=device, generator=generator) * sigma_start
    sigmas = torch.exp(torch.linspace(math.log(sigma_start), math.log(sigma_end), steps,
                                      device=device))
    x0 = x
    for i, sig in enumerate(sigmas):
        x0 = model(x, vocal_mel, cond, sig.expand(b))
        if i == len(sigmas) - 1:
            break
        eps = torch.randn(shape, device=device, generator=generator)
        x = x0 + eps * sigmas[i + 1]
    return x0.clamp(-1.0, 1.0)
