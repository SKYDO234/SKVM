"""Training loop for the vocal -> accompaniment U-Net.

Usage
-----
    python -m songforge_train.prepare_dataset raw/ cache/       # build features
    python -m songforge_train.train --cache cache/ --out runs/small \\
        --config configs/small.json --epochs 200 --device cuda

The loop is deliberately plain: AdamW + cosine schedule with warmup, gradient
clipping, EMA weights, periodic validation, resumable checkpoints, JSONL logs.
Everything you need to reproduce a run is written into the checkpoint.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import random
import time
from pathlib import Path

import numpy as np

try:
    import torch
    import torch.nn.functional as F
    from torch.utils.data import DataLoader, Dataset
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "training needs PyTorch:\n  pip install -r requirements-train.txt\n"
        f"(import error: {exc})")

from .dataset import FPS, N_MELS, SR
from .model import (COND_CHANNELS, SECTION_LABELS, VocalToMusicUNet, count_parameters,
                    sample_sigma)

DEFAULT_CONFIG = {
    "channels": [64, 128, 256, 384],
    "blocks": 2,
    "emb_dim": 256,
    "crop_seconds": 8.0,
    "batch_size": 8,
    "lr": 2e-4,
    "weight_decay": 0.01,
    "warmup_steps": 500,
    "epochs": 200,
    "grad_clip": 1.0,
    "ema_decay": 0.999,
    "sigma_min": 0.05,
    "sigma_max": 1.0,
    "smooth_loss_weight": 0.1,
    "val_fraction": 0.05,
    "num_workers": 2,
    "amp": True,
    "seed": 1234,
}


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
class PairDataset(Dataset):
    """Random crops from the cached .npz feature bundles."""

    def __init__(self, cache_dir: str, crop_seconds: float = 8.0, files=None,
                 train: bool = True, seed: int = 0):
        cache = Path(cache_dir)
        manifest = cache / "manifest.json"
        if manifest.exists():
            meta = json.loads(manifest.read_text(encoding="utf-8"))
            names = [t["file"] for t in meta["tracks"] if not t.get("failed")]
        else:
            names = [p.name for p in sorted(cache.glob("*.npz"))]
        if files is not None:
            names = [n for n in names if n in files]
        if not names:
            raise SystemExit(f"no cached tracks in {cache}; run prepare_dataset first")
        self.paths = [cache / n for n in names]
        self.crop = max(32, int(round(crop_seconds * FPS)))
        self.train = train
        self.rng = np.random.default_rng(seed)

    def __len__(self) -> int:
        return len(self.paths) * (50 if self.train else 1)

    def _load(self, idx: int) -> dict:
        with np.load(self.paths[idx]) as z:
            return {k: z[k] for k in z.files}

    def __getitem__(self, idx: int):
        item = self._load(idx % len(self.paths))
        t_total = len(item["vocal_mel"])
        crop = min(self.crop, t_total)
        if self.train and t_total > crop:
            start = int(self.rng.integers(0, t_total - crop))
        else:
            start = 0
        sl = slice(start, start + crop)

        vocal = item["vocal_mel"][sl].astype(np.float32)
        target = item["target_mel"][sl].astype(np.float32)
        chroma = item["chroma"][sl].astype(np.float32)
        beat = item["beat"][sl].astype(np.float32)
        energy = item["energy"][sl].astype(np.float32)
        section = item["section"][sl].astype(np.int64)

        # global conditioning broadcast along time
        key = np.zeros((crop, 12), dtype=np.float32)
        key[:, int(item["key_root"]) % 12] = 1.0
        mode = np.zeros((crop, 2), dtype=np.float32)
        mode[:, int(item["key_mode"])] = 1.0
        bpm = np.full((crop, 1), (float(item["bpm"]) - 70.0) / 100.0, dtype=np.float32)
        sec = np.zeros((crop, len(SECTION_LABELS)), dtype=np.float32)
        sec[np.arange(crop), np.clip(section, 0, len(SECTION_LABELS) - 1)] = 1.0

        cond = np.concatenate([chroma, beat, energy, sec, key, mode, bpm], axis=1)
        assert cond.shape[1] == COND_CHANNELS, cond.shape

        # pad short crops (only happens for very short tracks)
        if crop < self.crop:
            pad = self.crop - crop
            vocal = np.pad(vocal, ((0, pad), (0, 0)))
            target = np.pad(target, ((0, pad), (0, 0)))
            cond = np.pad(cond, ((0, pad), (0, 0)))

        return {
            "vocal_mel": torch.from_numpy(np.ascontiguousarray(vocal.T)),
            "target_mel": torch.from_numpy(np.ascontiguousarray(target.T)),
            "cond": torch.from_numpy(np.ascontiguousarray(cond.T)),
        }


def split_files(cache_dir: str, val_fraction: float, seed: int = 0) -> tuple[list, list]:
    cache = Path(cache_dir)
    names = sorted(p.name for p in cache.glob("*.npz"))
    rng = random.Random(seed)
    rng.shuffle(names)
    n_val = max(1, int(round(len(names) * val_fraction))) if len(names) > 1 else 0
    return names[n_val:], names[:n_val]


# --------------------------------------------------------------------------- #
# losses
# --------------------------------------------------------------------------- #
def training_loss(pred: torch.Tensor, target: torch.Tensor,
                  smooth_weight: float = 0.1) -> tuple[torch.Tensor, dict]:
    l1 = F.l1_loss(pred, target)
    smooth = F.l1_loss(pred[:, :, 1:] - pred[:, :, :-1], target[:, :, 1:] - target[:, :, :-1])
    # emphasise the perceptually important mid band (mel 10..70)
    band = slice(10, 70)
    band_l1 = F.l1_loss(pred[:, band], target[:, band])
    total = 0.5 * l1 + 0.5 * band_l1 + smooth_weight * smooth
    return total, {"l1": float(l1.detach()), "band_l1": float(band_l1.detach()),
                   "smooth": float(smooth.detach())}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class EMA:
    def __init__(self, model: torch.nn.Module, decay: float = 0.999):
        self.decay = decay
        self.shadow = copy.deepcopy(model).eval()
        for p in self.shadow.parameters():
            p.requires_grad_(False)

    @torch.no_grad()
    def update(self, model: torch.nn.Module) -> None:
        for s, p in zip(self.shadow.state_dict().values(), model.state_dict().values()):
            if s.dtype.is_floating_point:
                s.mul_(self.decay).add_(p.detach(), alpha=1.0 - self.decay)
            else:
                s.copy_(p)


def lr_at(step: int, base_lr: float, warmup: int, total: int) -> float:
    if step < warmup:
        return base_lr * (step + 1) / max(1, warmup)
    progress = min(1.0, (step - warmup) / max(1, total - warmup))
    return base_lr * (0.5 * (1.0 + math.cos(math.pi * progress)))


def load_config(path: str | None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path:
        cfg.update(json.loads(Path(path).read_text(encoding="utf-8")))
    return cfg


def save_checkpoint(path: Path, model, ema, optimizer, cfg, step, epoch, best_val) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save({
        "model": model.state_dict(),
        "ema": ema.shadow.state_dict() if ema else None,
        "optimizer": optimizer.state_dict() if optimizer else None,
        "config": cfg, "step": step, "epoch": epoch, "best_val": best_val,
        "n_mels": N_MELS, "sr": SR, "fps": FPS,
    }, path)


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
@torch.no_grad()
def evaluate(model, loader, device, cfg, max_batches: int = 20) -> float:
    model.eval()
    total, n = 0.0, 0
    for i, batch in enumerate(loader):
        if i >= max_batches:
            break
        target = batch["target_mel"].to(device)
        cond = batch["cond"].to(device)
        sigma = torch.full((target.shape[0],), 0.35, device=device)
        noisy = target + torch.randn_like(target) * 0.35
        pred = model(noisy, batch["vocal_mel"].to(device), cond, sigma)
        loss, _ = training_loss(pred, target, cfg["smooth_loss_weight"])
        total += float(loss)
        n += 1
    model.train()
    return total / max(1, n)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def train(args) -> dict:
    cfg = load_config(args.config)
    for key in ("epochs", "batch_size", "lr", "crop_seconds", "num_workers"):
        if getattr(args, key, None) is not None:
            cfg[key] = getattr(args, key)
    torch.manual_seed(cfg["seed"])
    random.seed(cfg["seed"])
    np.random.seed(cfg["seed"])

    device = torch.device(args.device or ("cuda" if torch.cuda.is_available() else "cpu"))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    train_files, val_files = split_files(args.cache, cfg["val_fraction"], cfg["seed"])
    if args.val_cache:
        _, val_files = split_files(args.val_cache, 1.0, cfg["seed"])
    ds_train = PairDataset(args.cache, cfg["crop_seconds"], files=train_files, train=True,
                           seed=cfg["seed"])
    ds_val = PairDataset(args.val_cache or args.cache, cfg["crop_seconds"],
                         files=val_files or None, train=False, seed=cfg["seed"])
    loader = DataLoader(ds_train, batch_size=cfg["batch_size"], shuffle=True,
                        num_workers=cfg["num_workers"], drop_last=True,
                        pin_memory=device.type == "cuda")
    val_loader = DataLoader(ds_val, batch_size=cfg["batch_size"], shuffle=False,
                            num_workers=0)

    model = VocalToMusicUNet(channels=tuple(cfg["channels"]), blocks=cfg["blocks"],
                             emb_dim=cfg["emb_dim"]).to(device)
    print(f"model: {count_parameters(model) / 1e6:.1f}M params | device {device}")
    print(f"train crops: {len(ds_train)} | val clips: {len(ds_val)}")
    ema = EMA(model, cfg["ema_decay"])
    opt = torch.optim.AdamW(model.parameters(), lr=cfg["lr"],
                            weight_decay=cfg["weight_decay"])
    scaler = torch.amp.GradScaler("cuda", enabled=bool(cfg["amp"]) and device.type == "cuda")
    steps_per_epoch = max(1, len(loader))
    total_steps = steps_per_epoch * cfg["epochs"]

    step, start_epoch, best_val = 0, 0, float("inf")
    if args.resume:
        ckpt = torch.load(args.resume, map_location="cpu")
        model.load_state_dict(ckpt["model"])
        if ckpt.get("ema"):
            ema.shadow.load_state_dict(ckpt["ema"])
        if ckpt.get("optimizer"):
            opt.load_state_dict(ckpt["optimizer"])
        step = int(ckpt.get("step", 0))
        start_epoch = int(ckpt.get("epoch", 0)) + 1
        best_val = float(ckpt.get("best_val", float("inf")))
        print(f"resumed {args.resume} at epoch {start_epoch} step {step}")

    log_path = out / "log.jsonl"
    history = []
    for epoch in range(start_epoch, cfg["epochs"]):
        model.train()
        t0 = time.time()
        running = 0.0
        for batch in loader:
            vocal = batch["vocal_mel"].to(device, non_blocking=True)
            target = batch["target_mel"].to(device, non_blocking=True)
            cond = batch["cond"].to(device, non_blocking=True)

            sigma = sample_sigma(target.shape[0], device, cfg["sigma_min"], cfg["sigma_max"])
            noisy = target + torch.randn_like(target) * sigma.reshape(-1, 1, 1)

            lr = lr_at(step, cfg["lr"], cfg["warmup_steps"], total_steps)
            for group in opt.param_groups:
                group["lr"] = lr

            with torch.autocast("cuda", enabled=scaler.is_enabled()):
                pred = model(noisy, vocal, cond, sigma)
                loss, parts = training_loss(pred, target, cfg["smooth_loss_weight"])
            opt.zero_grad(set_to_none=True)
            scaler.scale(loss).backward()
            scaler.unscale_(opt)
            torch.nn.utils.clip_grad_norm_(model.parameters(), cfg["grad_clip"])
            scaler.step(opt)
            scaler.update()
            if step % args.ema_every == 0:
                ema.update(model)

            running += float(loss)
            if step % args.log_every == 0:
                rec = {"step": step, "epoch": epoch, "lr": round(lr, 7),
                       "loss": round(float(loss), 4),
                       **{k: round(v, 4) for k, v in parts.items()},
                       "sigma_mean": round(float(sigma.mean()), 4)}
                history.append(rec)
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec) + "\n")
            step += 1

        train_loss = running / steps_per_epoch
        val_loss = evaluate(ema.shadow, val_loader, device, cfg)
        elapsed = time.time() - t0
        print(f"epoch {epoch:3d} | train {train_loss:.4f} | val(ema) {val_loss:.4f} "
              f"| lr {lr:.2e} | {elapsed:.1f}s")
        save_checkpoint(out / "last.pt", model, ema, opt, cfg, step, epoch, best_val)
        if val_loss < best_val:
            best_val = val_loss
            save_checkpoint(out / "best.pt", model, ema, opt, cfg, step, epoch, best_val)
            print(f"  new best (val {best_val:.4f}) -> {out / 'best.pt'}")
        if args.max_epochs and epoch + 1 >= args.max_epochs:
            break

    save_checkpoint(out / "final.pt", model, ema, opt, cfg, step, epoch, best_val)
    (out / "history.json").write_text(json.dumps(history, indent=2), encoding="utf-8")
    print(f"done: {out / 'final.pt'} (best val {best_val:.4f})")
    return {"best_val": best_val, "steps": step}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Train the vocal->accompaniment model")
    p.add_argument("--cache", required=True, help="feature cache from prepare_dataset")
    p.add_argument("--val-cache", default=None, help="separate cache for validation")
    p.add_argument("--out", required=True, help="run directory for checkpoints/logs")
    p.add_argument("--config", default=None, help="JSON config overriding defaults")
    p.add_argument("--device", default=None, help="cpu / cuda / mps")
    p.add_argument("--resume", default=None, help="checkpoint to resume from")
    p.add_argument("--epochs", type=int, default=None)
    p.add_argument("--batch-size", type=int, default=None)
    p.add_argument("--lr", type=float, default=None)
    p.add_argument("--crop-seconds", type=float, default=None)
    p.add_argument("--num-workers", type=int, default=None)
    p.add_argument("--max-epochs", type=int, default=None, help="stop early (smoke tests)")
    p.add_argument("--log-every", type=int, default=20)
    p.add_argument("--ema-every", type=int, default=10)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    train(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
