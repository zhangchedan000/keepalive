#!/usr/bin/env python3
"""
System Metrics Collector
拟真综合负载：日夜作息 CPU 波动 + 常驻内存起伏 + 定期大流量下载(网络保活) + 零星磁盘 IO。
进程名伪装为 python3-worker，三项指标(CPU/内存/网络)均达标。
"""
import os, time, random, gzip, hashlib, urllib.request, tempfile, multiprocessing, datetime

# ===== 可调参数（想改占用改这里）=====
# 白天时段（服务器本地时间，24小时制）
DAY_START = 8
DAY_END = 24

# 白天 CPU 占用区间（每核）
CPU_DAY_MIN = 0.30
CPU_DAY_MAX = 0.42
# 夜间 CPU 占用区间（每核）
CPU_NIGHT_MIN = 0.12
CPU_NIGHT_MAX = 0.20

# 内存占用区间（总内存百分比）
MEM_MIN_PCT = 0.30
MEM_MAX_PCT = 0.45

# CPU 偶发尖峰
SPIKE_CHANCE = 0.05
SPIKE_MIN = 0.55
SPIKE_MAX = 0.72

# 网络：定期大文件下载（只下不存）
NET_DL_MB_MIN = 300          # 每次下载大小下限(MB)
NET_DL_MB_MAX = 1200         # 每次下载大小上限(MB)
NET_GAP_DAY_MIN = 20 * 60    # 白天两次下载间隔下限(秒)
NET_GAP_DAY_MAX = 40 * 60    # 白天间隔上限
NET_GAP_NIGHT_MIN = 45 * 60  # 夜间间隔下限
NET_GAP_NIGHT_MAX = 90 * 60  # 夜间间隔上限
NET_MAX_SECONDS = 300        # 单次下载最长时间(秒)，防卡死
# =====================================

# 小流量请求目标
SMALL_URLS = [
    "https://www.cloudflare.com/cdn-cgi/trace",
    "https://api.github.com",
    "https://www.bing.com",
    "https://www.debian.org",
    "https://www.wikipedia.org",
]

# 大文件下载源（可指定字节数的优先，挂了自动换下一个）
def big_url(nbytes):
    return [
        f"https://speed.cloudflare.com/__down?bytes={nbytes}",
        "https://speedtest.tele2.net/1GB.zip",
        "https://proof.ovh.net/files/1Gb.dat",
        "http://speedtest.tele2.net/1GB.zip",
    ]


def disguise(name=b"python3-worker"):
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").prctl(15, name, 0, 0, 0)
    except Exception:
        pass


def total_mem_bytes():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 2 * 1024 ** 3


def is_daytime():
    h = datetime.datetime.now().hour
    return DAY_START <= h < DAY_END


def pick_cpu_target():
    if random.random() < SPIKE_CHANCE:
        return random.uniform(SPIKE_MIN, SPIKE_MAX)
    if is_daytime():
        return random.uniform(CPU_DAY_MIN, CPU_DAY_MAX)
    return random.uniform(CPU_NIGHT_MIN, CPU_NIGHT_MAX)


def cpu_worker():
    disguise()
    data = os.urandom(200_000)
    target = pick_cpu_target()
    next_drift = time.time() + random.uniform(30, 90)
    window = 0.2
    while True:
        if time.time() > next_drift:
            target = pick_cpu_target()
            if target >= SPIKE_MIN:
                next_drift = time.time() + random.uniform(8, 25)
            else:
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
    disguise()
    total = total_mem_bytes()
    while True:
        target_pct = random.uniform(MEM_MIN_PCT, MEM_MAX_PCT)
        target_bytes = int(total * target_pct)
        try:
            block = bytearray(target_bytes)
            for i in range(0, target_bytes, 4096):
                block[i] = 1
        except MemoryError:
            block = bytearray(int(total * MEM_MIN_PCT))
        hold = random.uniform(90, 240)
        end = time.time() + hold
        while time.time() < end:
            block[random.randint(0, len(block) - 1)] = random.randint(0, 255)
            time.sleep(random.uniform(2, 6))
        del block
        time.sleep(random.uniform(1, 3))


def download_once(nbytes):
    """下载指定字节数的大文件，只读不存，限时 NET_MAX_SECONDS"""
    deadline = time.time() + NET_MAX_SECONDS
    for url in big_url(nbytes):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                got = 0
                while time.time() < deadline:
                    chunk = r.read(1024 * 256)  # 每次读 256KB
                    if not chunk:
                        break
                    got += len(chunk)
                    if got >= nbytes:
                        break
            return True   # 成功下完或读到目标量
        except Exception:
            continue      # 这个源挂了，换下一个
    return False


def net_worker():
    """网络保活：定期下大文件 + 平时零星小请求"""
    disguise()
    while True:
        # 一次大下载
        mb = random.randint(NET_DL_MB_MIN, NET_DL_MB_MAX)
        download_once(mb * 1024 * 1024)

        # 决定下次大下载的间隔（白天密、夜间疏）
        if is_daytime():
            gap = random.uniform(NET_GAP_DAY_MIN, NET_GAP_DAY_MAX)
        else:
            gap = random.uniform(NET_GAP_NIGHT_MIN, NET_GAP_NIGHT_MAX)

        # 间隔期间穿插小请求，保持网络不完全归零
        end = time.time() + gap
        while time.time() < end:
            try:
                req = urllib.request.Request(
                    random.choice(SMALL_URLS), headers={"User-Agent": "Mozilla/5.0"}
                )
                urllib.request.urlopen(req, timeout=10).read(random.randint(2048, 65536))
            except Exception:
                pass
            time.sleep(random.uniform(20, 60))


def disk_worker():
    disguise()
    while True:
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
        time.sleep(random.uniform(15, 50))


def main():
    disguise()
    cores = os.cpu_count() or 1
    specs = [cpu_worker] * cores + [mem_worker, net_worker, disk_worker]
    procs = []
    for fn in specs:
        p = multiprocessing.Process(target=fn, daemon=True)
        p.start()
        procs.append(p)

    while True:
        time.sleep(10)
        for i, p in enumerate(procs):
            if not p.is_alive():
                np = multiprocessing.Process(target=specs[i], daemon=True)
                np.start()
                procs[i] = np


if __name__ == "__main__":
    main()
