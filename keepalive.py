#!/usr/bin/env python3
"""Adaptive resource workload controller for owned Linux servers.

Each host derives a stable, continuous "personality" from its machine seed, so a
fleet of servers never behaves like copies of one script. The personality drives
the shape of CPU, memory and network activity: a four-state CPU rhythm fed by a
mixed task pool, a cache/eviction memory model, and a mixed HTTP request mix.

The service observes whole-machine usage and only adds what is missing from a
per-host target. It uses ordinary compression, hashing, JSON, CSV, regex and
SQLite work. It does not hide, rename or disguise itself.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import logging
import logging.handlers
import lzma
import multiprocessing as mp
import os
import random
import re
import signal
import socket
import sqlite3
import statistics
import threading
import time
import urllib.request
import zlib
from collections import OrderedDict, deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

STATE_DIR = Path('/var/lib/keepalive')
CACHE_DIR = STATE_DIR / 'cache'
DB_PATH = STATE_DIR / 'metrics.db'
STATE_PATH = STATE_DIR / 'state.json'
STATUS_PATH = STATE_DIR / 'status.json'
PROFILE_PATH = STATE_DIR / 'profile.json'
CONFIG_PATH = Path('/etc/keepalive/config.json')
LOG_DIR = Path('/var/log/keepalive')
LOG_PATH = LOG_DIR / 'keepalive.log'
GIB = 1024 ** 3
MIB = 1024 ** 2
STOP = threading.Event()

log = logging.getLogger('keepalive')

DEFAULT_CONFIG = {
    'cycle_days': 7,
    # Legacy fields kept as fallbacks for hosts already deployed with them.
    'cpu_start_pct': 20.0,
    'cpu_target_min_pct': 22.0,
    'cpu_target_max_pct': 27.0,
    'cpu_pause_pct': 85.0,
    'memory_start_pct': 20.0,
    'memory_target_min_pct': 22.0,
    'memory_target_max_pct': 26.0,
    'memory_release_pct': 28.0,
    'min_available_memory_pct': 10.0,
    'normal_network_min_gb': 0.3,
    'normal_network_max_gb': 2.0,
    'high_network_min_gb': 5.0,
    'high_network_max_gb': 13.0,
    'network_chunk_mib': 32,
    'network_timeout_sec': 45,
    'sample_interval_sec': 5,
    'status_interval_sec': 30,
    'write_downloads_to_disk': False,
    'disk_free_stop_pct': 15.0,
    'download_url': 'https://speed.cloudflare.com/__down?bytes={bytes}',
    'log_backup_days': 7,
    'log_gzip': True,
    # Endpoint pools for the mixed request scheduler. Override freely.
    # 'download' is the bulk filler used only to top up a daily gap; day-to-day
    # texture comes from the api/json/head pulls below (like a real service).
    'network_endpoints': {
        'download': ['https://speed.cloudflare.com/__down?bytes={bytes}'],
        'head': [
            'https://www.cloudflare.com/',
            'https://api.github.com/',
            'https://www.debian.org/',
            'https://1.1.1.1/',
        ],
        'json': [
            'https://www.cloudflare.com/cdn-cgi/trace',
            'https://api.github.com/meta',
            'https://api.ipify.org/?format=json',
            'https://api.github.com/repos/torvalds/linux',
            'https://worldtimeapi.org/api/timezone/Etc/UTC',
        ],
        'range': [
            'https://speed.cloudflare.com/__down?bytes=8388608',
            'https://github.com/git/git/archive/refs/heads/master.zip',
        ],
    },
    # Share of the daily budget delivered as steady small pulls rather than one
    # bulk download. The rest is topped up by the (irregular, chunked) filler.
    'network_texture_fraction': 0.35,
}


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    try:
        user = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
        # Merge network_endpoints one level deep so partial overrides work.
        if isinstance(user.get('network_endpoints'), dict):
            merged = dict(cfg['network_endpoints'])
            merged.update(user['network_endpoints'])
            user = dict(user)
            user['network_endpoints'] = merged
        cfg.update(user)
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


# --------------------------------------------------------------------------- #
# Personality                                                                 #
# --------------------------------------------------------------------------- #

def _trait(seed: int, name: str) -> float:
    """A decorrelated 0..1 value for a named trait, stable per machine."""
    h = hashlib.sha256(f'{seed}:{name}'.encode()).hexdigest()
    return int(h[:16], 16) / float(1 << 64)


def _band_label(v: float, low: str, mid: str, high: str) -> str:
    return low if v < 0.34 else (mid if v < 0.67 else high)


def derive_profile(seed: int, cfg: dict[str, Any]) -> dict[str, Any]:
    """Build a continuous, multi-dimensional personality from the machine seed.

    The traits are the source of truth; the *_personality labels are only for
    human-readable status output.
    """
    compute = _trait(seed, 'compute')          # light data juggling .. heavy compute/db
    burst = _trait(seed, 'burst')              # steady .. spiky
    cache_stick = _trait(seed, 'cache')        # lean .. cache-heavy
    net_activity = _trait(seed, 'network')     # light .. heavy
    diurnal = round(0.15 + _trait(seed, 'diurnal') * 0.30, 3)  # 0.15 .. 0.45 (visible day/night)
    weekend_dip = round(0.10 + _trait(seed, 'weekend') * 0.25, 3)  # 0.10 .. 0.35 quieter weekends
    jitter_amp = round(1.5 + _trait(seed, 'jitter') * 2.5, 2)  # +/- 1.5 .. 4.0 pct wobble
    spike_prob = round(0.010 + _trait(seed, 'spike') * 0.025, 4)  # short bursts per tick
    task_interval = round(0.6 + _trait(seed, 'interval') * 1.4, 2)  # 0.6 .. 2.0
    startup_delay = int(10 + _trait(seed, 'startup') * 110)         # 10 .. 120s

    # Timezone personality: the machine's "working day" falls in a real region's
    # daytime, so a fleet doesn't all peak at the same UTC hour.
    regions = [
        ('US-West', -8.0), ('US-East', -5.0), ('Brazil', -3.0),
        ('UK', 0.0), ('CET', 1.0), ('Moscow', 3.0),
        ('India', 5.5), ('Asia-SG', 8.0), ('Asia-JP', 9.0), ('Sydney', 11.0),
    ]
    region, utc_offset = regions[int(_trait(seed, 'tz') * len(regions)) % len(regions)]
    peak_local = 13.0 + _trait(seed, 'peakhour') * 4.0 - 2.0   # busy peak 11:00-15:00 local
    peak_hour_utc = (peak_local - utc_offset) % 24.0

    cpu_center = round(23.0 + compute * 7.0, 2)      # 23 .. 30 (floor keeps p95 clear of 20%)
    mem_center = round(21.0 + cache_stick * 9.0, 2)  # 21 .. 30

    if burst > 0.7:
        cpu_label = 'bursty'
    else:
        cpu_label = _band_label(compute, 'steady', 'balanced', 'compute')

    return {
        'cpu_personality': cpu_label,
        'memory_personality': _band_label(cache_stick, 'lean', 'normal', 'cache-heavy'),
        'network_personality': _band_label(net_activity, 'light', 'active', 'heavy'),
        'timezone': region,
        'task_interval': task_interval,
        'startup_delay': startup_delay,
        # Continuous traits driving behaviour:
        'traits': {
            'compute_bias': round(compute, 3),
            'burstiness': round(burst, 3),
            'cache_stickiness': round(cache_stick, 3),
            'network_activity': round(net_activity, 3),
            'diurnal_strength': diurnal,
            'weekend_dip': weekend_dip,
            'jitter_amp_pct': jitter_amp,
            'spike_prob': spike_prob,
            'utc_offset': utc_offset,
            'peak_hour_utc': round(peak_hour_utc, 2),
        },
        'cpu_target_center_pct': cpu_center,
        'memory_target_center_pct': mem_center,
    }


def load_or_create_profile(seed: int, cfg: dict[str, Any]) -> dict[str, Any]:
    try:
        existing = json.loads(PROFILE_PATH.read_text(encoding='utf-8'))
        # Keep a hand-edited profile, but refresh a pre-upgrade schema (missing
        # the newer traits) deterministically from the same seed.
        if isinstance(existing, dict) and 'traits' in existing \
                and 'peak_hour_utc' in existing.get('traits', {}):
            return existing
    except Exception:
        pass
    profile = derive_profile(seed, cfg)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_json(PROFILE_PATH, profile)
    return profile


def activity_curve(profile: dict[str, Any], now: float) -> float:
    """Unfloored 0..1-ish activity level: deep day/night by the host's own
    timezone, damped further on that host's local weekend."""
    import math
    t = profile['traits']
    strength = float(t.get('diurnal_strength', 0.3))
    peak = float(t.get('peak_hour_utc', 13.0))
    offset = float(t.get('utc_offset', 0.0))
    utc = datetime.fromtimestamp(now, timezone.utc)
    hour = utc.hour + utc.minute / 60.0
    day = 0.5 + 0.5 * math.cos((hour - peak) / 24.0 * 2 * math.pi)  # 0 night .. 1 peak
    level = 1.0 - strength * (1.0 - day)
    # Weekend by the machine's local calendar.
    local = datetime.fromtimestamp(now + offset * 3600.0, timezone.utc)
    if local.weekday() >= 5:
        level *= (1.0 - float(t.get('weekend_dip', 0.2)))
    return max(0.0, min(1.0, level))


