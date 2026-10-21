"""Fast, dependency-light tests: `python -m unittest discover -s tests`."""

import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from songforge.analyze import (analyze_vocal, estimate_key, estimate_tempo,  # noqa: E402
                               notes_from_track, pitch_track, scale_pcs)
from songforge.audio import (DEFAULT_SR, db_to_gain, fade, limiter, lufs,  # noqa: E402
                             pitch_shift, read_audio, resample, time_stretch,
                             write_audio)
from songforge.demo import make_demo_vocal  # noqa: E402
from songforge.mix import MixSettings, mix_song  # noqa: E402
from songforge.procedural import ProceduralBackend, bar_chords, detect_style  # noqa: E402
from songforge.render import render_melody  # noqa: E402


def sine(freq, seconds, sr=DEFAULT_SR, amp=0.5):
    t = np.arange(int(seconds * sr)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


class AudioTests(unittest.TestCase):
    def test_wav_roundtrip(self):
        x = np.stack([sine(220, 0.5), sine(330, 0.5)], axis=1)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.wav")
            write_audio(p, x, 44100)
            y, sr = read_audio(p)
        self.assertEqual(sr, 44100)
        self.assertEqual(y.shape, x.shape)
        self.assertLess(np.max(np.abs(y - x)), 1e-3)

    def test_resample_length(self):
        x = sine(440, 1.0, 44100)
        y = resample(x, 44100, 22050)
        self.assertAlmostEqual(len(y) / 22050, 1.0, places=2)

    def test_lufs_reference(self):
        # a 1 kHz sine at -20 dBFS is ~ -23.0 LUFS
        x = sine(1000, 3.0, 48000, amp=db_to_gain(-20))
        self.assertAlmostEqual(lufs(x, 48000), -23.0, delta=1.0)

    def test_limiter_respects_ceiling(self):
        x = (sine(120, 1.0, 44100, amp=1.0) * 3.0).astype(np.float32)
        y = limiter(x, 44100, ceiling_db=-1.0)
        self.assertLessEqual(float(np.max(np.abs(y))), db_to_gain(-1.0) + 1e-6)

    def test_time_stretch_length_and_pitch(self):
        x = sine(440, 1.0)
        y = time_stretch(x, 44100, rate=1.5)      # 1.5x faster -> 2/3 length
        self.assertAlmostEqual(len(y) / 44100, 1.0 / 1.5, delta=0.05)
        spec = np.abs(np.fft.rfft(y))
        peak_hz = np.argmax(spec) * 44100 / len(y)
        self.assertAlmostEqual(peak_hz, 440, delta=20)
        longer = time_stretch(x, 44100, rate=0.5)
        self.assertAlmostEqual(len(longer) / 44100, 2.0, delta=0.05)

    def test_pitch_shift_semitones(self):
        x = sine(440, 1.0)
        y = pitch_shift(x, 44100, 12)
        spec = np.abs(np.fft.rfft(y[: 44100 // 2]))
        peak_hz = np.argmax(spec) * 44100 / (44100 // 2)
        self.assertAlmostEqual(peak_hz, 880, delta=30)

    def test_fade_edges(self):
        x = np.ones((1000, 2), dtype=np.float32)
        y = fade(x, 44100, 10, 10)
        self.assertLess(float(y[0, 0]), 0.05)
        self.assertLess(float(y[-1, 0]), 0.05)
        self.assertAlmostEqual(float(y[500, 0]), 1.0, places=5)


class AnalyzeTests(unittest.TestCase):
    def test_tempo_click_track(self):
        sr, bpm = 22050, 120
        n = sr * 8
        x = np.zeros(n, dtype=np.float32)
        period = int(sr * 60 / bpm)
        for i in range(0, n - period, period):
            x[i:i + 200] += np.hanning(200).astype(np.float32)
        est, _ = estimate_tempo(x, sr)
        self.assertAlmostEqual(est, bpm, delta=4.0)

    def test_key_of_c_major_scale(self):
        sr = 22050
        notes = [60, 62, 64, 65, 67, 69, 71, 72]
        x = np.concatenate([sine(440 * 2 ** ((m - 69) / 12), 0.4, sr) for m in notes])
        root, mode, _ = estimate_key(x, sr)
        self.assertEqual(root, 0)
        self.assertEqual(mode, "major")

    def test_pitch_track_and_notes(self):
        sr = 22050
        x = np.concatenate([
            sine(440 * 2 ** ((m - 69) / 12), 0.3, sr, amp=0.5) for m in (60, 60, 64, 67)])
        times, midi, periodicity, rms_db = pitch_track(x, sr)
        notes = notes_from_track(times, midi, rms_db, 0, "major")
        self.assertGreater(len(notes), 2)
        self.assertTrue(all(abs(n.midi - 60) < 2 or abs(n.midi - 64) < 2
                            or abs(n.midi - 67) < 2 for n in notes))
        self.assertGreater(float(np.nanmax(periodicity)), 0.5)

    def test_scale_pcs(self):
        self.assertEqual(scale_pcs(0, "major"), [0, 2, 4, 5, 7, 9, 11])
        self.assertEqual(scale_pcs(9, "minor"), [9, 11, 0, 2, 4, 5, 7])

    def test_demo_vocal_analysis(self):
        vocal = make_demo_vocal(bpm=104.0, sr=22050, repeats=1)
        cond = analyze_vocal(vocal, 22050)
        self.assertAlmostEqual(cond.bpm, 104.0, delta=6.0)
        self.assertGreater(len(cond.melody), 8)
        self.assertGreater(len(cond.sections), 0)


class GenerateTests(unittest.TestCase):
    def test_style_detection(self):
        self.assertEqual(detect_style("chill lo-fi beat"), "lofi")
        self.assertEqual(detect_style("house track"), "house")
        self.assertEqual(detect_style(""), "backbeat")

    def test_bar_chords_follow_key(self):
        vocal = make_demo_vocal(repeats=1, sr=22050)
        cond = analyze_vocal(vocal, 22050, prompt="lofi")
        chords = bar_chords(cond)
        self.assertGreater(len(chords), 4)
        for root, triad in chords:
            self.assertTrue(24 <= root <= 48)
            self.assertEqual(len(triad), 3)

    def test_procedural_backend_length_and_level(self):
        vocal = make_demo_vocal(repeats=1, sr=22050)
        cond = analyze_vocal(vocal, 22050, prompt="lofi")
        music = ProceduralBackend(seed=1).generate(cond)
        self.assertEqual(music.shape[1], 2)
        self.assertGreaterEqual(len(music), int(cond.duration * 22050))
        self.assertGreater(float(np.max(np.abs(music))), 0.05)
        self.assertLessEqual(float(np.max(np.abs(music))), 1.0)

    def test_melody_guide_matches_notes(self):
        vocal = make_demo_vocal(repeats=1, sr=22050)
        cond = analyze_vocal(vocal, 22050)
        guide = render_melody(cond, 22050, cond.duration)
        self.assertEqual(len(guide), int(cond.duration * 22050))
        self.assertGreater(float(np.max(np.abs(guide))), 0.2)


class MixTests(unittest.TestCase):
    def test_mix_levels_and_length(self):
        sr = 22050
        vocal = make_demo_vocal(repeats=1, sr=sr)
        music = sine(110, len(vocal) / sr, sr, amp=0.3)[:, None] * np.ones((1, 2))
        out, report = mix_song(vocal, music, sr, settings=MixSettings(target_lufs=-14.0))
        self.assertEqual(out.shape[1], 2)
        self.assertGreaterEqual(len(out), len(vocal))
        self.assertLessEqual(len(out), len(vocal) + 5 * sr)
        self.assertLessEqual(report["out"]["peak_db"], -0.9)
        self.assertAlmostEqual(report["out"]["lufs"], -14.0, delta=1.2)
        self.assertIn("settings", report)

    def test_no_master_path(self):
        sr = 22050
        vocal = make_demo_vocal(repeats=1, sr=sr)
        music = sine(110, len(vocal) / sr, sr, amp=0.3)[:, None] * np.ones((1, 2))
        out, report = mix_song(vocal, music, sr, master=False)
        self.assertLessEqual(len(out), len(vocal) + 5 * sr)
        self.assertIn("out", report)


class PipelineTests(unittest.TestCase):
    def test_full_pipeline_procedural(self):
        from songforge.pipeline import PipelineConfig, run_pipeline
        sr = 22050
        with tempfile.TemporaryDirectory() as d:
            vocal = make_demo_vocal(repeats=1, sr=sr)
            inp = os.path.join(d, "vocal.wav")
            write_audio(inp, vocal, sr)
            cfg = PipelineConfig(input_path=inp, out_dir=os.path.join(d, "out"),
                                 input_kind="vocals", backend="procedural",
                                 prompt="lofi", seed=2, sr=sr, max_seconds=12.0)
            res = run_pipeline(cfg, progress=lambda *_: None)
            for path in (res.master_path, res.music_path, res.vocals_path,
                         res.conditioning_path, res.report_path):
                self.assertTrue(os.path.exists(path), path)
            audio, out_sr = read_audio(res.master_path)
            self.assertEqual(out_sr, sr)
            self.assertGreater(len(audio), int(10 * sr))
            self.assertIn("out", res.report)
            self.assertEqual(res.report["backend"], "procedural")

    def test_cli_help(self):
        from songforge.cli import main
        with self.assertRaises(SystemExit) as ctx:
            main(["--help"])
        self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
