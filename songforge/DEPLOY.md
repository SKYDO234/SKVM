# Letting other people use it

`songforge serve` turns the pipeline into a small web app + JSON API: upload a
vocal in a browser, get three files back (finished song, instrumental, cleaned
vocal), with progress streaming while it renders. This file covers every way to
put that in front of other people, cheapest first.

```bash
pip install "songforge[server]"
songforge serve                                  # http://localhost:8000
songforge serve --max-seconds 60 --workers 2 --ttl-hours 6 --api-key secret
```

Everything is bounded by flags/env vars, because a public URL is otherwise a way
for strangers to burn your CPU and disk:

| Setting | Flag | Env | Default |
|---|---|---|---|
| max audio length | `--max-seconds` | `SONGFORGE_MAX_SECONDS` | 60 |
| max upload | `--max-upload-mb` | `SONGFORGE_MAX_UPLOAD_MB` | 25 |
| parallel renders | `--workers` | `SONGFORGE_WORKERS` | 1 |
| file retention | `--ttl-hours` | `SONGFORGE_TTL_HOURS` | 6 |
| per-address limits | `--rate-per-hour/--rate-per-day` | `SONGFORGE_RATE_*` | 10 / 40 |
| access code | `--api-key` | `SONGFORGE_API_KEY` | none |
| backend | `--backend` | `SONGFORGE_BACKEND` | procedural |
| job storage | `--instance` | `SONGFORGE_INSTANCE` | `instance/` |

Flags override environment variables; unset flags fall back to `SONGFORGE_*`, so
a container can be reconfigured without touching its command line.

The backend is chosen by **you**, never by the request, so a public deployment
cannot be turned into a remote-shell. (`SONGFORGE_EXTERNAL_COMMAND` is only read
from the environment, never from a form field.)

---

## 1. Which route fits whom

| Situation | Route | Cost | Setup |
|---|---|---|---|
| Just show a friend / a few users | local + tunnel (§2) | €0 | 2 min |
| Public demo, CPU backend, no GPU | Hugging Face Spaces (§4) or a €5 VPS (§3) | €0–5/mo | 15 min |
| Real traffic, your own domain | VPS + Docker + Caddy (§3) | €5–20/mo | 30 min |
| Real musical quality (MusicGen/ACE-Step/your checkpoint) | GPU host (§5) | €0.3–2/hr | 1–2 h |
| Nobody should run a server at all | Colab notebook (§7) | €0 | 5 min |
| Team/CI integration | the API (§8) | — | minutes |

Rule of thumb with the CPU backend, measured on a 2-vCPU box: **a 25 s vocal
takes ~18 s to render**. One worker ≈ 3 songs/minute; two workers double that
until the CPU saturates. Each job writes ~12 MB of WAVs (25 s at 44.1 kHz
stereo), so with 6 h retention and 40 users/day you need < 5 GB of disk.

---

## 2. Zero-cost public link in two minutes

```bash
songforge serve --port 8000 &
# Cloudflare quick tunnel (no account, random URL, dies with the process)
cloudflared tunnel --url http://localhost:8000
# or: ngrok http 8000
```

You get a `https://…trycloudflare.com` URL you can paste anywhere. Good for a
demo to friends, useless for anything permanent (the URL changes, your laptop
sleeps). For permanent, use a named tunnel or route 3.

---

## 3. Your own VPS (recommended for a real public service)

Any 2 vCPU / 4 GB box (Hetzner CX22 ≈ €4/mo, DigitalOcean, Contabo…):

```bash
ssh root@your-server
apt update && apt install -y docker.io docker-compose-v2 git
git clone https://github.com/SKYDO234/SKVM.git && cd SKVM/songforge
docker compose -f deploy/docker-compose.yml up -d --build
curl localhost:8000/api/health          # {"status":"ok",...}
```