def cpu_activity_factor(profile: dict[str, Any], now: float) -> float:
    """CPU keeps a floor so the 7-day p95 stays clear of the reclaim line, even
    though nights/weekends visibly dip."""
    return max(0.80, activity_curve(profile, now))


def memory_activity_factor(profile: dict[str, Any], now: float) -> float:
    """Cache frees a little at night but not dramatically."""
    return max(0.85, 0.9 + 0.1 * activity_curve(profile, now))


# --------------------------------------------------------------------------- #
# Metric readers                                                              #
# --------------------------------------------------------------------------- #

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


def net_interfaces() -> list[str]:
    names = []
    for line in Path('/proc/net/dev').read_text(encoding='utf-8').splitlines()[2:]:
        name = line.split(':', 1)[0].strip()
        if name and name != 'lo':
            names.append(name)
    return names


def disk_free_pct(path: Path) -> float:
    st = os.statvfs(path)
    return 100.0 * st.f_bavail / max(1, st.f_blocks)


# --------------------------------------------------------------------------- #
# Health check                                                                #
# --------------------------------------------------------------------------- #

def health_check() -> dict[str, Any]:
    cores = max(1, os.cpu_count() or 1)
    total_mem, available_mem, swap_total, _ = memory_info()
    disk = disk_free_pct(STATE_DIR)
    ifaces = net_interfaces()
    worker_count = min(cores, 4)
    # Small hosts: fewer, gentler workers and a lower memory ceiling.
    mem_ceiling = 30.0
    if total_mem < 2 * GIB:
        worker_count = min(worker_count, 2)
        mem_ceiling = 18.0
    elif total_mem < 4 * GIB:
        mem_ceiling = 24.0
    if cores == 1:
        worker_count = 1
    return {
        'cores': cores,
        'total_memory_gb': round(total_mem / GIB, 2),
        'swap_gb': round(swap_total / GIB, 2),
        'disk_free_pct': round(disk, 1),
        'interfaces': ifaces,
        'network_ok': bool(ifaces),
        'worker_count': worker_count,
        'memory_ceiling_pct': mem_ceiling,
    }


