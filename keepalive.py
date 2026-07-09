#!/usr/bin/env python3
"""
System Metrics Collector
可控综合负载：CPU 稳定在 30~40% 波动，内存常驻 40~50% 波动，
外加零星网络 / 磁盘 IO。进程名伪装为 python3-worker，曲线为自然波浪状。
"""
import os, time, random, gzip, hashlib, urllib.request, tempfile, multiprocessing

# ===== 可调参数（想改占用改这里）=====
CPU_MIN = 0.30          # CPU 占用下限（每核 30%）
CPU_MAX = 0.40          # CPU 占用上限（每核 40%）
MEM_MIN_PCT = 0.40      # 内存占用下限（总内存的 40%）
MEM_MAX_PCT = 0.50      # 内存占用上限（总内存的 50%）
# =====================================

URLS = [
    "https://www.cloudflare.com/cdn-cgi/trace",
    "https://api.github.com",
    "https://www.bing.com",
    "https://mirrors.ubuntu.com/mirrors.txt",
    "https://www.debian.org",
]


def disguise(name=b"python3-worker"):
    """把进程名伪装成 python3-worker"""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").prctl(15, name, 0, 0, 0)
    except Exception:
        pass


def total_mem_bytes():
    """读取系统总内存（字节）"""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 2 * 1024 ** 3  # 兜底 2G


def cpu_worker():
    """单核 CPU 占空比控制：目标占用在 CPU_MIN~CPU_MAX 之间缓慢漂移，形成波浪"""
    disguise()
    data = os.urandom(200_000)
    target = random.uniform(CPU_MIN, CPU_MAX)
    next_drift = time.time() + random.uniform(30, 90)  # 每 30~90 秒换一次目标
    window = 0.2  # 每 0.2 秒为一个控制周期
    while True:
        if time.time() > next_drift:
            target = random.uniform(CPU_MIN, CPU_MAX)
            next_drift = time.time() + random.uniform(30, 90)

        busy = window * target
        b_end = time.time() + busy
        while time.time() < b_end:
            c = gzip.compress(data, 6)
            hashlib.sha256(c).hexdigest()
        idle = window - busy
        if idle > 0:
            time.sleep(idle)


def mem_worker():
    """内存常驻占用：占住总内存的 MEM_MIN~MEM_MAX，并小幅起伏"""
    disguise()
    total = total_mem_bytes()
    while True:
        target_pct = random.uniform(MEM_MIN_PCT, MEM_MAX_PCT)
        target_bytes = int(total * target_pct)
        try:
            block = bytearray(target_bytes)
            # 每隔一页写一下，确保物理内存真正被占用
            for i in range(0, target_bytes, 4096):
                block[i] = 1
        except MemoryError:
            block = bytearray(int(total * MEM_MIN_PCT))
        hold = random.uniform(60, 180)
        end = time.time() + hold
        while time.time() < end:
            block[random.randint(0, len(block) - 1)] = random.randint(0, 255)
            time.sleep(random.uniform(2, 6))
        del block
        time.sleep(random.uniform(1, 3))


def io_worker():
    """零星网络 + 磁盘 IO，增加曲线真实感"""
    disguise()
    while True:
        r = random.random()
        if r < 0.6:
            for _ in range(random.randint(1, 3)):
                try:
                    req = urllib.request.Request(
                        random.choice(URLS), headers={"User-Agent": "Mozilla/5.0"}
                    )
                    urllib.request.urlopen(req, timeout=10).read(
                        random.randint(1024, 20480)
                    )
                except Exception:
                    pass
                time.sleep(random.uniform(0.5, 3))
        else:
            try:
                with tempfile.NamedTemporaryFile(delete=False, dir="/tmp") as f:
                    path = f.name
                    f.write(os.urandom(random.randint(1, 20) * 1024 * 1024))
                with open(path, "rb") as rf:
                    while rf.read(1024 * 1024):
                        pass
                os.remove(path)
            except Exception:
                pass
        time.sleep(random.uniform(10, 40))


def main():
    disguise()
    cores = os.cpu_count() or 1
    procs = []

    for _ in range(cores):
        p = multiprocessing.Process(target=cpu_worker, daemon=True)
        p.start()
        procs.append(p)

    p = multiprocessing.Process(target=mem_worker, daemon=True)
    p.start()
    procs.append(p)

    p = multiprocessing.Process(target=io_worker, daemon=True)
    p.start()
    procs.append(p)

    while True:
        time.sleep(10)
        for i, p in enumerate(procs):
            if not p.is_alive():
                if i < cores:
                    np = multiprocessing.Process(target=cpu_worker, daemon=True)
                elif i == cores:
                    np = multiprocessing.Process(target=mem_worker, daemon=True)
                else:
                    np = multiprocessing.Process(target=io_worker, daemon=True)
                np.start()
                procs[i] = np


if __name__ == "__main__":
    main()
