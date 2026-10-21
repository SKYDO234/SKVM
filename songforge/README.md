# songforge — give it a vocal, get the whole song back

A complete, runnable reference for the thing you described:

> *take the vocals of a song → generate the music that fits them → merge the two into a finished track*

It is two things in one folder:

1. **A working pipeline** (`songforge/`) — separation, analysis, music generation,
   mixing and mastering. Runs on a laptop CPU with nothing but `numpy`. One
   command turns a vocal into a mastered song.
2. **A real trainable model** (`train/`) — a conditional U-Net that learns
   *"given this vocal, produce this accompaniment"*, plus dataset preparation,
   training loop, vocoder and inference. This is the "own AI model" part.

Nothing here touches the SKVM panel in the parent directory; it is a
self-contained project.

---

## 0. The honest version first

There is no magic recipe where you hand a model a vocal and it invents a
platinum arrangement. Realistic options, cheapest to most ambitious:

| Route | What you do | Data you need | Compute | Result |
|---|---|---|---|---|
| **A. Use an existing open model** | Text/melody-driven model + your vocal on top | none | 8–24 GB VRAM, or a rented GPU | Songs in minutes; backing does not truly *follow* the vocal |
| **B. Use this pipeline as-is** | rule-based arranger locked to the vocal's tempo/key/form | none | CPU | Coherent, in-key, on-beat backing; sounds "generated", not recorded |
| **C. Fine-tune a foundation model** | LoRA/adapters on ACE-Step/MusicGen with *your* style | 1–20 h of audio | 24 GB GPU, hours–days | Your style, still not vocal-conditioned |
| **D. Train your own conditioning model** | `train/` in this folder, on paired vocal+backing stems | 20 h = demo, 200 h = usable, 1 000 h+ = good | 1 GPU, days–weeks | Music that actually follows the vocal; quality scales with data |

Most people asking this question want **A or D**. This repo gives you B working
today and D end-to-end, with A one flag away.

---

## 1. Architecture

```
                    ┌────────────────────────────────────────────────────┐
 vocal stem ───────►│ 1. ANALYSIS   tempo · key · melody MIDI · form      │
 (or a full song)   │    (numpy DSP: onset autocorrelation, Krumhansl,    │
        │           │     autocorrelation pitch tracking)                 │
        │           └───────────────┬────────────────────────────────────┘
        │                           │  conditioning (bpm, key, notes, sections)
        │                           ▼
        │           ┌────────────────────────────────────────────────────┐
        │           │ 2. MUSIC MODEL                                      │
        │           ├────────────────────────────────────────────────────┤
        │           │ procedural  · rule arranger + synthesiser (CPU)     │
        │           │ musicgen    · MusicGen-Melody, 30 s windows         │
        │           │ ace-step    · ACE-Step text+audio prompt            │
        │           │ external    · any CLI model, incl. YOUR checkpoint  │
        │           └───────────────┬────────────────────────────────────┘
        │                           │ instrumental (stereo, same length)
        ▼                           ▼
    ┌────────────────────────────────────────────────────────────────────┐
    │ 3. MIX   clean vocal → sidechain-ducked instrumental → glue      │
    │ 4. MASTER  target LUFS → look-ahead limiter → fade-out             │
    └───────────────────────────────┬────────────────────────────────────┘
                                    ▼
                               song.wav  +  report.json
```

The important idea: **the vocal is the condition, not something you paste on
later.** Everything that makes a backing "fit" — tempo grid, key, chord
choices, where the chorus goes — is extracted from the vocal and fed to the
generator. Then the mix stage exists because two stems that fit musically still
fight each other spectrally.

### What is verified, and what is not

Verified in the sandbox this was built in (2 vCPU, no GPU):

* the whole pipeline with the `procedural` backend (unit tests + a real
  40 s demo run), including mastering to −14 LUFS;
* the `external` backend, end-to-end, including a real trained checkpoint
  (a deliberately tiny 1-epoch run: it proves the plumbing, not the music);
* the training stack: feature caching, a training run (loss decreasing), and
  inference with the checkpoint (`train/tests`).

**Not** executed there, because the dependencies/GPU were unavailable: the
Demucs separation path, the `musicgen` backend and the `ace-step` backend.
They follow the upstream APIs at the time of writing but expect to adjust
argument names. The demo vocal is synthetic — it proves the pipeline runs, not
that the generated music is good.

---

## 2. Quickstart (no GPU, no model download)