# --------------------------------------------------------------------------- #
# CPU task pool (runs inside worker processes)                                #
# --------------------------------------------------------------------------- #

def _t_gzip(rng: random.Random, blob: bytes, conn: sqlite3.Connection) -> None:
    gzip.compress(blob, compresslevel=rng.choice((1, 4, 7)))


def _t_lzma(rng: random.Random, blob: bytes, conn: sqlite3.Connection) -> None:
    lzma.compress(blob[: 96 * 1024], preset=rng.choice((0, 1, 2)))


def _t_zlib(rng: random.Random, blob: bytes, conn: sqlite3.Connection) -> None:
    data = zlib.compress(blob, rng.choice((3, 6, 9)))
    zlib.decompress(data)


def _t_json(rng: random.Random, blob: bytes, conn: sqlite3.Connection) -> None:
    obj = {
        'id': rng.randrange(1 << 22),
        'ts': time.time(),
        'vals': [round(rng.random(), 6) for _ in range(64)],
        'tags': [f'k{rng.randrange(50)}' for _ in range(8)],
    }
    json.loads(json.dumps(obj))


def _t_csv(rng: random.Random, blob: bytes, conn: sqlite3.Connection) -> None:
    buf = io.StringIO()
    writer = csv.writer(buf)
    for _ in range(rng.randint(150, 300)):
        writer.writerow([rng.randrange(100000), round(rng.random(), 4), f'row{rng.randrange(999)}'])
    buf.seek(0)
    for _ in csv.reader(buf):
        pass


def _t_string(rng: random.Random, blob: bytes, conn: sqlite3.Connection) -> None:
    s = ''.join(rng.choice('abcdefghij klmnop ') for _ in range(2500))
    s.upper().lower().split()
    s.replace('a', 'x').count('x')


def _t_regex(rng: random.Random, blob: bytes, conn: sqlite3.Connection) -> None:
    text = ' '.join(f'k{rng.randrange(9999)}={rng.random():.3f}' for _ in range(120))
    re.findall(r'k(\d+)=([\d.]+)', text)


def _t_hash(rng: random.Random, blob: bytes, conn: sqlite3.Connection) -> None:
    hashlib.sha256(blob).hexdigest()


def _t_db_insert(rng: random.Random, blob: bytes, conn: sqlite3.Connection) -> None:
    rows = [(f'k{rng.randrange(20000)}', rng.random(), int(time.time())) for _ in range(rng.randint(80, 200))]
    conn.executemany('INSERT INTO events(k, v, ts) VALUES(?, ?, ?)', rows)
    conn.commit()
    # Keep the scratch table bounded.
    conn.execute('DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY id LIMIT '
                 '(SELECT MAX(0, COUNT(*) - 20000) FROM events))')
    conn.commit()


def _t_db_select(rng: random.Random, blob: bytes, conn: sqlite3.Connection) -> None:
    conn.execute('SELECT k, COUNT(*), AVG(v) FROM events WHERE v > ? GROUP BY k LIMIT 50',
                 (rng.random(),)).fetchall()


def _t_db_index(rng: random.Random, blob: bytes, conn: sqlite3.Connection) -> None:
    conn.execute('CREATE INDEX IF NOT EXISTS ix_events_k ON events(k)')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_events_v ON events(v)')


def _t_db_vacuum(rng: random.Random, blob: bytes, conn: sqlite3.Connection) -> None:
    conn.execute('DELETE FROM events WHERE ts % 9 = 0')
    conn.commit()
    conn.execute('VACUUM')


TASK_POOL: dict[str, list[Callable[[random.Random, bytes, sqlite3.Connection], None]]] = {
    'compress': [_t_gzip, _t_lzma, _t_zlib, _t_hash],
    'data': [_t_json, _t_csv, _t_string, _t_regex],
    'db': [_t_db_insert, _t_db_select, _t_db_index, _t_db_vacuum],
}


