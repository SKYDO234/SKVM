"""Music-model backends.

Every backend exposes ``generate(cond, vocals=None, duration=None) -> np.ndarray``
returning a stereo float32 track at ``cond.sr``.

* ``procedural``  - numpy rule-based arranger (always available, CPU)
* ``musicgen``    - Meta MusicGen (melody/text conditioned, 30 s windows)
* ``ace-step``    - ACE-Step 1.x via its Python API (text + audio prompt)
* ``external``    - any CLI model, via a command template

Adding your own model means writing ~40 lines: load weights once in
``_load()``, turn ``cond`` into model inputs, return audio.

Status: ``procedural`` and ``external`` are exercised by the test suite;
``musicgen`` and ``ace-step`` follow the upstream Python APIs at the time of
writing but need their (large) dependencies, so they were not executed in the
sandbox this was built in. Expect to adjust argument names when the upstream
APIs move - that is exactly why the ``external`` backend exists as an escape
hatch.
"""

from __future__ import annotations

import importlib.util
import os
import shlex
import shutil
import subprocess
import tempfile
from typing import Optional

import numpy as np

from .analyze import Conditioning
from .audio import DEFAULT_SR, read_audio, resample, to_stereo, write_audio
from .procedural import ProceduralBackend, detect_style
from .render import build_prompt, render_melody, section_label_at