```bash
cd songforge
python -m venv .venv && . .venv/bin/activate
pip install -e .                 # numpy only
songforge doctor                 # what is installed

# 1) instant demo: builds a synthetic vocal, generates music, mixes, masters
songforge demo -o demo_out

# 2) your own material - a clean vocal stem
songforge run my_vocal.wav --input-kind vocals --prompt "chill lo-fi, warm keys" -o out

# 3) your own material - a full song (needs Demucs for separation)
pip install "songforge[separate]"
songforge run mysong.mp3 --prompt "indie rock, live drums" -o out
```

Outputs in `out/`: `song.wav` (finished), `instrumental.wav`, `vocals.wav`,
`melody_guide.wav`, `conditioning.json`, `report.json`.

Measured on the 2-vCPU sandbox this was developed in (40 s vocal, procedural
backend): **33 s wall clock**, master at **−14.0 LUFS / −1.0 dBFS peak**, tempo
detected within 0.1 BPM, 75 melody notes transcribed.

---

## 3. What each stage actually does

### 3.1 Separation — `songforge/separate.py`
Demucs (`htdemucs_ft`) splits a song into `vocals` + `accompaniment`. If you
already have a vocal stem (or recorded one), skip it — separation artifacts are
the number-one quality killer downstream. `songforge separate in.mp3 -o stems`.
(MIT-licensed, runs on CPU in minutes.)

### 3.2 Analysis — `songforge/analyze.py`
This is where a vocal becomes *conditioning*:

| Feature | Method | Used for |
|---|---|---|
| tempo + downbeat phase | onset spectral flux → autocorrelation with a log-normal 118 BPM prior | the grid every drum/bass note snaps to |
| key + mode | Krumhansl–Schmuckler chroma correlation | chord vocabulary, melody quantisation |
| melody | normalised-autocorrelation pitch track → median smoothing → key snapping | melody-conditioned models, counter-melody, register choices |
| chords | per-bar chroma triad template matching (diatonic-filtered) | harmony |
| form | per-bar vocal energy → intro/verse/chorus/bridge/outro | arrangement dynamics |

A useful sanity check: `songforge analyze vocals.wav --melody-out guide.wav`.

### 3.3 Generation — `songforge/backends.py` + `procedural.py`
Four interchangeable backends behind one interface
(`generate(conditioning, vocals, duration) -> stereo audio`):

