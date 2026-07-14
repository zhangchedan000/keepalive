#!/usr/bin/env python3
"""
OCI instance activity service.

Compared with the previous fixed-ratio stress loop, this version uses:
- per-host deterministic profiles;
- long low-load periods plus short compute jobs;
- gradual cache-sized memory changes;
- low-frequency disk verification;
- lightweight health checks and occasional bounded downloads.

It does not rename or hide the process. Resource use remains bounded by systemd.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import multiprocessing
import os
import random
import socket
import tempfile
import time
import urllib.request
from pathlib import Path

HOST = socket.gethostname()
SEED = int(hashlib.sha256(HOST.encode()).hexdigest()[:16], 16)
PROFILE = random.Random(SEED)

DAY_START = PROFILE.randint(7, 10)
DAY_END = PROFILE.randint(21, 24)

# Each host receives a different long-term range.
CPU_IDLE_MIN = PROFILE.uniform(0.02, 0.06)
CPU_IDLE_MAX = PROFILE.uniform(0.07, 0.13)
CPU_ACTIVE_MIN = PROFILE.uniform(0.16, 0.24)
CPU_ACTIVE_MAX = PROFILE.uniform(0.28, 0.42)
CPU_BURST_MAX = PROFILE.uniform(0.45, 0.62)

# Memory is a cache-like allocation, not a fixed percentage target.
MEM_MIN_PCT = PROFILE.uniform(0.08, 0.14)
MEM_MAX_PCT = PROFILE.uniform(0.18, 0.30)
MEM_STEP_MB = PROFILE.randint(16, 48)

HEALTH_URLS = [
    "https://www.cloudflare.com/cdn-cgi/trace",
    "https://api.github.com/rate_limit",
    "https://www.debian.org/",
]

DOWNLOAD_SIZES = [8, 16, 24, 32]  # MiB, bounded and infrequent
STATE_DIR = Path("/var/lib/oci-activity")


def total_mem_bytes() -> int:
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 1024**3


def is_daytime() -> bool:
    hour = time.localtime().tm_hour
    return DAY_START <= hour < DAY_END


def cpu_target(worker_id: int, now: float) -> float:
    # Slow independent waves avoid identical behavior between cores and hosts.
    period = 1200 + ((SEED + worker_id * 137) % 2400)
    phase = ((SEED >> 7) + worker_id * 193) % period
    wave = (math.sin((now + phase) * 2 * math.pi / period) + 1) / 2

    if is_daytime():
        low, high = CPU_IDLE_MIN, CPU_ACTIVE_MAX
    else:
        low, high = CPU_IDLE_MIN * 0.55, CPU_ACTIVE_MIN * 0.70

    target = low + (high - low) * (wave**2)
    return max(0.01, min(target, CPU_BURST_MAX))


def cpu_worker(worker_id: int) -> None:
    rng = random.Random(SEED + worker_id * 1009 + os.getpid())
    payload = os.urandom(rng.randint(96, 320) * 1024)
    window = 0.5
    target = CPU_IDLE_MIN
    next_update = 0.0
    burst_until = 0.0

    while True:
        now = time.time()
        if now >= next_update:
            target = cpu_target(worker_id, now)
            # Realistic short compute job, only occasionally.
            if rng.random() < 0.025:
                target = rng.uniform(CPU_ACTIVE_MIN, CPU_BURST_MAX)
                burst_until = now + rng.uniform(8, 35)
            elif burst_until and now >= burst_until:
                burst_until = 0.0
            next_update = now + rng.uniform(25, 110)

        busy = window * target
        end = time.perf_counter() + busy
        while time.perf_counter() < end:
            compressed = gzip.compress(payload, compresslevel=rng.choice((1, 3, 6)))
            hashlib.sha256(compressed).digest()

        time.sleep(max(0.0, window - busy))


def memory_worker() -> None:
    rng = random.Random(SEED + 500_003 + os.getpid())
    total = total_mem_bytes()
    blocks: list[bytearray] = []
    allocated = 0
    current_pct = rng.uniform(MEM_MIN_PCT, MEM_MAX_PCT)

    while True:
        # Slow drift resembles application cache growth and eviction.
        current_pct += rng.uniform(-0.008, 0.008)
        current_pct = max(MEM_MIN_PCT, min(current_pct, MEM_MAX_PCT))
        if not is_daytime():
            effective_pct = current_pct * rng.uniform(0.72, 0.92)
        else:
            effective_pct = current_pct

        target = int(total * effective_pct)
        step = rng.randint(max(8, MEM_STEP_MB // 2), MEM_STEP_MB) * 1024 * 1024

        try:
            if allocated + step < target:
                block = bytearray(step)
                for i in range(0, len(block), 4096):
                    block[i] = (i // 4096) % 251
                blocks.append(block)
                allocated += len(block)
            elif allocated - step > target and blocks:
                block = blocks.pop(0)
                allocated -= len(block)
            elif blocks:
                block = rng.choice(blocks)
                block[rng.randrange(0, len(block), 4096)] ^= 1
        except MemoryError:
            if blocks:
                allocated -= len(blocks.pop(0))

        time.sleep(rng.uniform(25, 75))


def request_small(url: str, limit: int = 64 * 1024) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "OCI-Activity/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            response.read(limit)
    except Exception:
        return


def bounded_download(size_mib: int) -> None:
    size = size_mib * 1024 * 1024
    url = f"https://speed.cloudflare.com/__down?bytes={size}"
    req = urllib.request.Request(url, headers={"User-Agent": "OCI-Activity/2.0"})
    remaining = size
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            while remaining > 0:
                chunk = response.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                time.sleep(0.03)
    except Exception:
        return


def network_worker() -> None:
    rng = random.Random(SEED + 700_001 + os.getpid())
    next_download = time.time() + rng.uniform(4 * 3600, 9 * 3600)

    while True:
        request_small(rng.choice(HEALTH_URLS), rng.randint(8, 64) * 1024)
        if time.time() >= next_download:
            bounded_download(rng.choice(DOWNLOAD_SIZES))
            next_download = time.time() + rng.uniform(5 * 3600, 12 * 3600)

        delay = rng.uniform(4 * 60, 15 * 60)
        if not is_daytime():
            delay *= rng.uniform(1.3, 2.0)
        time.sleep(delay)


def disk_worker() -> None:
    rng = random.Random(SEED + 900_001 + os.getpid())
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    while True:
        time.sleep(rng.uniform(45 * 60, 150 * 60))
        path: Path | None = None
        try:
            size = rng.randint(4, 24) * 1024 * 1024
            with tempfile.NamedTemporaryFile(
                dir=STATE_DIR, prefix="verify-", suffix=".bin", delete=False
            ) as f:
                path = Path(f.name)
                digest = hashlib.sha256()
                remaining = size
                while remaining:
                    chunk = os.urandom(min(1024 * 1024, remaining))
                    f.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
                f.flush()
                os.fsync(f.fileno())

            read_digest = hashlib.sha256()
            with path.open("rb") as f:
                while chunk := f.read(1024 * 1024):
                    read_digest.update(chunk)

            status = {
                "time": int(time.time()),
                "size": size,
                "ok": digest.hexdigest() == read_digest.hexdigest(),
            }
            (STATE_DIR / "last-verify.json").write_text(
                json.dumps(status), encoding="utf-8"
            )
        except Exception:
            pass
        finally:
            if path:
                path.unlink(missing_ok=True)


def start_process(target, *args) -> multiprocessing.Process:
    process = multiprocessing.Process(target=target, args=args, daemon=False)
    process.start()
    return process


def main() -> None:
    # Stable per-host startup offset plus small runtime jitter.
    time.sleep((SEED % 90) + random.uniform(0, 30))
    cores = os.cpu_count() or 1

    # Use fewer CPU workers on larger machines; each worker remains independently bounded.
    cpu_workers = max(1, min(cores, 4))
    specs: list[tuple] = [(cpu_worker, (i,)) for i in range(cpu_workers)]
    specs.extend([(memory_worker, ()), (network_worker, ()), (disk_worker, ())])

    processes = [start_process(fn, *args) for fn, args in specs]

    while True:
        time.sleep(20)
        for i, process in enumerate(processes):
            if not process.is_alive():
                fn, args = specs[i]
                processes[i] = start_process(fn, *args)


if __name__ == "__main__":
    main()