def _crossfade_concat(chunks: list[np.ndarray], overlap: int, total: int) -> np.ndarray:
    """Equal-power crossfade of consecutive chunks into one buffer."""
    if not chunks:
        return np.zeros((total, 2), dtype=np.float32)
    out = np.zeros((total, 2), dtype=np.float32)
    norm = np.zeros(total, dtype=np.float32)
    pos = 0
    for i, chunk in enumerate(chunks):
        chunk = to_stereo(chunk)
        n = min(len(chunk), total - pos)
        if n <= 0:
            break
        seg = chunk[:n].copy()
        win = np.ones(n, dtype=np.float32)
        k = min(overlap, n // 2)
        if i > 0 and k > 1:
            win[:k] = np.sin(np.linspace(0, np.pi / 2, k, dtype=np.float32))
        if i < len(chunks) - 1 and k > 1:
            win[-k:] = np.cos(np.linspace(0, np.pi / 2, k, dtype=np.float32))
        out[pos:pos + n] += seg * win[:, None]
        norm[pos:pos + n] += win
        pos += n - min(overlap, n)
    valid = norm > 1e-4
    out[valid] /= norm[valid, None]
    return out.astype(np.float32)


# --------------------------------------------------------------------------- #
# MusicGen
# --------------------------------------------------------------------------- #
class MusicGenBackend:
    """Meta MusicGen (audiocraft), melody-conditioned, generated in 30 s windows."""

    name = "musicgen"
    max_window = 29.0

    def __init__(self, model: str = "facebook/musicgen-melody",
                 device: Optional[str] = None, window: float = 25.0,
                 overlap: float = 3.0, guidance: float = 3.0,
                 temperature: float = 1.0, melody_source: str = "notes",
                 large: bool = False):
        self.model_name = model
        self.device = device
        self.window = min(window, self.max_window)
        self.overlap = overlap
        self.guidance = guidance
        self.temperature = temperature
        self.melody_source = melody_source
        self.large = large
        self._model = None
        self._torch = None

    # -- loading ----------------------------------------------------------- #
    def _load(self):
        if self._model is not None:
            return
        if importlib.util.find_spec("torch") is None:
            raise RuntimeError("MusicGen needs PyTorch: pip install torch")
        if importlib.util.find_spec("audiocraft") is None:
            raise RuntimeError(
                "MusicGen needs audiocraft: pip install audiocraft "
                "(or `pip install git+https://github.com/facebookresearch/audiocraft.git`)")
        import torch  # noqa: F401
        from audiocraft.models import MusicGen  # type: ignore

        self._torch = torch
        device = self.device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.device = device
        self._model = MusicGen.get_pretrained(self.model_name, device=device)

    # -- generation -------------------------------------------------------- #
    def generate(self, cond: Conditioning, vocals: Optional[np.ndarray] = None,
                 duration: Optional[float] = None) -> np.ndarray:
        self._load()
        torch = self._torch
        sr = cond.sr or DEFAULT_SR
        duration = float(duration or cond.duration)
        torch.manual_seed(cond.seed)

        guide = None
        if "melody" in self.model_name:
            if self.melody_source == "vocals" and vocals is not None:
                guide = resample(np.asarray(vocals, dtype=np.float32), sr, 32000)
                if guide.ndim > 1:
                    guide = guide.mean(axis=1)
            else:
                mono = render_melody(cond, sr, duration)
                guide = resample(mono, sr, 32000)
            guide = torch.from_numpy(np.ascontiguousarray(guide)).float().to(self.device)

        chunks = []
        step = self.window - self.overlap
        n_windows = max(1, int(np.ceil((duration - self.overlap) / step)))
        for i in range(n_windows):
            start = i * step
            length = min(self.window, duration - start)
            if length < 2.0:
                break
            label = section_label_at(cond, start + length / 2.0)
            prompt = build_prompt(cond, label, detect_style(cond.prompt))
            self._model.set_generation_params(
                duration=float(length), use_sampling=True, top_k=250,
                temperature=self.temperature, cfg_coef=self.guidance)
            with torch.no_grad():
                if guide is not None:
                    g0 = int(start * 32000)
                    g1 = g0 + int(length * 32000)
                    pad = max(0, g1 - len(guide))
                    seg = torch.cat([guide[g0:g1], torch.zeros(pad, device=guide.device)]) \
                        if g0 < len(guide) else torch.zeros(int(length * 32000), device=guide.device)
                    out = self._model.generate_with_chroma(
                        [prompt], [seg[None, :]], melody_sample_rate=32000)
                else:
                    out = self._model.generate([prompt], progress=False)
            arr = out.detach().cpu().numpy()
            clip = arr[0].T if arr.ndim == 3 else arr
            chunks.append(to_stereo(clip))
            print(f"[musicgen] window {i + 1}/{n_windows} "
                  f"({start:.1f}s-{start + length:.1f}s, {label})")
        total = int(np.ceil(duration * sr)) + int(0.5 * sr)
        music = _crossfade_concat([resample(c, 32000, sr) for c in chunks],
                                  overlap=int(self.overlap * sr), total=total)
        return to_stereo(music)


# --------------------------------------------------------------------------- #
# ACE-Step
# --------------------------------------------------------------------------- #
class ACEStepBackend:
    """ACE-Step (text + optional reference audio prompt).

    ACE-Step generates full songs including vocals; we use it *instrumentally*
    so the supplied vocal stays the only voice.  Kept as a thin adapter because
    its API is still moving — check the repo you installed.
    """

    name = "ace-step"
    max_window = 240.0

    def __init__(self, model: str = "ACE-Step/ACE-Step-v1-3.5B", device: Optional[str] = None,
                 steps: int = 27, guidance: float = 15.0, audio_prompt: bool = True):
        self.model_name = model
        self.device = device
        self.steps = steps
        self.guidance = guidance
        self.audio_prompt = audio_prompt
        self._pipeline = None

    def _load(self):
        if self._pipeline is not None:
            return
        try:
            from acestep.pipeline_ace_step import ACEStepPipeline  # type: ignore
        except Exception as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "ACE-Step is not importable. Clone https://github.com/ace-step/ACE-Step "
                "and `pip install -e .` first.") from exc
        self._pipeline = ACEStepPipeline(
            checkpoint_dir=None, device_id=0 if self.device != "cpu" else -1,
            dtype="bfloat16", torch_compile=False)

    def generate(self, cond: Conditioning, vocals: Optional[np.ndarray] = None,
                 duration: Optional[float] = None) -> np.ndarray:
        self._load()
        sr = cond.sr or DEFAULT_SR
        duration = float(duration or cond.duration)
        prompt = build_prompt(cond, "", detect_style(cond.prompt), extra="no vocals")
        tmpdir = tempfile.mkdtemp(prefix="acestep-")
        ref = None
        if self.audio_prompt and vocals is not None:
            ref = os.path.join(tmpdir, "vocal_ref.wav")
            write_audio(ref, to_stereo(vocals)[: int(min(30.0, duration) * sr)], sr)
        out = self._pipeline(
            format="wav", audio_duration=float(min(duration, self.max_window)),
            prompt=prompt, lyrics="[instrumental]", infer_step=self.steps,
            guidance_scale=self.guidance, scheduler_type="euler",
            cfg_type="apg", omega_scale=self.guidance,
            actual_seeds=[cond.seed], repaint_segments=[],
            use_erg_lyric=True, use_erg_diffusion=True,
            guidance_interval=0.5, guidance_interval_decay=0.0,
            audio2audio_enable=bool(ref), ref_audio_strength=0.35,
            ref_audio_input=ref, audio_prompt=ref if self.audio_prompt else None,
            output_path=tmpdir,
        )
        path = out[0] if isinstance(out, (list, tuple)) else out
        audio, file_sr = read_audio(str(path))
        if file_sr != sr:
            audio = resample(audio, file_sr, sr)
        return to_stereo(audio)


