#!/usr/bin/env python3
"""
System Metrics Collector
轻量综合负载：CPU 尖峰 + 网络 + 磁盘 IO + 内存涨落
进程名伪装为 python3-worker，曲线为毛刺状，无 stress-ng 特征。
"""
import os, time, random, gzip, hashlib, urllib.request, tempfile

# 伪装进程名（在 ps/top 里显示为 python3-worker）
try:
    import ctypes
    ctypes.CDLL("libc.so.6").prctl(15, b"python3-worker", 0, 0, 0)
except Exception:
    pass

URLS = [
    "https://www.cloudflare.com/cdn-cgi/trace",
    "https://api.github.com",
    "https://www.bing.com",
    "https://mirrors.ubuntu.com/mirrors.txt",
    "https://www.debian.org",
]


def cpu_burst(seconds):
    """CPU 尖峰：压缩/哈希/浮点，忙一阵停一阵，形成毛刺曲线"""
    end = time.time() + seconds
    data = os.urandom(random.randint(50_000, 500_000))
    while time.time() < end:
        b_end = time.time() + random.uniform(0.05, 0.4)
        while time.time() < b_end:
            c = gzip.compress(data, random.randint(1, 9))
            hashlib.sha256(c).hexdigest()
            _ = sum(i * i for i in range(random.randint(1000, 8000)))
        time.sleep(random.uniform(0.02, 0.3))


def net_burst():
    """真实对外 HTTPS 请求，产生进出流量"""
    for _ in range(random.randint(1, 3)):
        try:
            req = urllib.request.Request(
                random.choice(URLS), headers={"User-Agent": "Mozilla/5.0"}
            )
            urllib.request.urlopen(req, timeout=10).read(random.randint(1024, 20480))
        except Exception:
            pass
        time.sleep(random.uniform(0.5, 3))


def disk_burst():
    """真实磁盘读写：写临时文件再读再删"""
    try:
        with tempfile.NamedTemporaryFile(delete=False, dir="/tmp") as f:
            path = f.name
            f.write(os.urandom(random.randint(1, 20) * 1024 * 1024))
        with open(path, "rb") as r:
            while r.read(1024 * 1024):
                pass
        os.remove(path)
    except Exception:
        pass


def mem_churn():
    """内存动态分配/释放，非一整块占死"""
    blocks = []
    for _ in range(random.randint(3, 15)):
        blocks.append(bytearray(random.randint(20, 120) * 1024 * 1024))
        time.sleep(random.uniform(0.1, 0.5))
    del blocks


def main():
    random.seed()
    while True:
        # 活跃期 10~30 分钟
        a_end = time.time() + random.randint(600, 1800)
        while time.time() < a_end:
            r = random.random()
            if r < 0.55:
                cpu_burst(random.uniform(3, 12))
            elif r < 0.8:
                net_burst()
            elif r < 0.93:
                disk_burst()
            else:
                mem_churn()
            time.sleep(random.uniform(0.5, 4))
        # 低谷休息 1~5 分钟
        time.sleep(random.randint(60, 300))


if __name__ == "__main__":
    main()