Now put TLS in front of it. With Caddy (automatic Let's Encrypt):

```caddyfile
# /etc/caddy/Caddyfile
songforge.example.com {
    encode zstd gzip
    request_body {
        max_size 30MB
    }
    reverse_proxy 127.0.0.1:8000
}
```

```bash
apt install -y caddy && systemctl reload caddy
```

Point a DNS `A` record at the box first. That is the whole deployment: HTTPS,
upload limit at the edge, rate limits inside the app.

**Sizing:** CPU is the bottleneck (`numpy` FFTs). 2 vCPU ≈ 1 job / 18 s; 4 vCPU
with `SONGFORGE_WORKERS=3` ≈ 3 jobs / 20 s. Add RAM (≥ 1 GB free per worker;
a 60 s stereo file plus intermediate buffers is ~300 MB peak).

**Updating:** `git pull && docker compose -f deploy/docker-compose.yml up -d --build`.

---

## 4. Hugging Face Spaces (free public demo)

1. <https://huggingface.co/new-space> → **SDK: Docker** → *Blank* → CPU basic
   (2 vCPU, 16 GB RAM, free).
2. In the Space repo, put the contents of `songforge/` at the root and copy
   `deploy/hf-space/README.md` to the Space's `README.md` (the YAML header sets
   `sdk: docker` and `app_port: 7860`), plus `deploy/Dockerfile` as `Dockerfile`.
3. Change the last line of the Dockerfile to also set the port:

   ```dockerfile
   ENV SONGFORGE_PORT=7860
   CMD ["songforge", "serve", "--host", "0.0.0.0", "--port", "7860"]
   ```

4. Space → **Settings → Variables**: `SONGFORGE_MAX_SECONDS=45`,
   `SONGFORGE_TTL_HOURS=2`, `SONGFORGE_WORKERS=2`.
5. Push. The first build takes ~5 minutes; you get a public URL like
   `https://<user>-songforge.hf.space`.

Caveats: free Spaces **sleep** after inactivity (first visitor waits ~30 s), the
filesystem is **ephemeral** (jobs vanish on restart — fine, they are capped by
TTL anyway), and the CPU backend is the only thing that runs comfortably. For
MusicGen, use a **ZeroGPU** Space (H200 slices, `spaces.GPU` decorator — needs a
small wrapper around `MusicGenBackend`) or route 5.

---

## 5. GPU hosts (when you want real musical quality)

The CPU `procedural` backend is a placeholder for quality. MusicGen / ACE-Step /
your own checkpoint need a GPU:

| Host | Shape | Rough price | Good for |
|---|---|---|---|
| RunPod / Vast.ai | RTX 4090 24 GB, on demand | $0.3–0.6/hr | MusicGen-medium, your `small.json` model |
| RunPod / Lambda | A100 40–80 GB | $1.2–2.5/hr | `medium.json` training, big checkpoints |
| Modal / Replicate | serverless per-second | ~$0.0005–0.002/s | spiky traffic, no idle cost |
| HF ZeroGPU Space | H200 slices | free quota | public demos of a model |

Deployment is the same container with `SONGFORGE_BACKEND=musicgen` (install
`torch`+`audiocraft` in the image, expose the model cache as a volume so it is
downloaded once), or `SONGFORGE_BACKEND=external` with
`SONGFORGE_EXTERNAL_COMMAND` pointing at your checkpoint's `infer.py`.

Practical numbers: MusicGen-melody on a 4090 renders 25 s of audio in ~20–40 s;
your own small U-Net + Griffin-Lim vocoder is faster (~5–15 s) but the music is
only as good as your dataset.

Serverless (Modal/Replicate) wants **one request = one song**; set
`SONGFORGE_WORKERS=1` and let the platform scale instances, and use a webhook
or polling instead of holding a connection for minutes.

---

## 6. Under the hood: how a job flows

```
POST /api/jobs (multipart)          -> 202 {id, status:"queued"}
  ├─ size/extension/duration/rate checks      -> 400/413/429
  ├─ write instance/jobs/<id>/input.wav
  └─ worker thread -> run_pipeline(max_seconds=..., backend=<server>)
GET  /api/jobs/<id>                 -> status, elapsed, log lines, file URLs
GET  /api/jobs/<id>/audio/{song|instrumental|vocals}   -> wav
GET  /api/jobs/<id>/report          -> report.json (LUFS, peaks, conditioning)
DELETE /api/jobs/<id>               -> remove files now
GET  /docs                          -> OpenAPI UI (FastAPI)
```

Storage is a plain directory, the queue is a thread pool, the limiter is an
in-memory sliding window. That is deliberate: one container, no Redis, no
database, nothing to operate. When you outgrow it:

* replace `JobManager._work` with a Celery/RQ task (same function body);
* put the instance directory on object storage if you scale past one machine;
* add a webhook `POST` on completion so clients do not poll.

---

## 7. No server at all: Colab

