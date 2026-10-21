"""Tests for the training package. Skipped automatically when torch is missing.

    python -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

try:
    import torch
    HAVE_TORCH = True
except Exception:                                        # pragma: no cover
    HAVE_TORCH = False


@unittest.skipUnless(HAVE_TORCH, "torch not installed")
class ModelTests(unittest.TestCase):
    def test_forward_shapes(self):
        from songforge_train.model import COND_CHANNELS, VocalToMusicUNet, count_parameters
        model = VocalToMusicUNet(channels=(16, 32, 48), blocks=1, emb_dim=64)
        b, t = 2, 160
        noisy = torch.randn(b, 80, t)
        vocal = torch.randn(b, 80, t)
        cond = torch.randn(b, COND_CHANNELS, t)
        out = model(noisy, vocal, cond, torch.rand(b))
        self.assertEqual(out.shape, (b, 80, t))
        self.assertGreater(count_parameters(model), 1000)
        self.assertLessEqual(float(out.detach().abs().max()), 1.0)

    def test_odd_length_padding(self):
        from songforge_train.model import COND_CHANNELS, VocalToMusicUNet
        model = VocalToMusicUNet(channels=(16, 32, 48), blocks=1, emb_dim=64)
        t = 157                                                # not a multiple of 8
        out = model(torch.randn(1, 80, t), torch.randn(1, 80, t),
                    torch.randn(1, COND_CHANNELS, t), torch.rand(1))
        self.assertEqual(out.shape[-1], t)

    def test_sampler_is_seed_stable(self):
        from songforge_train.model import COND_CHANNELS, VocalToMusicUNet, sample
        model = VocalToMusicUNet(channels=(16, 32, 48), blocks=1, emb_dim=64).eval()
        vocal = torch.randn(1, 80, 96)
        cond = torch.randn(1, COND_CHANNELS, 96)
        a = sample(model, vocal, cond, steps=2,
                   generator=torch.Generator().manual_seed(7))
        b = sample(model, vocal, cond, steps=2,
                   generator=torch.Generator().manual_seed(7))
        self.assertTrue(torch.allclose(a, b))


@unittest.skipUnless(HAVE_TORCH, "torch not installed")
class CodecTests(unittest.TestCase):
    def test_griffin_lim_length_and_range(self):
        from songforge_train.codec import griffin_lim
        from songforge_train.dataset import HOP
        mel = torch.zeros(1, 80, 64)
        audio = griffin_lim(mel, n_iter=4)
        self.assertEqual(audio.shape[-1], (64 - 1) * HOP)
        self.assertLessEqual(float(audio.abs().max()), 1.0)

    def test_mel_roundtrip_shapes(self):
        from songforge_train.codec import mel_to_linear
        from songforge_train.dataset import log_mel, mel_filterbank, mel_to_db
        self.assertEqual(mel_to_db(np.array([-1.0, 0.0, 1.0])).tolist(), [-80.0, -40.0, 0.0])
        fb = mel_filterbank()
        sig = np.random.default_rng(0).standard_normal(22050).astype(np.float32) * 0.1
        mel = log_mel(sig, 22050, fb)
        self.assertEqual(mel.shape[1], 80)
        self.assertGreater(mel.shape[0], 40)
        self.assertGreaterEqual(float(mel.min()), -1.01)
        self.assertLessEqual(float(mel.max()), 1.01)
        lin = mel_to_linear(torch.from_numpy(mel.T)[None])
        self.assertEqual(lin.shape[1], 1025)


@unittest.skipUnless(HAVE_TORCH, "torch not installed")
class DataTests(unittest.TestCase):
    def test_cache_and_dataset(self):
        from songforge.audio import write_audio
        from songforge.demo import make_demo_vocal
        from songforge_train.dataset import analyze_track, build_cache
        from songforge_train.train import PairDataset

        with tempfile.TemporaryDirectory() as d:
            raw = os.path.join(d, "raw")
            for i in range(2):
                track = os.path.join(raw, f"t{i}")
                os.makedirs(track)
                vocal = make_demo_vocal(bpm=100 + 10 * i, seed=i, repeats=1)
                acc = np.zeros_like(vocal)
                acc[:, 0] = 0.1 * np.sin(2 * np.pi * 110 * np.arange(len(acc)) / 44100)
                write_audio(os.path.join(track, "vocals.wav"), vocal, 44100)
                write_audio(os.path.join(track, "accompaniment.wav"), acc, 44100)
            cache = os.path.join(d, "cache")
            build_cache(raw, cache, min_seconds=5.0)
            self.assertTrue(os.path.exists(os.path.join(cache, "manifest.json")))
            ds = PairDataset(cache, crop_seconds=4.0, train=True, seed=0)
            item = ds[0]
            crop = int(4.0 * 22050 / 512) + 1
            self.assertEqual(item["vocal_mel"].shape[0], 80)
            self.assertEqual(item["cond"].shape[0], 36)
            self.assertLessEqual(item["vocal_mel"].shape[1], crop + 1)
            self.assertGreater(item["vocal_mel"].shape[1], 100)

            bundle = analyze_track(np.zeros(22050 * 6, np.float32),
                                   np.zeros(22050 * 6, np.float32), 22050)
            self.assertIn("target_mel", bundle)
            self.assertEqual(bundle["vocal_mel"].shape,
                             bundle["target_mel"].shape)


class ConfigTests(unittest.TestCase):
    def test_configs_parse(self):
        cfg_dir = os.path.join(os.path.dirname(_HERE), "configs")
        for name in os.listdir(cfg_dir):
            if name.endswith(".json"):
                import pathlib
                cfg = json.loads(pathlib.Path(cfg_dir, name).read_text(encoding="utf-8"))
                self.assertIn("channels", cfg)
                self.assertIn("batch_size", cfg)


if __name__ == "__main__":
    unittest.main()
