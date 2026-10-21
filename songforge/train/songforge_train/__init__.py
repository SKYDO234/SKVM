"""Training code for a vocal-conditioned accompaniment model.

    dataset.py  feature extraction + cache (numpy)
    model.py    conditional 1-D U-Net (torch)
    train.py    training loop (torch)
    codec.py    mel -> audio: Griffin-Lim / HiFi-GAN
    infer.py    vocal -> instrumental -> mixed song with your checkpoint
"""

__version__ = "0.1.0"