def make_scratch_db() -> sqlite3.Connection:
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT, k TEXT, v REAL, ts INTEGER)')
    conn.commit()
    return conn


def pick_category(rng: random.Random, compute_bias: float) -> str:
    w_compress = 0.20 + compute_bias * 0.50
    w_db = 0.10 + compute_bias * 0.40
    w_data = 0.90 - compute_bias * 0.45
    total = w_compress + w_db + w_data
    r = rng.random() * total
    if r < w_compress:
        return 'compress'
    if r < w_compress + w_db:
        return 'db'
    return 'data'


def cpu_worker(duty, stop, seed: int, worker_id: int, compute_bias: float, task_interval: float) -> None:
    rng = random.Random(seed + worker_id * 1009)
    payload = os.urandom(rng.randint(128, 512) * 1024)
    conn = make_scratch_db()
    window = 0.5
    try:
        while not stop.is_set():
            target = max(0.0, min(0.95, duty.value))
            busy_until = time.perf_counter() + window * target
            while time.perf_counter() < busy_until and not stop.is_set():
                category = pick_category(rng, compute_bias)
                task = rng.choice(TASK_POOL[category])
                try:
                    task(rng, payload, conn)
                except Exception:
                    pass
                # Micro-pacing gives each host a slightly different task cadence.
                if task_interval > 1.0 and rng.random() < 0.15:
                    time.sleep(min(0.05, (task_interval - 1.0) * 0.03))
            stop.wait(max(0.0, window - window * target))
    finally:
        try:
            conn.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# CPU state machine                                                           #
# --------------------------------------------------------------------------- #

class CPUStateMachine:
    """idle -> normal -> busy -> cooldown, weighted and irregular per host.

    'normal' dominates so the 7-day p95 stays comfortably above the reclaim
    threshold; other states add texture around it.
    """

    TRANSITIONS = {
        'normal':   [('busy', 0.34), ('cooldown', 0.24), ('idle', 0.07), ('normal', 0.35)],
        'busy':     [('cooldown', 0.58), ('normal', 0.37), ('busy', 0.05)],
        'cooldown': [('normal', 0.70), ('idle', 0.15), ('cooldown', 0.15)],
        'idle':     [('normal', 0.76), ('cooldown', 0.24)],
    }
    BASE_DWELL = {'idle': 120.0, 'cooldown': 300.0, 'normal': 900.0, 'busy': 180.0}

    def __init__(self, rng: random.Random, base_target: float, burstiness: float,
                 jitter_amp: float = 2.0, spike_prob: float = 0.015):
        self.rng = rng
        self.base = base_target
        self.burst = burstiness
        self.jitter_amp = jitter_amp
        self.spike_prob = spike_prob
        self.spike_ticks = 0
        self.spike_mag = 0.0
        self.state = 'normal'
        self.until = 0.0
        self.mult = {
            'idle': 0.30,
            'cooldown': 0.55,
            'normal': 1.0,
            'busy': 1.30 + 0.45 * burstiness,
        }

    def _dwell(self, state: str) -> float:
        base = self.BASE_DWELL[state] * (1.0 + self.rng.uniform(-0.4, 0.6))
        if state == 'normal':
            base *= (1.3 - 0.6 * self.burst)   # bursty hosts hold 'normal' less
        elif state == 'busy':
            base *= (0.7 + 0.9 * self.burst)   # and spike harder/longer
        return max(30.0, base)

    def _advance(self) -> None:
        choices = self.TRANSITIONS[self.state]
        r = self.rng.random() * sum(w for _, w in choices)
        acc = 0.0
        for name, w in choices:
            acc += w
            if r <= acc:
                self.state = name
                return
        self.state = choices[-1][0]

    def target(self, now: float, diurnal: float) -> tuple[float, bool]:
        changed = False
        if now >= self.until:
            self._advance()
            self.until = now + self._dwell(self.state)
            changed = True
        base = self.base * self.mult[self.state] * diurnal
        # Per-tick wobble so the curve is grainy, not a smooth line.
        base += self.rng.uniform(-self.jitter_amp, self.jitter_amp)
        # Occasional short microburst lasting a few ticks.
        if self.spike_ticks > 0:
            self.spike_ticks -= 1
            base += self.spike_mag
        elif self.rng.random() < self.spike_prob:
            self.spike_ticks = self.rng.randint(2, 6)
            self.spike_mag = self.rng.uniform(6.0, 16.0)
            base += self.spike_mag
        return max(0.0, base), changed


# --------------------------------------------------------------------------- #
# Memory: cache / eviction model                                              #
# --------------------------------------------------------------------------- #

