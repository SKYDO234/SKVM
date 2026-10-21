"""Command-line entry point: ``songforge`` / ``python -m songforge``."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import __version__
from .analyze import analyze_vocal
from .audio import read_audio, write_audio
from .backends import available_backends, create_backend
from .mix import MixSettings, mix_song, reference_report
from .pipeline import PipelineConfig, run_pipeline, summarize_report
from .render import describe, render_melody
from .separate import demucs_version, separate_song


def _add_mix_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--vocal-gain", type=float, default=0.0, help="vocal trim in dB")
    p.add_argument("--music-gain", type=float, default=-2.5, help="instrumental trim in dB")
    p.add_argument("--duck", type=float, default=4.0,
                   help="sidechain depth in dB under the vocal (0 disables)")
    p.add_argument("--target-lufs", type=float, default=-14.0)
    p.add_argument("--ceiling", type=float, default=-1.0, help="limiter ceiling, dBFS")
    p.add_argument("--no-master", action="store_true",
                   help="skip loudness normalisation and limiting")


def _settings(a) -> MixSettings:
    return MixSettings(vocal_gain_db=a.vocal_gain, music_gain_db=a.music_gain,
                       duck_db=a.duck, target_lufs=a.target_lufs,
                       ceiling_db=a.ceiling)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="songforge",
        description="Give it a vocal; get the whole song back.")
    p.add_argument("--version", action="version", version=f"songforge {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    # ---- run ------------------------------------------------------------
    r = sub.add_parser("run", help="full pipeline: song/vocal in -> finished song out")
    r.add_argument("input", help="audio file (a full song, or a clean vocal stem)")
    r.add_argument("-o", "--out", default="out", help="output directory")
    r.add_argument("--input-kind", choices=["song", "vocals"], default="song",
                   help="'song' runs Demucs first; 'vocals' skips separation")
    r.add_argument("--backend", default="procedural",
                   choices=sorted(available_backends()))
    r.add_argument("--model", default=None, help="model id / checkpoint")
    r.add_argument("--command", default=None, help="command template for --backend external")
    r.add_argument("--style", default="auto", help="style hint, e.g. lofi / house / trap")
    r.add_argument("--prompt", default="", help="free-text style description")
    r.add_argument("--seed", type=int, default=0)
    r.add_argument("--seconds", type=float, default=None, help="process only the first N seconds")
    r.add_argument("--sr", type=int, default=44100)
    r.add_argument("--format", default="wav", choices=["wav", "mp3", "flac"])
    r.add_argument("--device", default=None, help="cpu / cuda / mps")
    r.add_argument("--demucs-model", default="htdemucs_ft")
    r.add_argument("--stems-dir", default=None)
    _add_mix_args(r)

    # ---- separate -------------------------------------------------------
    s = sub.add_parser("separate", help="split a song into vocals / accompaniment")
    s.add_argument("input")
    s.add_argument("-o", "--out", default="stems")
    s.add_argument("--model", default="htdemucs_ft")
    s.add_argument("--all-stems", action="store_true", help="keep drums/bass/other too")
    s.add_argument("--device", default="cpu")

    # ---- analyze --------------------------------------------------------
    a = sub.add_parser("analyze", help="vocal -> tempo, key, melody, form (JSON)")
    a.add_argument("input", help="clean vocal stem")
    a.add_argument("-o", "--out", default=None, help="write conditioning.json here")
    a.add_argument("--sr", type=int, default=44100)
    a.add_argument("--prompt", default="")
    a.add_argument("--melody-out", default=None, help="also write melody_guide.wav")

    # ---- generate -------------------------------------------------------
    g = sub.add_parser("generate", help="vocal -> instrumental only")
    g.add_argument("input", help="clean vocal stem")
    g.add_argument("-o", "--out", default="instrumental.wav")
    g.add_argument("--backend", default="procedural",
                   choices=sorted(available_backends()))
    g.add_argument("--model", default=None)
    g.add_argument("--command", default=None)
    g.add_argument("--style", default="auto")
    g.add_argument("--prompt", default="")
    g.add_argument("--seed", type=int, default=0)
    g.add_argument("--sr", type=int, default=44100)

    # ---- mix ------------------------------------------------------------
    m = sub.add_parser("mix", help="vocal + instrumental -> mastered song")
    m.add_argument("vocals")
    m.add_argument("music")
    m.add_argument("-o", "--out", default="song.wav")
    m.add_argument("--sr", type=int, default=44100)
    _add_mix_args(m)

    # ---- demo / doctor --------------------------------------------------
    d = sub.add_parser("demo", help="build a synthetic vocal and run the whole pipeline")
    d.add_argument("-o", "--out", default="demo_out")
    d.add_argument("--backend", default="procedural",
                   choices=sorted(available_backends()))
    d.add_argument("--seed", type=int, default=3)
    d.add_argument("--bpm", type=float, default=104.0)

    sub.add_parser("doctor", help="show which optional pieces are installed")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd == "run":
        cfg = PipelineConfig(
            input_path=args.input, out_dir=args.out, input_kind=args.input_kind,
            backend=args.backend, model=args.model, command=args.command,
            style=args.style, prompt=args.prompt, seed=args.seed,
            max_seconds=args.seconds, sr=args.sr, fmt=args.format,
            device=args.device, stems_dir=args.stems_dir,
            mix=_settings(args), master=not args.no_master,
            demucs_model=args.demucs_model)
        res = run_pipeline(cfg)
        print("\nsummary\n-------")
        print(summarize_report(res.report_path))
        return 0

    if args.cmd == "separate":
        stems = separate_song(args.input, args.out, model=args.model,
                              device=args.device, two_stems=None if args.all_stems else "vocals")
        print(json.dumps(stems, indent=2))
        return 0

    if args.cmd == "analyze":
        vocals, sr = read_audio(args.input, sr=args.sr)
        cond = analyze_vocal(vocals, sr, prompt=args.prompt)
        print(describe(cond))
        if args.out:
            Path(args.out).mkdir(parents=True, exist_ok=True)
            out = Path(args.out) / "conditioning.json"
            out.write_text(json.dumps(cond.to_dict(), indent=2), encoding="utf-8")
            print(f"wrote {out}")
        if args.melody_out:
            write_audio(args.melody_out, render_melody(cond, sr, cond.duration), sr)
            print(f"wrote {args.melody_out}")
        return 0

    if args.cmd == "generate":
        vocals, sr = read_audio(args.input, sr=args.sr)
        cond = analyze_vocal(vocals, sr, prompt=args.prompt, seed=args.seed)
        print(describe(cond))
        backend = create_backend(args.backend, model=args.model, command=args.command,
                                 style=args.style, seed=args.seed)
        music = backend.generate(cond, vocals=vocals, duration=cond.duration)
        write_audio(args.out, music, sr)
        print(f"wrote {args.out}")
        return 0

    if args.cmd == "mix":
        vocals, sr = read_audio(args.vocals, sr=args.sr)
        music, _ = read_audio(args.music, sr=sr)
        out, report = mix_song(vocals, music, sr, settings=_settings(args),
                               master=not args.no_master)
        write_audio(args.out, out, sr)
        print(reference_report(vocals, music, out, sr))
        print(f"wrote {args.out} ({report['out']['lufs']:.1f} LUFS)")
        return 0

    if args.cmd == "demo":
        from .demo import make_demo_vocal
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        vocal = make_demo_vocal(bpm=args.bpm, seed=args.seed)
        vocal_path = out_dir / "demo_vocal.wav"
        write_audio(str(vocal_path), vocal, 44100)
        print(f"wrote {vocal_path}")
        cfg = PipelineConfig(input_path=str(vocal_path), out_dir=str(out_dir),
                             input_kind="vocals", backend=args.backend, seed=args.seed,
                             prompt="chill lo-fi hip hop backing track")
        res = run_pipeline(cfg)
        print("\nsummary\n-------")
        print(summarize_report(res.report_path))
        return 0

    if args.cmd == "doctor":
        info = available_backends()
        env = info.pop("_env")
        print(f"songforge {__version__}")
        print(f"python            {sys.version.split()[0]} in {sys.executable}")
        for name, ok in env.items():
            print(f"{name:<17} {'yes' if ok else 'no'}")
        print(f"{'demucs version':<17} {demucs_version() or '-'}")
        print(f"{'ffmpeg':<17} {shutil.which('ffmpeg') or '-'}")
        print("\nbackends (max window seconds, None = unlimited):")
        for name, window in info.items():
            ready = True
            if name == "musicgen":
                ready = env["torch"] and env["audiocraft"]
            if name == "ace-step":
                ready = env["torch"] and env.get("acestep", False)
            print(f"  {name:<12} {'ready' if ready else 'needs extra deps':<18} {window}")
        if env["torch"] and env["audiocraft"]:
            suggestion = "musicgen"
        elif env["torch"] and env.get("acestep"):
            suggestion = "ace-step"
        else:
            suggestion = "procedural"
        print("\nsuggested backend:", suggestion)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
