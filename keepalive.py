#!/usr/bin/env python3
"""
System Metrics Collector
拟真综合负载：日夜作息 CPU 波动 + 常驻内存渐进起伏 + 定期大流量下载(网络保活) + 零星磁盘 IO。
进程名伪装为 python3-worker，三项指标(CPU/内存/网络)均达标，含多机随机化。
"""
import os, time, random, gzip, hashlib, urllib.request, tempfile, multiprocessing, datetime

# ===== 可调参数（想改占用改这里）=====
# 白天时段（服务器本地时间，24小时制）
DAY_START = 8
DAY_END = 24

# 白天 CPU 占用区间（每核）
CPU_DAY_MIN = 0.30
CPU_DAY_MAX = 0.42
# 夜间 CPU 占用区间（每核）—— 压在甲骨文回收线(20%)上方，安全不浪费
CPU_NIGHT_MIN = 0.20
CPU_NIGHT_MAX = 0.25

# 内存占用区间（总内存百分比）
MEM_MIN_PCT = 0.30
MEM_MAX_PCT = 0.45

# CPU 偶发尖峰
SPIKE_CHANCE = 0.05
SPIKE_MIN = 0.55
SPIKE_MAX = 0.72

# 网络：定期大文件下载（只下不存）
NET_DL_MB_MIN = 80
NET_DL_MB_MAX = 200
NET_GAP_DAY_MIN = 45 * 60
NET_GAP_DAY_MAX = 90 * 60
NET_GAP_NIGHT_MIN = 90 * 60
NET_GAP_NIGHT_MAX = 180 * 60
NET_MAX_SECONDS = 300        # 单次下载最长时间
NET_MIN_SPEED = 50 * 1024    # 最低可接受速率(50KB/s)，低于此判为龟速，换源

# 多机随机化：启动时随机延迟，避免多台机器行为完全同步
STARTUP_JITTER_MAX = 120     # 启动随机延迟 0~120 秒
# =====================================

# 小流量请求目标
SMALL_URLS = [
    "https://www.cloudflare.com/cdn-cgi/trace",
    "https://api.github.com",
    "https://www.bing.com",
    "https://www.debian.org",
    "https://www.wikipedia.org",
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


def big_url(nbytes):
    """下载源：Cloudflare(可指定字节数)优先，后接多个公开测速站备用，顺序打乱增加随机性"""
    cf = [f"https://speed.cloudflare.com/__down?bytes={nbytes}"]
    mirrors = [
        "https://speed.hetzner.de/1GB.bin",
        "https://speedtest.tele2.net/1GB.zip",
        "https://proof.ovh.net/files/1Gio.dat",
        "https://lon.speedtest.clouvider.net/1g.bin",
        "https://nyc.speedtest.clouvider.net/1g.bin",
        "https://la.speedtest.clouvider.net/1g.bin",
    ]
    random.shuffle(mirrors)   # 备用源随机排序，多机不撞同一个
    return cf + mirrors


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
    """内存常驻：分块渐进申请（不一次猛占），到点释放再重来"""
    disguise()
    total = total_mem_bytes()
    CHUNK = 128 * 1024 * 1024   # 每块 128MB，渐进增长
    while True:
        target_bytes = int(total * random.uniform(MEM_MIN_PCT, MEM_MAX_PCT))
        blocks = []
        allocated = 0
        # 分块申请，边申请边真实写入，避免瞬时冲击
        try:
            while allocated < target_bytes:
                size = min(CHUNK, target_bytes - allocated)
                blk = bytearray(size)
                for i in range(0, size, 4096):
                    blk[i] = 1
                blocks.append(blk)
                allocated += size
                time.sleep(0.05)   # 每块之间小停顿，平滑增长
        except MemoryError:
            pass   # 申请到多少算多少，不崩
        # 保持占用一段时间，期间轻触防换出
        end = time.time() + random.uniform(90, 240)
        while time.time() < end and blocks:
            blk = random.choice(blocks)
            blk[random.randint(0, len(blk) - 1)] = random.randint(0, 255)
            time.sleep(random.uniform(2, 6))
        # 先彻底释放再进入下一轮，避免与自身重启叠加
        blocks.clear()
        del blocks
        time.sleep(random.uniform(2, 5))


def download_once(nbytes):
    """下载指定字节数，只读不存；限时+龟速检测，慢就换源"""
    deadline = time.time() + NET_MAX_SECONDS
    for url in big_url(nbytes):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                got = 0
                last_check = time.time()
                last_bytes = 0
                while time.time() < deadline:
                    chunk = r.read(1024 * 256)
                    if not chunk:
                        break
                    got += len(chunk)
                    if got >= nbytes:
                        return True
                    # 每 5 秒检测一次速率，龟速就放弃换下一个源
                    now = time.time()
                    if now - last_check >= 5:
                        speed = (got - last_bytes) / (now - last_check)
                        if speed < NET_MIN_SPEED:
                            break   # 太慢，跳出去换源
                        last_check = now
                        last_bytes = got
            if got >= nbytes:
                return True
        except Exception:
            continue
    return False


def net_worker():
    disguise()
    while True:
        mb = random.randint(NET_DL_MB_MIN, NET_DL_MB_MAX)
        download_once(mb * 1024 * 1024)
        if is_daytime():
            gap = random.uniform(NET_GAP_DAY_MIN, NET_GAP_DAY_MAX)
        else:
            gap = random.uniform(NET_GAP_NIGHT_MIN, NET_GAP_NIGHT_MAX)
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
    # 多机随机化：启动随机延迟，错开多台机器的节奏
    time.sleep(random.uniform(0, STARTUP_JITTER_MAX))
    random.seed()

    cores = os.cpu_count() or 1
    specs = [cpu_worker] * cores + [mem_worker, net_worker, disk_worker]
    procs = []
    for fn in specs:
        p = multiprocessing.Process(target=fn, daemon=True)
        p.start()
        procs.append(p)
        time.sleep(0.2)   # 子进程错峰启动

    # 守护：进程崩了独立重启（每个进程内存互相隔离，不会叠加占用）
    while True:
        time.sleep(10)
        for i, p in enumerate(procs):
            if not p.is_alive():
                np = multiprocessing.Process(target=specs[i], daemon=True)
                np.start()
                procs[i] = np


if __name__ == "__main__":
    main()