class CacheModel:
    """A resident 'cache' with hits, misses and evictions that drifts within a
    band and periodically flushes, imitating a long-running service."""

    def __init__(self, rng: random.Random, stickiness: float):
        self.rng = rng
        self.stickiness = stickiness
        self.entries: OrderedDict[str, bytearray] = OrderedDict()
        self.total = 0
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.entry_min = 2 * MIB
        self.entry_max = 16 * MIB

    def footprint(self) -> int:
        return self.total

    def _touch(self, ba: bytearray) -> None:
        for i in range(0, len(ba), 65536):
            ba[i] = (ba[i] + 1) & 0xFF

    def tick(self, target: int, may_grow: bool) -> None:
        # Access an existing entry (a cache hit) with probability biased by
        # stickiness — keeps pages resident without necessarily growing.
        if self.entries and self.rng.random() < (0.40 + 0.40 * self.stickiness):
            key = next(iter(self.entries))  # oldest
            # promote a random-ish recent-ish key to most-recent
            keys = list(self.entries.keys())
            key = keys[self.rng.randrange(len(keys))]
            ba = self.entries.pop(key)
            self.entries[key] = ba
            self._touch(ba)
            self.hits += 1
            return
        self.misses += 1
        if may_grow and self.total < target:
            size = self.rng.randint(self.entry_min, self.entry_max)
            size = min(size, max(0, target - self.total))
            if size >= MIB:
                try:
                    ba = bytearray(size)
                    for i in range(0, size, 4096):
                        ba[i] = (i // 4096) & 0xFF
                    self.entries[f'obj{self.rng.randrange(1 << 30)}'] = ba
                    self.total += size
                except MemoryError:
                    pass

    def evict(self, count: int = 1) -> None:
        for _ in range(count):
            if not self.entries:
                break
            _, ba = self.entries.popitem(last=False)
            self.total -= len(ba)
            self.evictions += 1

    def flush_fraction(self, frac: float) -> None:
        if self.entries:
            self.evict(max(1, int(len(self.entries) * frac)))

    def shrink_to(self, target: int) -> None:
        while self.total > target and self.entries:
            self.evict(1)

    def clear(self) -> None:
        self.entries.clear()
        self.total = 0


# --------------------------------------------------------------------------- #
# Network: mixed request scheduler                                            #
# --------------------------------------------------------------------------- #

def _request(url: str, timeout: int, method: str = 'GET', headers: dict | None = None,
             read_limit: int | None = None) -> int:
    hdrs = {'User-Agent': 'keepalive-load-controller/4.0'}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs, method=method)
    got = 0
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for _, v in resp.getheaders():
            got += len(v)
        if method != 'HEAD':
            while not STOP.is_set():
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                got += len(chunk)
                if read_limit is not None and got >= read_limit:
                    break
    return got


def net_download(cfg: dict[str, Any], amount: int, state: dict[str, Any], lock: threading.Lock) -> int:
    chunk_cap = int(cfg['network_chunk_mib']) * MIB
    amount = min(amount, chunk_cap)
    pool = cfg['network_endpoints'].get('download') or [cfg['download_url']]
    template = random.choice(pool)
    url = template.format(bytes=amount) if '{bytes}' in template else template
    req = urllib.request.Request(url, headers={'User-Agent': 'keepalive-load-controller/4.0'})
    written = 0
    handle = None
    path: Path | None = None
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
        _account(state, lock, written)
        return written
    except Exception as exc:
        log.warning('download task failed: %s', exc)
        return 0
    finally:
        if handle and not handle.closed:
            handle.close()
        if path and path.suffix == '.part' and written == 0:
            path.unlink(missing_ok=True)


def net_light(cfg: dict[str, Any], state: dict[str, Any], lock: threading.Lock, kind: str) -> int:
    endpoints = cfg['network_endpoints']
    timeout = int(cfg['network_timeout_sec'])
    got = 0
    try:
        if kind == 'head' and endpoints.get('head'):
            got = _request(random.choice(endpoints['head']), timeout, method='HEAD')
        elif kind == 'json' and endpoints.get('json'):
            got = _request(random.choice(endpoints['json']), timeout, read_limit=256 * 1024)
        elif kind == 'range' and endpoints.get('range'):
            length = random.randint(256 * 1024, 4 * MIB)
            got = _request(random.choice(endpoints['range']), timeout,
                           headers={'Range': f'bytes=0-{length - 1}'}, read_limit=length + 4096)
        elif kind == 'archive' and endpoints.get('download'):
            template = random.choice(endpoints['download'])
            size = random.randint(512 * 1024, 3 * MIB)
            url = template.format(bytes=size) if '{bytes}' in template else template
            raw_bytes = 0
            hdrs = {'User-Agent': 'keepalive-load-controller/4.0'}
            with urllib.request.urlopen(urllib.request.Request(url, headers=hdrs), timeout=timeout) as resp:
                data = resp.read(size)
                raw_bytes = len(data)
            # verify: compress + checksum, imitating an archive read pipeline
            hashlib.sha256(data).hexdigest()
            gzip.compress(data[: 512 * 1024])
            got = raw_bytes
        else:
            return 0
        _account(state, lock, got)
        log.info('network task %s ok (%d B)', kind, got)
        return got
    except Exception as exc:
        log.warning('network task %s failed: %s', kind, exc)
        return 0


def _account(state: dict[str, Any], lock: threading.Lock, written: int) -> None:
    with lock:
        state['script_network_bytes'] = int(state.get('script_network_bytes', 0)) + written


