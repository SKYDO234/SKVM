"""Placeholder for a HiFi-GAN generator definition.

Drop your vocoder architecture here (any module that maps
``(B, n_mels, T) -> (B, 1, samples)`` at 22.05 kHz) and point
``--vocoder hifigan --vocoder-ckpt path.pt`` at matching weights.
"""

from __future__ import annotations

import torch
import torch.nn as nn


class Generator(nn.Module):
    def __init__(self, n_mels: int = 80, upsample: tuple[int, ...] = (8, 8, 2, 2)):
        super().__init__()
        layers = []
        ch = 512
        layers.append(nn.Conv1d(n_mels, ch, 7, padding=3))
        for k in upsample:
            layers += [nn.LeakyReLU(0.1), nn.ConvTranspose1d(ch, ch // 2, 2 * k,
                                                             stride=k, padding=k // 2)]
            ch //= 2
        layers += [nn.LeakyReLU(0.1), nn.Conv1d(ch, 1, 7, padding=3), nn.Tanh()]
        self.net = nn.Sequential(*layers)

    def forward(self, mel: torch.Tensor) -> torch.Tensor:
        return self.net(mel)