# --------------------------------------------------------------------------- #
# external CLI backend
# --------------------------------------------------------------------------- #
class ExternalBackend:
    """Run any command-line model.

    Placeholders: ``{input}`` (melody guide wav), ``{output}`` (wav to write),
    ``{prompt}``, ``{duration}``, ``{key}``, ``{bpm}``, ``{seed}``.

    Example::

        songforge run in.wav --backend external --model-name my-model \\
            --command "python infer.py --audio {input} --text '{prompt}' \\
                       --seconds {duration} --seed {seed} --out {output}"
    """

    name = "external"
    max_window = None

    def __init__(self, command: str, sr: int = DEFAULT_SR, timeout: int = 3600):
        if not command:
            raise ValueError("external backend needs --command")
        self.command = command
        self.sr = sr
        self.timeout = timeout

    def generate(self, cond: Conditioning, vocals: Optional[np.ndarray] = None,
                 duration: Optional[float] = None) -> np.ndarray:
        sr = cond.sr or self.sr
        duration = float(duration or cond.duration)
        workdir = tempfile.mkdtemp(prefix="songforge-ext-")
        guide_path = os.path.join(workdir, "melody.wav")
        out_path = os.path.join(workdir, "music.wav")
        write_audio(guide_path, render_melody(cond, sr, duration), sr)
        args = {
            "input": guide_path, "output": out_path,
            "prompt": build_prompt(cond, section_label_at(cond, duration / 2),
                                   detect_style(cond.prompt)),
            "duration": f"{duration:.3f}", "key": cond.key_name,
            "bpm": f"{cond.bpm:.2f}", "seed": str(cond.seed),
        }
        cmd = self.command.format(**{k: shlex.quote(v) if k != "prompt" else v.replace("'", "")
                                     for k, v in args.items()})
        print(f"[external] {cmd}")
        subprocess.run(cmd, shell=True, check=True, timeout=self.timeout)
        if not os.path.exists(out_path):
            produced = [f for f in os.listdir(workdir) if f.endswith((".wav", ".flac", ".mp3", ".ogg"))]
            if not produced:
                raise RuntimeError(f"external model produced no audio in {workdir}")
            out_path = os.path.join(workdir, produced[-1])
        audio, file_sr = read_audio(out_path)
        if file_sr != sr:
            audio = resample(audio, file_sr, sr)
        return to_stereo(audio)


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
BACKENDS = {
    "procedural": ProceduralBackend,
    "musicgen": MusicGenBackend,
    "ace-step": ACEStepBackend,
    "external": ExternalBackend,
}


def create_backend(name: str, **kwargs):
    name = (name or "procedural").lower()
    if name not in BACKENDS:
        raise SystemExit(f"unknown backend {name!r}; choose from {', '.join(BACKENDS)}")
    cls = BACKENDS[name]
    if name == "procedural":
        kwargs = {k: v for k, v in kwargs.items() if k in ("style", "seed")}
    elif name == "musicgen":
        kwargs = {k: v for k, v in kwargs.items()
                  if k in ("model", "device", "window", "overlap", "guidance",
                           "temperature", "melody_source")}
        if kwargs.get("model") is None:
            kwargs.pop("model", None)
    elif name == "ace-step":
        kwargs = {k: v for k, v in kwargs.items() if k in ("model", "device", "steps", "guidance")}
        if kwargs.get("model") is None:
            kwargs.pop("model", None)
    elif name == "external":
        kwargs = {"command": kwargs.get("command") or "", "sr": kwargs.get("sr", DEFAULT_SR)}
    return cls(**kwargs)


def available_backends() -> dict:
    info = {}
    for name, cls in BACKENDS.items():
        info[name] = getattr(cls, "max_window", None)
    info["_env"] = {
        "numpy": importlib.util.find_spec("numpy") is not None,
        "soundfile": importlib.util.find_spec("soundfile") is not None,
        "torch": importlib.util.find_spec("torch") is not None,
        "audiocraft": importlib.util.find_spec("audiocraft") is not None,
        "acestep": importlib.util.find_spec("acestep") is not None,
        "demucs": importlib.util.find_spec("demucs") is not None,
        "ffmpeg": shutil.which("ffmpeg") is not None,
    }
    return info


__all__ = ["BACKENDS", "MusicGenBackend", "ACEStepBackend", "ExternalBackend",
           "ProceduralBackend", "create_backend", "available_backends"]