def pick_network_kind(rng: random.Random, activity: float) -> str:
    weights = [
        ('head', 0.30 - activity * 0.10),
        ('json', 0.28 - activity * 0.08),
        ('range', 0.24 + activity * 0.06),
        ('archive', 0.18 + activity * 0.12),
    ]
    total = sum(max(0.01, w) for _, w in weights)
    r = rng.random() * total
    acc = 0.0
    for name, w in weights:
        acc += max(0.01, w)
        if r <= acc:
            return name
    return 'head'


# --------------------------------------------------------------------------- #
# Cycle bookkeeping                                                           #
# --------------------------------------------------------------------------- #

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


def build_cycle(cfg: dict[str, Any], profile: dict[str, Any], rng: random.Random,
                previous: dict[str, Any] | None = None) -> dict[str, Any]:
    today = date.today()
    if previous:
        start = date.fromisoformat(previous['cycle_start'])
        if today < start + timedelta(days=int(cfg['cycle_days'])):
            return previous
    activity = float(profile['traits']['network_activity'])
    # The configured min/max are hard bounds; network_activity only sets how far
    # up the band a host reaches (so caps are never exceeded).
    n_min = float(cfg['normal_network_min_gb'])
    n_max = float(cfg['normal_network_max_gb'])
    h_min = float(cfg['high_network_min_gb'])
    h_max = float(cfg['high_network_max_gb'])
    normal_lo = n_min
    normal_hi = n_min + (n_max - n_min) * (0.35 + activity * 0.65)
    high_lo = h_min
    high_hi = h_min + (h_max - h_min) * (0.35 + activity * 0.65)
    high_day = rng.randrange(int(cfg['cycle_days']))
    targets = []
    for i in range(int(cfg['cycle_days'])):
        if i == high_day:
            gb = rng.uniform(high_lo, high_hi)
        else:
            gb = rng.uniform(normal_lo, normal_hi)
        targets.append(round(gb, 3))
    return {
        'cycle_start': today.isoformat(),
        'high_day': high_day,
        'daily_network_targets_gb': targets,
        'cpu_target_pct': float(profile['cpu_target_center_pct']),
        'memory_target_pct': float(profile['memory_target_center_pct']),
        'day_key': today.isoformat(),
        'day_network_base': network_bytes(),
        'script_network_bytes': 0,
    }


def cycle_day(state: dict[str, Any], cfg: dict[str, Any]) -> int:
    start = date.fromisoformat(state['cycle_start'])
    return max(0, min(int(cfg['cycle_days']) - 1, (date.today() - start).days))


def cleanup_old_cache(cycle_start: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.combine(date.fromisoformat(cycle_start), datetime.min.time()).timestamp()
    for item in CACHE_DIR.iterdir():
        try:
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Logging                                                                     #
# --------------------------------------------------------------------------- #

def setup_logging(cfg: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    log.handlers.clear()
    handler = logging.handlers.TimedRotatingFileHandler(
        LOG_PATH, when='midnight', backupCount=int(cfg.get('log_backup_days', 7)),
        encoding='utf-8', utc=False)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s',
                                            datefmt='%Y-%m-%d %H:%M:%S'))
    if bool(cfg.get('log_gzip', True)):
        def rotator(source: str, dest: str) -> None:
            with open(source, 'rb') as sf, gzip.open(dest + '.gz', 'wb') as df:
                df.writelines(sf)
            os.remove(source)
        handler.rotator = rotator
        handler.namer = lambda name: name  # keep base name, rotator adds .gz
    log.addHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s',
                                           datefmt='%Y-%m-%d %H:%M:%S'))
    console.setLevel(logging.WARNING)
    log.addHandler(console)


# --------------------------------------------------------------------------- #
# Status output                                                               #
# --------------------------------------------------------------------------- #

def render_status_human(status: dict[str, Any], profile: dict[str, Any]) -> str:
    t = profile.get('traits', {})
    lines = [
        'Machine Profile:',
        f"  {profile.get('cpu_personality','?')} / {profile.get('memory_personality','?')} / "
        f"{profile.get('network_personality','?')}  [{profile.get('timezone','?')}]",
        f"  compute={t.get('compute_bias')} burst={t.get('burstiness')} "
        f"cache={t.get('cache_stickiness')} net={t.get('network_activity')}",
        f"  diurnal={t.get('diurnal_strength')} weekend_dip={t.get('weekend_dip')} "
        f"jitter={t.get('jitter_amp_pct')} peak_utc={t.get('peak_hour_utc')}h",
        f"  activity_now={status.get('activity_level','?')}",
        f"  task_interval={profile.get('task_interval')}  startup_delay={profile.get('startup_delay')}s",
        '',
        'CPU:',
        f"  state    {status.get('cpu_state','?')}",
        f"  current  {status.get('cpu_total_pct','?')}%",
        f"  target   {status.get('cpu_target_pct','?')}%",
        f"  workers  {status.get('cpu_workers','?')}",
        f"  p95      {status.get('cpu_p95_rolling_pct','?')}%",
        '',
        'Memory:',
        f"  used     {status.get('memory_total_pct','?')}%",
        f"  cache    {status.get('script_memory_gb','?')}GB",
        f"  hit/miss {status.get('cache_hits','?')}/{status.get('cache_misses','?')} "
        f"evict={status.get('cache_evictions','?')}",
        '',
        'Network:',
        f"  today    {status.get('today_network_used_gb','?')}GB",
        f"  target   {status.get('today_network_target_gb','?')}GB",
        f"  script   {status.get('script_network_gb','?')}GB",
        '',
        'Cycle:',
        f"  day      {status.get('cycle_day','?')}/{status.get('cycle_days','?')}",
        f"  high day {status.get('high_network_day','?')}",
        f"  updated  {status.get('updated_at','?')}",
    ]
    return '\n'.join(lines)