* **`procedural`** — a rule-based arranger: library progressions filtered by the
  vocal's chords, a 16-step drum machine with style patterns, off-beat bass,
  voice-led pads (inversions chosen so the vocal's octave stays clear), kicks
  sidechained into a pumping envelope, section-dependent energy, fills into the
  chorus. No model — and it is completely deterministic on CPU, which makes it
  the best possible *baseline / test fixture* for the neural one.
* **`musicgen`** — Meta MusicGen-Melody, generated in overlapping 25 s windows
  crossfaded together; the melody guide (rendered MIDI, not the raw vocal)
  drives the chroma conditioning.
* **`ace-step`** — ACE-Step 1.x with a text prompt plus the vocal as an audio
  prompt; adapter is small because its Python API is still moving.
* **`external`** — any command line model. **This is how your own trained
  checkpoint plugs in** (see §5.4).

### 3.4 Mix + master — `songforge/mix.py`
1. fit both stems to the same length (short music tail);
2. vocal clean-up: 90 Hz high-pass, split-band de-esser, small plate;
3. loudness-match both stems internally, then **multiband sidechain**: only the
   region above ~180 Hz ducks under the vocal, so the kick and bass keep their
   weight (a plain broadband ducker makes the whole track pump);
4. bus glue (soft 1.6:1), loudness normalisation to −14 LUFS (streaming target;
   −9…−11 for club), look-ahead limiter at −1 dBFS, fade-out;
5. `report.json` with the measurements so you can compare versions.

---

## 4. Train your own model

```bash
cd songforge/train
pip install -r requirements-train.txt        # torch (CUDA wheel strongly recommended)

# 4.1 build the feature cache (mel spectrograms, chroma, beat phase, sections)
python -m songforge_train.prepare_dataset ../dataset_raw ../cache --device cuda

# 4.2 train
python -m songforge_train.train --cache ../cache --out runs/small \
       --config configs/small.json --epochs 200 --device cuda

# 4.3 monitor: runs/small/log.jsonl (train/val loss per step), best.pt saved on improvement

# 4.4 generate with YOUR checkpoint
python -m songforge_train.infer --checkpoint runs/small/best.pt \
       --vocal ../out/vocals.wav --out ../out/ai_instrumental.wav --steps 4 --device cuda
```

### 4.1 Data layout

```
dataset_raw/
  track_0001/{vocals.wav, accompaniment.wav}     # ideal: you already have stems
  track_0002/{vocals.wav, drums.wav, bass.wav, other.wav}
song/                      # or just drop full songs here, Demucs splits them
  track_0003.wav
```

Target: **at least 20 h** to hear learning, 200 h for something you would play
to friends, 1 000 h+ to approach commercial. Prefer *real, human-played*
recordings over synthetic stems — models trained on Demucs-split data learn
Demucs artifacts.

Feature cache per track (~40 MB/h from 22 kHz mel): `vocal_mel` (80 mels),
`target_mel`, `chroma`, `beat` (sin/cos phase), `energy`, `section` ids, key,
BPM. Cached once, so epochs are I/O-cheap.

### 4.2 The model (`train/songforge_train/model.py`)

```
noisy target mel (80) ┐
vocal mel (80)        ├─► conv stem ─► 3–4 down/up U-Net levels ─► 80 ch tanh
conditioning (36)     ┘     (GroupNorm + SiLU + FiLM from the noise-level embedding)
```

* **Conditioning (36 channels/frame):** 12 chroma, 2 beat phase, 1 vocal energy,
  6 section one-hot, 12 key one-hot, 2 mode, 1 normalised BPM.
* **Objective:** denoising regression — corrupt the target mel with
  `x_t = x0 + σ·ε`, `σ ~ log-uniform(0.05, 1.0)`, predict `x0`. Loss = L1 +
  mid-band L1 (mel 10–70) + 0.1 × temporal-difference L1.
* **Sampling:** start from noise at σ=1, predict, re-noise to a lower σ, predict
  again (4 steps by default). Same "predict-the-clean-signal and refine" loop as
  diffusion, one network instead of two.
* **Vocoder:** Griffin-Lim (momentum, 64 iters) inside `codec.py`; drop a
  HiFi-GAN into `hifigan.py` for release quality.
* `configs/small.json` ≈ a few million params, trains on one 12–24 GB GPU.

Why mel + vocoder rather than raw waveform? Raw-audio models (Jukebox, MusicGen)
need tens of thousands of GPU-hours to sound good; a mel target is ~100× cheaper
and the vocoder carries the last mile. Why a mel U-Net rather than a
transformer? At 25 s chunks it fits in memory on a single GPU and trains in
hours, which is what a first "own model" should be.

### 4.3 Expected timeline (single 24 GB GPU, `small.json`, 200 h data)

| Step | Time |
|---|---|
| feature cache (200 h) | 3–6 h CPU (parallelisable) |
| train to first recognisable output | 4–8 h |
| train to "usable" (val plateaus) | 2–5 days |
| inference, 1 min song, 4 steps | 5–20 s + 10–30 s vocoding |

Loss curves that mean something: val loss must fall and **generated audio must
be in key and on beat** — trivially checkable with `songforge analyze` on the
output. If the loss falls but the audio drifts, your conditioning channels are
broken or your data is misaligned (vocals/backing must be sample-aligned; the
cache builder truncates both to the shorter one, so keep pairs exact).

### 4.4 Plugging your checkpoint into the full pipeline

Checkpoints are just another backend — the `external` backend takes a command
template with `{input}` (melody guide wav), `{output}`, `{prompt}`, `{duration}`,
`{key}`, `{bpm}`, `{seed}`:

```bash
songforge run my_vocal.wav --input-kind vocals -o out --backend external \
  --command "python train/songforge_train/infer.py --checkpoint runs/small/best.pt \
             --vocal {input} --out {output} --steps 4"
```

The pipeline then mixes and masters the AI instrumental with the original vocal
and writes `report.json` exactly as with the built-in backends.

---

## 5. Quality: what to listen for, and how to fix it

| Symptom | Likely cause | Fix |
|---|---|---|
| backing is in the wrong key | melody-heavy chroma, weak key confidence | check `conditioning.json`; force key with a forced-key patch in `choose_progression`, or give the model more data |
| drums drift / feel "off" | tempo estimation error (odd meter, rubato) | verify BPM in the report; quantise the vocal or hand-set `--bpm` (analysis has a tempo prior you can edit) |
| vocal sounds buried | no spectral separation; arrangement too dense | raise `--duck`, high-pass the pad bus, reduce arp layer, cut 2–4 kHz on the music |
| instrumental sounds "MIDI" | procedural synthesis, no real timbres | fine-tune a real-audio model (route A/C) or layer sampled instruments |
| metallic / smeared artefacts | Griffin-Lim vocoder | raise `--gl-iter`, or use a HiFi-GAN vocoder |
| pumping | broadband ducking | use the multiband ducker (default) rather than a plain compressor |
| harsh sibilance after summing | stacked de-essing | increase `--vocal-gain` earlier instead of de-essing twice |

Fast objective checks:

```bash
songforge analyze out/instrumental.wav        # same key as the vocal? same BPM?
python -c "from songforge.audio import *; ..."   # report.json already has LUFS/peak
```

---

## 6. Compute and cost (rough)

| Setup | What it can do |
|---|---|
| 4–8 vCPU, no GPU | this pipeline with `procedural`; Demucs separation; mel analysis |
| 8–12 GB GPU | MusicGen-small/Melody inference; training the tiny config |
| 16–24 GB GPU | ACE-Step/musicgen-medium, HiFi-GAN vocoding, `small.json` training |
| 24–80 GB GPU | `medium.json` training, fine-tuning foundation models |
| rented A100/H100 (€1–3/h) | 200 h dataset: cache overnight, train for 2–4 days for a few hundred € |

For a first serious run, one rented 24 GB GPU for a week beats a local laptop.

---

## 7. Legal / ethical guardrails

* **Training data**: only use recordings you own, have a licence for, or that are
  explicitly licensed for training (e.g. your own sessions, commissioned
  recordings, appropriately-licensed libraries). "It was on YouTube" is not a
  licence, and models memorise.
* **Vocal likeness**: a vocal stem is personal data and, in many jurisdictions, a
  protected likeness. Get consent before generating a "finished song" around
  someone's voice, and disclose that the backing is AI-generated.
* **Model licences** (check the current text, they change):
  MusicGen/MusicGen-Melody weights are **CC-BY-NC 4.0** (no commercial use);
  audiocraft code is MIT. ACE-Step and DiffRhythm are **Apache-2.0**.
  Demucs is MIT. Dataset licences differ wildly: MUSDB18 and MedleyDB are
  non-commercial/research, Slakh2100 is a synthetic dataset with its own terms.
* **Distribution**: streaming platforms increasingly require disclosure of AI
  content; some reject fully synthetic vocals.

---

## 8. Reference: paper trail

* **StemGen** (Meta, 2023) — non-autoregressive transformer that *listens* to a
  context stem and generates the target stem; the closest published match to
  "vocal in, music out". <https://arxiv.org/abs/2312.08723>
* **MusicGen / MusicGen-Melody** (Meta) — autoregressive, text + melody
  conditioning. <https://github.com/facebookresearch/audiocraft>
* **ACE-Step** — open full-song model (text+lyrics, audio prompt, LoRA).
  <https://github.com/ace-step/ACE-Step>
* **DiffRhythm** — open full-length song model via latent diffusion.
  <https://github.com/ASLP-lab/DiffRhythm>
* **Demucs** — music source separation. <https://github.com/facebookresearch/demucs>
* **MusicGen-Stem / STAGE** — multi-stem generation and prefix-based
  accompaniment conditioning (papers, no weights).

---

## 9. Repo map

```
songforge/
├── songforge/
│   ├── audio.py        WAV/ffmpeg I/O, resampling, filters, LUFS, limiter, WSOLA
│   ├── analyze.py      tempo, key, melody, chords, form  → conditioning
│   ├── separate.py     Demucs wrapper
│   ├── procedural.py   rule-based arranger + synthesiser (the CPU backend)
│   ├── backends.py     musicgen / ace-step / external / procedural
│   ├── render.py       melody guide rendering, prompt building
│   ├── mix.py          vocal cleanup, multiband duck, master
│   ├── pipeline.py     the 5-stage orchestration
│   ├── demo.py         synthetic vocal for a zero-asset demo + tests
│   └── cli.py          `songforge run|separate|analyze|generate|mix|demo|doctor`
├── train/
│   ├── songforge_train/{dataset,model,train,codec,infer}.py
│   ├── configs/{small,medium}.json
│   └── tests/
├── tests/              20 tests, numpy only, ~12 s
└── README.md           this file
```

Run the tests:

```bash
cd songforge && python -m unittest discover -s tests   # pipeline (numpy only)
cd train && python -m unittest discover -s tests       # model (needs torch)
```
