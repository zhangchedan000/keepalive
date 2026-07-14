#!/usr/bin/env python3
"""Adaptive resource workload controller for owned Linux servers.

The service observes whole-machine CPU, memory and network usage, then adds only
what is missing from a configurable target. It uses ordinary compression,
hashing, JSON and SQLite work; it does not hide or rename itself.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import multiprocessing as mp
import os
import random
import signal
import socket
import sqlite3
import statistics
import threading
import time
import urllib.request
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

STATE_DIR = Path('/var/lib/keepalive')
CACHE_DIR = STATE_DIR / 'cache'
DB_PATH = STATE_DIR / 'metrics.db'
STATE_PATH = STATE_DIR / 'state.json'
STATUS_PATH = STATE_DIR / 'status.json'
CONFIG_PATH = Path('/etc/keepalive/config.json')
GIB = 1024 ** 3
MIB = 1024 ** 2
STOP = threading.Event()

DEFAULT_CONFIG = {
    'cycle_days': 7,
    'cpu_start_pct': 20.0,
    'cpu_target_min_pct': 22.0,
    'cpu_target_max_pct': 27.0,
    'cpu_pause_pct': 85.0,
    'memory_start_pct': 20.0,
    'memory_target_min_pct': 22.0,
    'memory_target_max_pct': 26.0,
    'memory_release_pct': 28.0,
    'min_available_memory_pct': 10.0,
    'normal_network_min_gb': 1.0,
    'normal_network_max_gb': 5.0,
    'high_network_min_gb': 10.0,
    'high_network_max_gb': 50.0,
    'network_chunk_mib': 32,
    'network_timeout_sec': 45,
    'sample_interval_sec': 5,
    'status_interval_sec': 30,
    'write_downloads_to_disk': False,
    'disk_free_stop_pct': 15.0,
    'download_url': 'https://speed.cloudflare.com/__down?bytes={bytes}',
}


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding='utf-8')))
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f'config warning: {exc}', flush=True)
    return cfg


def machine_seed() -> int:
    parts = [socket.gethostname()]
    for path in ('/etc/machine-id', '/var/lib/dbus/machine-id'):
        try:
            parts.append(Path(path).read_text(encoding='utf-8').strip())
            break
        except OSError:
            continue
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    salt_path = STATE_DIR / 'salt'
    if not salt_path.exists():
        salt_path.write_text(os.urandom(24).hex(), encoding='utf-8')
    parts.append(salt_path.read_text(encoding='utf-8').strip())
    return int(hashlib.sha256('|'.join(parts).encode()).hexdigest()[:16], 16)


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def read_cpu_times() -> tuple[int, int]:
    fields = Path('/proc/stat').read_text(encoding='utf-8').splitlines()[0].split()[1:]
    values = [int(x) for x in fields]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def cpu_percent(previous: tuple[int, int], current: tuple[int, int]) -> float:
    total = current[0] - previous[0]
    idle = current[1] - previous[1]
    return 0.0 if total <= 0 else max(0.0, min(100.0, 100.0 * (total - idle) / total))


def memory_info() -> tuple[int, int, int, int]:
    values: dict[str, int] = {}
    for line in Path('/proc/meminfo').read_text(encoding='utf-8').splitlines():
        key, raw = line.split(':', 1)
        values[key] = int(raw.strip().split()[0]) * 1024
    total = values.get('MemTotal', GIB)
    available = values.get('MemAvailable', values.get('MemFree', 0))
    swap_total = values.get('SwapTotal', 0)
    swap_free = values.get('SwapFree', 0)
    return total, available, swap_total, swap_total - swap_free


def network_bytes() -> int:
    total = 0
    for line in Path('/proc/net/dev').read_text(encoding='utf-8').splitlines()[2:]:
        name, payload = line.split(':', 1)
        if name.strip() == 'lo':
            continue
        parts = payload.split()
        total += int(parts[0]) + int(parts[8])
    return total


def disk_free_pct(path: Path) -> float:
    st = os.statvfs(path)
    return 100.0 * st.f_bavail / max(1, st.f_blocks)


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''CREATE TABLE IF NOT EXISTS samples (
        ts INTEGER PRIMARY KEY, cpu REAL, mem REAL, net_total INTEGER,
        script_net INTEGER, script_mem INTEGER, cpu_duty REAL)''')
    conn.commit()
    return conn


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * p))))
    return ordered[idx]


def build_cycle(cfg: dict[str, Any], rng: random.Random, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    today = date.today()
    if previous:
        start = date.fromisoformat(previous['cycle_start'])
        if today < start + timedelta(days=int(cfg['cycle_days'])):
            return previous
    high_day = rng.randrange(int(cfg['cycle_days']))
    targets = []
    for i in range(int(cfg['cycle_days'])):
        if i == high_day:
            gb = rng.uniform(float(cfg['high_network_min_gb']), float(cfg['high_network_max_gb']))
        else:
            gb = rng.uniform(float(cfg['normal_network_min_gb']), float(cfg['normal_network_max_gb']))
        targets.append(round(gb, 3))
    return {
        'cycle_start': today.isoformat(),
        'high_day': high_day,
        'daily_network_targets_gb': targets,
        'cpu_target_pct': round(rng.uniform(float(cfg['cpu_target_min_pct']), float(cfg['cpu_target_max_pct'])), 2),
        'memory_target_pct': round(rng.uniform(float(cfg['memory_target_min_pct']), float(cfg['memory_target_max_pct'])), 2),
        'day_key': today.isoformat(),
        'day_network_base': network_bytes(),
        'script_network_bytes': 0,
    }


def cycle_day(state: dict[str, Any], cfg: dict[str, Any]) -> int:
    start = date.fromisoformat(state['cycle_start'])
    return max(0, min(int(cfg['cycle_days']) - 1, (date.today() - start).days))


def cpu_worker(duty: mp.Value, stop: mp.Event, seed: int, worker_id: int) -> None:
    rng = random.Random(seed + worker_id * 1009)
    payload = os.urandom(rng.randint(128, 512) * 1024)
    window = 0.5
    while not stop.is_set():
        target = max(0.0, min(0.95, duty.value))
        busy_until = time.perf_counter() + window * target
        while time.perf_counter() < busy_until:
            level = rng.choice((1, 3, 6))
            compressed = gzip.compress(payload, compresslevel=level)
            digest = hashlib.sha256(compressed).hexdigest()
            json.loads(json.dumps({'d': digest, 'n': len(compressed), 't': time.time()}))
        stop.wait(max(0.0, window - window * target))


def network_fill(cfg: dict[str, Any], requested: int, state: dict[str, Any], lock: threading.Lock) -> int:
    if requested <= 0 or STOP.is_set():
        return 0
    chunk_cap = int(cfg['network_chunk_mib']) * MIB
    amount = min(requested, chunk_cap)
    url = str(cfg['download_url']).format(bytes=amount)
    req = urllib.request.Request(url, headers={'User-Agent': 'keepalive-load-controller/3.0'})
    written = 0
    path: Path | None = None
    handle = None
    try:
        if bool(cfg['write_downloads_to_disk']) and disk_free_pct(STATE_DIR) >= float(cfg['disk_free_stop_pct']):
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path = CACHE_DIR / f'{int(time.time())}-{random.randrange(1_000_000)}.part'
            handle = path.open('wb')
        with urllib.request.urlopen(req, timeout=int(cfg['network_timeout_sec'])) as response:
            while written < amount and not STOP.is_set():
                chunk = response.read(min(256 * 1024, amount - written))
                if not chunk:
                    break
                written += len(chunk)
                if handle:
                    handle.write(chunk)
                time.sleep(random.uniform(0.01, 0.06))
        if handle:
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
        with lock:
            state['script_network_bytes'] = int(state.get('script_network_bytes', 0)) + written
        return written
    except Exception as exc:
        print(f'network warning: {exc}', flush=True)
        return 0
    finally:
        if handle and not handle.closed:
            handle.close()
        if path and path.suffix == '.part' and written == 0:
            path.unlink(missing_ok=True)


def cleanup_old_cache(cycle_start: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.combine(date.fromisoformat(cycle_start), datetime.min.time()).timestamp()
    for item in CACHE_DIR.iterdir():
        try:
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink()
        except OSError:
            pass


def print_status() -> int:
    try:
        data = json.loads(STATUS_PATH.read_text(encoding='utf-8'))
    except FileNotFoundError:
        print('No status data. Is keepalive running?')
        return 1
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()
    if args.status:
        return print_status()

    cfg = load_config()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    seed = machine_seed()
    rng = random.Random(seed)
    try:
        previous_state = json.loads(STATE_PATH.read_text(encoding='utf-8'))
    except Exception:
        previous_state = None
    state = build_cycle(cfg, rng, previous_state)
    cleanup_old_cache(state['cycle_start'])
    atomic_json(STATE_PATH, state)

    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())

    cores = max(1, os.cpu_count() or 1)
    worker_count = min(cores, 4)
    duties = [mp.Value('d', 0.0) for _ in range(worker_count)]
    mp_stop = mp.Event()
    workers = [mp.Process(target=cpu_worker, args=(duties[i], mp_stop, seed, i), daemon=False) for i in range(worker_count)]
    for worker in workers:
        worker.start()

    mem_blocks: deque[bytearray] = deque()
    script_mem = 0
    state_lock = threading.Lock()
    conn = init_db()
    previous_cpu = read_cpu_times()
    cpu_samples: deque[float] = deque(maxlen=10080)
    mem_samples: deque[float] = deque(maxlen=10080)
    next_status = 0.0
    next_network = time.time() + rng.uniform(30, 120)
    last_swap_used = 0

    try:
        while not STOP.wait(float(cfg['sample_interval_sec'])):
            now = time.time()
            current_cpu = read_cpu_times()
            cpu = cpu_percent(previous_cpu, current_cpu)
            previous_cpu = current_cpu
            total_mem, available_mem, _, swap_used = memory_info()
            mem_pct = 100.0 * (total_mem - available_mem) / max(1, total_mem)
            available_pct = 100.0 * available_mem / max(1, total_mem)
            current_net = network_bytes()

            today_key = date.today().isoformat()
            with state_lock:
                if state.get('day_key') != today_key:
                    state['day_key'] = today_key
                    state['day_network_base'] = current_net
                    state['script_network_bytes'] = 0
                if date.today() >= date.fromisoformat(state['cycle_start']) + timedelta(days=int(cfg['cycle_days'])):
                    state = build_cycle(cfg, rng, None)
                    cleanup_old_cache(state['cycle_start'])
                day_index = cycle_day(state, cfg)
                day_target = float(state['daily_network_targets_gb'][day_index]) * GIB
                day_used = max(0, current_net - int(state.get('day_network_base', current_net)))

            cpu_target = float(state['cpu_target_pct'])
            if cpu >= float(cfg['cpu_pause_pct']):
                duty_total = 0.0
            elif cpu < float(cfg['cpu_start_pct']):
                duty_total = min(worker_count * 0.9, max(0.0, (cpu_target - cpu) / 100.0 * cores))
            else:
                duty_total = min(worker_count * 0.9, max(0.0, (cpu_target - cpu) / 100.0 * cores * 0.65))
            per_worker = duty_total / worker_count
            for duty in duties:
                duty.value = per_worker

            target_mem = int(total_mem * float(state['memory_target_pct']) / 100.0)
            used_mem = total_mem - available_mem
            pressure = available_pct < float(cfg['min_available_memory_pct']) or swap_used > last_swap_used + 16 * MIB
            if pressure or mem_pct >= float(cfg['memory_release_pct']):
                release = max(1, len(mem_blocks) // 4) if mem_blocks else 0
                for _ in range(release):
                    script_mem -= len(mem_blocks.popleft())
            elif mem_pct < float(cfg['memory_start_pct']) and used_mem < target_mem:
                amount = min(64 * MIB, target_mem - used_mem)
                if amount >= 4 * MIB:
                    try:
                        block = bytearray(amount)
                        for i in range(0, len(block), 4096):
                            block[i] = (i // 4096) % 251
                        mem_blocks.append(block)
                        script_mem += len(block)
                    except MemoryError:
                        pass
            elif used_mem > target_mem and mem_blocks:
                script_mem -= len(mem_blocks.popleft())
            last_swap_used = swap_used

            if now >= next_network:
                remaining = max(0, int(day_target - day_used))
                seconds_left = max(60, int(datetime.combine(date.today() + timedelta(days=1), datetime.min.time()).timestamp() - now))
                pace = int(remaining * min(1.0, 900 / seconds_left))
                requested = min(remaining, max(0, pace))
                if requested > 0:
                    threading.Thread(target=network_fill, args=(cfg, requested, state, state_lock), daemon=True).start()
                next_network = now + rng.uniform(8 * 60, 22 * 60)

            cpu_samples.append(cpu)
            mem_samples.append(mem_pct)
            if int(now) % 60 < int(cfg['sample_interval_sec']):
                conn.execute('INSERT OR REPLACE INTO samples VALUES (?, ?, ?, ?, ?, ?, ?)',
                             (int(now), cpu, mem_pct, current_net, int(state.get('script_network_bytes', 0)), script_mem, duty_total))
                conn.execute('DELETE FROM samples WHERE ts < ?', (int(now) - 8 * 86400,))
                conn.commit()

            if now >= next_status:
                status = {
                    'updated_at': datetime.now().isoformat(timespec='seconds'),
                    'cycle_start': state['cycle_start'],
                    'cycle_day': day_index + 1,
                    'high_network_day': int(state['high_day']) + 1,
                    'cpu_total_pct': round(cpu, 2),
                    'cpu_target_pct': cpu_target,
                    'cpu_script_duty_cores': round(duty_total, 3),
                    'cpu_p50_rolling_pct': round(statistics.median(cpu_samples), 2) if cpu_samples else 0,
                    'cpu_p95_rolling_pct': round(percentile(list(cpu_samples), 0.95), 2),
                    'memory_total_pct': round(mem_pct, 2),
                    'memory_target_pct': float(state['memory_target_pct']),
                    'script_memory_gb': round(script_mem / GIB, 3),
                    'available_memory_pct': round(available_pct, 2),
                    'today_network_target_gb': round(day_target / GIB, 3),
                    'today_network_used_gb': round(day_used / GIB, 3),
                    'script_network_gb': round(int(state.get('script_network_bytes', 0)) / GIB, 3),
                    'download_to_disk': bool(cfg['write_downloads_to_disk']),
                }
                atomic_json(STATUS_PATH, status)
                atomic_json(STATE_PATH, state)
                next_status = now + float(cfg['status_interval_sec'])
    finally:
        STOP.set()
        mp_stop.set()
        for duty in duties:
            duty.value = 0.0
        for worker in workers:
            worker.join(timeout=5)
            if worker.is_alive():
                worker.terminate()
        conn.close()
        mem_blocks.clear()
        atomic_json(STATE_PATH, state)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