def print_status(as_json: bool) -> int:
    try:
        status = json.loads(STATUS_PATH.read_text(encoding='utf-8'))
    except FileNotFoundError:
        print('No status data. Is keepalive running?')
        return 1
    if as_json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0
    try:
        profile = json.loads(PROFILE_PATH.read_text(encoding='utf-8'))
    except Exception:
        profile = {}
    print(render_status_human(status, profile))
    return 0


# --------------------------------------------------------------------------- #
# Main loop                                                                   #
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--json', action='store_true', help='machine-readable --status')
    args = parser.parse_args()
    if args.status:
        return print_status(args.json)

    cfg = load_config()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    setup_logging(cfg)

    seed = machine_seed()
    rng = random.Random(seed)
    profile = load_or_create_profile(seed, cfg)
    health = health_check()

    log.info('starting keepalive; profile=%s/%s/%s cores=%d mem=%.1fGB workers=%d',
             profile['cpu_personality'], profile['memory_personality'],
             profile['network_personality'], health['cores'],
             health['total_memory_gb'], health['worker_count'])

    # Apply health-derived memory ceiling to the profile target.
    mem_target_pct = min(float(profile['memory_target_center_pct']), float(health['memory_ceiling_pct']))

    try:
        previous_state = json.loads(STATE_PATH.read_text(encoding='utf-8'))
    except Exception:
        previous_state = None
    state = build_cycle(cfg, profile, rng, previous_state)
    state['memory_target_pct'] = mem_target_pct
    cleanup_old_cache(state['cycle_start'])
    atomic_json(STATE_PATH, state)

    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())

    # Stagger fleet startup so hosts don't all ramp at once.
    delay = int(profile.get('startup_delay', 30))
    log.info('startup delay %ds', delay)
    if STOP.wait(delay):
        return 0

    traits = profile['traits']
    worker_count = int(health['worker_count'])
    cores = int(health['cores'])
    duties = [mp.Value('d', 0.0) for _ in range(worker_count)]
    mp_stop = mp.Event()
    workers = [mp.Process(target=cpu_worker,
                          args=(duties[i], mp_stop, seed, i,
                                float(traits['compute_bias']), float(profile['task_interval'])),
                          daemon=False)
               for i in range(worker_count)]
    for worker in workers:
        worker.start()

    cpu_state = CPUStateMachine(rng, float(state['cpu_target_pct']), float(traits['burstiness']),
                                jitter_amp=float(traits.get('jitter_amp_pct', 2.0)),
                                spike_prob=float(traits.get('spike_prob', 0.015)))
    cache = CacheModel(rng, float(traits['cache_stickiness']))
    state_lock = threading.Lock()
    conn = init_db()
    previous_cpu = read_cpu_times()
    cpu_samples: deque[float] = deque(maxlen=10080)
    mem_samples: deque[float] = deque(maxlen=10080)
    next_status = 0.0
    next_network = time.time() + rng.uniform(30, 120)
    next_flush = time.time() + rng.uniform(20 * 60, 60 * 60)
    last_swap_used = 0
    duty_total = 0.0
    current_target = float(state['cpu_target_pct'])

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
                    log.info('day rollover; network counters reset')
                if date.today() >= date.fromisoformat(state['cycle_start']) + timedelta(days=int(cfg['cycle_days'])):
                    state = build_cycle(cfg, profile, rng, None)
                    state['memory_target_pct'] = mem_target_pct
                    cleanup_old_cache(state['cycle_start'])
                    cpu_state.base = float(state['cpu_target_pct'])
                    log.info('cycle rolled over; new high day=%d', int(state['high_day']) + 1)
                day_index = cycle_day(state, cfg)
                day_target = float(state['daily_network_targets_gb'][day_index]) * GIB
                day_used = max(0, current_net - int(state.get('day_network_base', current_net)))

            # ---- CPU: state machine sets target, duty controller fills gap ----
            dfac = cpu_activity_factor(profile, now)
            new_target, changed = cpu_state.target(now, dfac)
            current_target = new_target
            if changed:
                log.info('cpu state -> %s (target %.1f%%)', cpu_state.state, current_target)
            if cpu >= float(cfg['cpu_pause_pct']):
                duty_total = 0.0
            elif cpu < float(cfg['cpu_start_pct']):
                duty_total = min(worker_count * 0.9, max(0.0, (current_target - cpu) / 100.0 * cores))
            else:
                duty_total = min(worker_count * 0.9, max(0.0, (current_target - cpu) / 100.0 * cores * 0.65))
            per_worker = duty_total / worker_count
            for duty in duties:
                duty.value = per_worker

            # ---- Memory: cache / eviction model (frees a little at night) ----
            mfac = memory_activity_factor(profile, now)
            target_mem = int(total_mem * float(state['memory_target_pct']) / 100.0 * mfac)
            pressure = available_pct < float(cfg['min_available_memory_pct']) or swap_used > last_swap_used + 16 * MIB
            if pressure:
                cache.shrink_to(int(target_mem * 0.75))
                log.info('memory pressure; cache shrunk to %.2fGB', cache.footprint() / GIB)
            elif cache.footprint() > target_mem * 1.05:
                cache.shrink_to(target_mem)
            else:
                may_grow = mem_pct < float(cfg['memory_release_pct'])
                cache.tick(target_mem, may_grow)
            if now >= next_flush and cache.entries:
                frac = 0.10 + (1.0 - float(traits['cache_stickiness'])) * 0.25
                before = cache.footprint()
                cache.flush_fraction(frac)
                log.info('cache flush %.0f%%: %.2fGB -> %.2fGB', frac * 100,
                         before / GIB, cache.footprint() / GIB)
                next_flush = now + rng.uniform(20 * 60, 70 * 60)
            script_mem = cache.footprint()
            last_swap_used = swap_used

            # ---- Network: small pulls are the norm; bulk only tops up the
            #      daily budget, biased toward the host's active hours ----
            if now >= next_network:
                a = activity_curve(profile, now)
                behind = day_target - day_used
                seconds_left = max(60, int(datetime.combine(date.today() + timedelta(days=1),
                                                            datetime.min.time()).timestamp() - now))
                day_pressure = 1.0 - seconds_left / 86400.0
                # Reserve a share of the budget for steady small pulls; the rest
                # is bulk. Only push bulk when the host is "awake", unless the
                # day is running out (then catch up regardless).
                texture_share = float(cfg.get('network_texture_fraction', 0.35))
                bulk_owed = behind - texture_share * day_target * (seconds_left / 86400.0)
                want_bulk = bulk_owed > 48 * MIB and (a > 0.45 or day_pressure > 0.75)
                if want_bulk:
                    pace = bulk_owed * min(1.0, 1400.0 / seconds_left)
                    chunk = int(pace * rng.uniform(0.3, 1.0) * (0.5 + 0.5 * a))
                    chunk = max(8 * MIB, min(int(behind), chunk))
                    threading.Thread(target=net_download,
                                     args=(cfg, chunk, state, state_lock), daemon=True).start()
                    log.info('network bulk %.1fMB (owed %.2fGB, activity %.2f)',
                             chunk / MIB, max(0.0, behind) / GIB, a)
                else:
                    kind = pick_network_kind(rng, float(traits['network_activity']))
                    threading.Thread(target=net_light,
                                     args=(cfg, state, state_lock, kind), daemon=True).start()
                # Irregular spacing: longer at night, shorter when busy.
                spread = 1.0 - float(traits['network_activity']) * 0.4
                gap = rng.uniform(3 * 60, 15 * 60) * (1.6 - a) * spread
                next_network = now + max(60.0, gap)

            cpu_samples.append(cpu)
            mem_samples.append(mem_pct)
            if int(now) % 60 < int(cfg['sample_interval_sec']):
                conn.execute('INSERT OR REPLACE INTO samples VALUES (?, ?, ?, ?, ?, ?, ?)',
                             (int(now), cpu, mem_pct, current_net,
                              int(state.get('script_network_bytes', 0)), script_mem, duty_total))
                conn.execute('DELETE FROM samples WHERE ts < ?', (int(now) - 8 * 86400,))
                conn.commit()

            if now >= next_status:
                status = {
                    'updated_at': datetime.now().isoformat(timespec='seconds'),
                    'cycle_start': state['cycle_start'],
                    'cycle_day': day_index + 1,
                    'cycle_days': int(cfg['cycle_days']),
                    'high_network_day': int(state['high_day']) + 1,
                    'timezone': profile.get('timezone', '?'),
                    'activity_level': round(activity_curve(profile, now), 2),
                    'cpu_state': cpu_state.state,
                    'cpu_total_pct': round(cpu, 2),
                    'cpu_target_pct': round(current_target, 2),
                    'cpu_workers': worker_count,
                    'cpu_script_duty_cores': round(duty_total, 3),
                    'cpu_p50_rolling_pct': round(statistics.median(cpu_samples), 2) if cpu_samples else 0,
                    'cpu_p95_rolling_pct': round(percentile(list(cpu_samples), 0.95), 2),
                    'memory_total_pct': round(mem_pct, 2),
                    'memory_target_pct': round(float(state['memory_target_pct']), 2),
                    'script_memory_gb': round(script_mem / GIB, 3),
                    'available_memory_pct': round(available_pct, 2),
                    'cache_hits': cache.hits,
                    'cache_misses': cache.misses,
                    'cache_evictions': cache.evictions,
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
        cache.clear()
        atomic_json(STATE_PATH, state)
        log.info('keepalive stopped')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
