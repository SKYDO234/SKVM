# Training your own vocal → accompaniment model

Everything here is plain PyTorch. The goal of this folder is a checkpoint you can
point at any vocal and get a backing track that is in the same key, on the same
grid, and follows the song's form.

```
dataset.py    paired stems → cached numpy features (.npz)
model.py      conditional 1-D U-Net, denoising objective, iterative sampler
train.py      AdamW + cosine + EMA, resumable, JSONL logs
codec.py      mel → audio (Griffin-Lim, HiFi-GAN adapter)
infer.py      vocal → instrumental → optionally mixed song
configs/      small.json (one 12–24 GB GPU), medium.json
```

## 1. Install

```bash
pip install -r requirements-train.txt      # torch + numpy + soundfile
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
```

## 2. Data

```
dataset_raw/
  track_0001/vocals.wav, accompaniment.wav          # best
  track_0002/vocals.wav, drums.wav, bass.wav, other.wav
songs/track_0003.wav                                # gets Demucs-split for you
```

Rules that matter more than architecture:

* **sample-aligned pairs** — the vocal and the backing must start together; the
  cache truncates both to the shorter file, so a 200 ms offset silently teaches
  the model nonsense;
* **clean vocals** — bleed from the backing leaks the answer into the input;
* **diversity** — genres, tempos (60–180), keys, languages, production styles;
* **real recordings** over Demucs-split ones (a split dataset teaches Demucs
  artifacts);
* 20 h = demo, 200 h = usable, 1 000 h+ = good. Slakh2100 (synthetic, 145 h) is
  a fine first dataset for a smoke run; MUSDB18 / MedleyDB are research-only.

```bash
python -m songforge_train.prepare_dataset ../dataset_raw ../cache --device cuda
# -> ../cache/<track>.npz + manifest.json   (22.05 kHz, hop 512, 80 mels, 43 fps)
```

## 3. Train

```bash
python -m songforge_train.train --cache ../cache --out runs/small \
    --config configs/small.json --epochs 200 --device cuda --resume runs/small/last.pt
```

What is logged: `log.jsonl` per step (`loss`, `l1`, `band_l1`, `smooth`, `lr`,
sigma), `history.json` per epoch, `best.pt` (EMA weights, best val) and
`last.pt` (resume). Config values and shapes are stored in the checkpoint, so
inference can never load a mismatched model.

Knobs worth touching first:

| Knob | Effect |
|---|---|
| `crop_seconds` | longer = more musical context, more VRAM |
| `channels` / `blocks` | capacity; watch val loss before growing |
| `batch_size` + `lr` | 8 × 2e-4 is a sane start for `small.json` |
| `sigma_min` | lower = sharper detail at fine noise levels |
| `smooth_loss_weight` | higher = smoother, less "grainy" output |

## 4. Listen

```bash
python -m songforge_train.infer --checkpoint runs/small/best.pt \
    --vocal ../out/vocals.wav --out /tmp/ai_music.wav --song /tmp/ai_song.wav \
    --steps 4 --gl-iter 64 --device cuda
```

`--steps` trades speed for coherence; `--vocoder hifigan --vocoder-ckpt …` after
you drop an architecture into `hifigan.py`.

## 5. Sanity checks

```bash
cd .. && python -m unittest discover -s tests          # pipeline, numpy only
cd train && python -m unittest discover -s tests       # model/codec/dataset
```

Objective checks on the generated instrumental:

```bash
songforge analyze /tmp/ai_music.wav     # key and BPM must match the vocal
python -c "import json;r=json.load(open('/tmp/report.json'));print(r['out'])"
```

## 6. Where the quality comes from

1. data alignment and cleanliness (biggest)
2. dataset size and diversity
3. conditioning quality — bad tempo/key labels poison training
4. model capacity (last)

If output is muddy: the vocal mel is dominating the input; try dropout on the
vocal-mel channels, or predict a *residual* on top of a simple arrangement.
If output collapses to a drone: check that `sigma` is actually varying (see
`log.jsonl`) and that the target mel is not near-silent (inspect a cached
`.npz`).
