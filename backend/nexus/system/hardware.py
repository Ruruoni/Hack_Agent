"""Hardware / resource detection and practical estimates."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

import psutil


def _gpus() -> list[dict]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "--query-gpu=name,memory.total,memory.free,driver_version",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=8).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    gpus = []
    for line in out.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) >= 4:
            try:
                gpus.append({"name": p[0], "vram_total_mb": int(p[1]), "vram_free_mb": int(p[2]), "driver": p[3]})
            except ValueError:
                pass
    return gpus


def snapshot(data_dir: str | Path = ".", active_jobs: int = 0) -> dict[str, Any]:
    vm = psutil.virtual_memory()
    path = Path(data_dir)
    path.mkdir(parents=True, exist_ok=True)
    du = shutil.disk_usage(path)
    gpus = _gpus()
    torch_info: dict[str, Any] = {"installed": False}
    try:
        import importlib.util
        if importlib.util.find_spec("torch"):
            import torch  # type: ignore
            torch_info = {"installed": True, "version": torch.__version__, "cuda": torch.cuda.is_available()}
    except Exception:  # noqa: BLE001 - torch import can fail in many ways; only informational
        pass
    return {
        "os": f"{platform.system()} {platform.release()}", "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(), "cpu_percent": psutil.cpu_percent(interval=None),
        "ram_total_mb": vm.total // 2**20, "ram_available_mb": vm.available // 2**20,
        "disk_total_mb": du.total // 2**20, "disk_free_mb": du.free // 2**20,
        "gpus": gpus, "torch": torch_info, "background_jobs": active_jobs,
    }


def default_batch_size(hw: dict) -> int:
    ram = hw.get("ram_available_mb", 2048)
    return 16 if ram < 3000 else 64 if ram < 12000 else 128


def estimate_ingest(total_bytes: int, throughput_bps: float = 2_000_000) -> dict:
    secs = total_bytes / max(throughput_bps, 1)
    return {"bytes": total_bytes, "est_seconds": round(secs, 1),
            "note": "Rough estimate at ~2 MB/s with the local embedder; real speed depends on the embedding model."}


def check_disk(path: str | Path, need_bytes: int, reserve_mb: int = 500) -> tuple[bool, int]:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(p).free
    return free - need_bytes >= reserve_mb * 2**20, free