`notebooks/colab_songforge.ipynb` installs the package, generates a demo song,
lets you upload your own vocal and downloads the result. Free CPU Colab renders
a 30 s song in about a minute. Share the notebook — everyone can run their own
copy with their own files. (Gradio is another option: an `iface.launch(share=True)`
15-liner around `run_pipeline` — use it for a quick demo, not for production.)

---

## 8. Using the API from code

```bash
# upload
ID=$(curl -s -F "file=@vocal.wav" -F "style=lofi" -F "prompt=warm keys" \
     -H "X-API-Key: $KEY" https://songforge.example.com/api/jobs | jq -r .id)

# poll
until [ "$(curl -s -H "X-API-Key: $KEY" .../api/jobs/$ID | jq -r .status)" = done ]; do sleep 2; done

# download
curl -OJ .../api/jobs/$ID/audio/song
```

```python
import time, requests

BASE = "https://songforge.example.com"
headers = {"X-API-Key": "secret"}

with open("vocal.wav", "rb") as fh:
    job = requests.post(f"{BASE}/api/jobs", headers=headers,
                        files={"file": fh},
                        data={"style": "lofi", "prompt": "warm tape"},
                        timeout=60).json()

while True:
    state = requests.get(f"{BASE}/api/jobs/{job['id']}", headers=headers).json()
    if state["status"] in ("done", "error"):
        break
    print(state["status"], state["elapsed"], "s")
    time.sleep(2)

if state["status"] == "error":
    raise SystemExit(state["error"])

for kind, url in state["files"].items():
    audio = requests.get(BASE + url, headers=headers).content
    open(f"{kind}.wav", "wb").write(audio)
print(state["summary"])          # {'lufs': -14.0, 'peak_db': -1.0, ...}
```

Embed in a page:

```html
<iframe src="https://songforge.example.com" width="100%" height="900"
        style="border:0;border-radius:16px" title="songforge"></iframe>
```

---

## 9. Running a public service: checklist

**Before you open the URL**

- [ ] HTTPS (Caddy/Cloudflare) — people upload personal recordings
- [ ] `SONGFORGE_MAX_SECONDS` + `MAX_UPLOAD_MB` + rate limits set for your CPU
- [ ] `SONGFORGE_TTL_HOURS` low (1–6 h), `/data` on a volume you can wipe
- [ ] container runs as non-root (the bundled Dockerfile does), read-only root fs if you can
- [ ] access code or login if it is not meant for the whole internet
- [ ] `/api/health` monitored (uptime ping every minute)
- [ ] log retention defined; IP addresses used for rate limiting are personal data

**Abuse and legal**

- Uploading a vocal is uploading a **voice**: get consent, say what you do with
  it, delete it (the TTL does), and never train on user uploads without explicit
  permission.
- Users may upload vocals from copyrighted recordings. Require them to confirm
  they have the rights; keep a takedown contact; log the upload timestamp.
- Generated music is **AI-made** — say so in the UI (`SONGFORGE_NOTICE`) and in
  the files' metadata/report. Some distributors and platforms require this.
- **Model licences matter for a public service**: MusicGen weights are
  CC-BY-NC (no commercial hosting), ACE-Step and DiffRhythm are Apache-2.0
  (keep their NOTICE), Demucs is MIT. Your own weights are yours.
- Terms of service + privacy page: what is stored, for how long, who can see a
  share link (anyone with the link — job IDs are random 12-hex strings, not
  auth), refunds/limits, acceptable use.

**Do not**

- let the request choose the backend/command (RCE),
- keep uploads forever "for debugging",
- serve more concurrent workers than you have RAM for (each job peaks ~300 MB),
- put a public URL in front of a `--reload` dev server.

---

## 10. What a small deployment costs

| Item | Spec | Monthly |
|---|---|---|
| VPS, CPU backend | 2 vCPU / 4 GB | €4–6 |
| VPS, bigger | 4 vCPU / 8 GB, 3 workers | €12–20 |
| Domain | optional | €1 |
| GPU, on demand | 4090, 1 h/day of real use | ~€12 |
| GPU, always-on | 4090 24/7 | €250–450 |
| HF Space, CPU | free tier | €0 |
| Storage | 5 GB volume | included / <€1 |

Start on the free/five-euro end: the procedural backend is genuinely usable for
demos, and the same container switches to a real model by changing one
environment variable when you have a GPU.
