# Adaptive Resource Workload Controller

面向自有 Linux 服务器的透明资源负载服务。它先读取整机 CPU、内存和网络使用量,已有业务达标时不额外补负载,低于目标时只补差额。

与早期版本的区别:**每台机器会从自身的 machine seed 派生出一个稳定、连续、多维的"运行性格"(Profile)**。这个性格决定 CPU、内存、网络活动的*形态*,让一批机器不会像同一个脚本复制出来的。

## 运行性格(Profile)

首次启动生成 `/var/lib/keepalive/profile.json`,内容重启不变、重装可复现、每台唯一。示例:

```json
{
  "cpu_personality": "compute",
  "memory_personality": "cache-heavy",
  "network_personality": "active",
  "timezone": "US-East",
  "task_interval": 1.27,
  "startup_delay": 70,
  "traits": {
    "compute_bias": 0.71,
    "burstiness": 0.42,
    "cache_stickiness": 0.83,
    "network_activity": 0.55,
    "diurnal_strength": 0.33,
    "weekend_dip": 0.22,
    "jitter_amp_pct": 2.8,
    "spike_prob": 0.02,
    "utc_offset": -5.0,
    "peak_hour_utc": 18.0
  }
}
```

`*_personality` 只用于状态展示;真正驱动行为的是连续的 `traits`。想手动指定某台机器的性格,直接改这个文件后 `systemctl restart keepalive` 即可。

## 行为形态

- **CPU 状态机**:`idle → normal → busy → cooldown`,加权且不规整地循环(`normal` 占主导,保证 7 天 p95 稳在回收阈值之上)。目标值上叠加逐 tick 抖动(±1.5–4%)和偶发短尖峰(持续几十秒),曲线毛糙不平滑。
- **CPU 任务池**:压缩(gzip / lzma / zlib)、数据处理(JSON / CSV / 字符串 / 正则)、数据库(SQLite insert / select / index / vacuum,跑在每个 worker 独立的内存库,不影响统计)。任务权重随 `compute_bias` 变化。
- **内存缓存模型**:模拟带 hit / miss / eviction 的常驻缓存,占用在目标带内漂移并周期性成批释放,夜间略微回落。可用内存过低或 Swap 增长时强制释放。
- **网络以小请求为主**:日常是周期性 API/JSON 拉取、HEAD 心跳、Range 部分请求、压缩包读取校验;大块下载只在需要补当日流量缺口时才用,且拆成不规整的小块。真实业务流量优先计入。
- **时区作息**:每台机器按 seed 落在某个真实区域(美/欧/亚等)的白天为忙时段,深夜近乎静默;各机器峰值 UTC 小时不同,整批不会同时冲高。
- **周末**:按机器自身时区的周末,活跃度进一步下调(`weekend_dip`)。

## 健康检查

启动时探测核数、内存、磁盘、网卡,自动决定 worker 数与内存上限(小内存机器更保守)。

## 日志

`/var/log/keepalive/keepalive.log`,按天轮转、保留 7 天(可 gzip)。记录状态转移、缓存刷新、网络任务、周期滚动等。

## 一键安装或更新

```bash
curl -fsSL https://raw.githubusercontent.com/zhangchedan000/keepalive/main/install.sh | sudo bash
```

安装脚本幂等,重跑即更新并重启服务;已存在的 `config.json` 不会被覆盖。

## 查看状态

```bash
python3 /root/keepalive.py --status          # 人类可读
python3 /root/keepalive.py --status --json    # 机器可读
systemctl status keepalive --no-pager
journalctl -u keepalive -f
tail -f /var/log/keepalive/keepalive.log
```

`--status` 示例:

```text
Machine Profile:
  compute / cache-heavy / active
  compute=0.71 burst=0.42 cache=0.83 net=0.55
  task_interval=1.27  startup_delay=70s

CPU:
  state    normal
  current  23%
  target   25%
  workers  2
  p95      24.8%

Memory:
  used     26%
  cache    2.1GB
  hit/miss 1840/612 evict=44

Network:
  today    1.2GB
  target   2.4GB
  script   1.2GB

Cycle:
  day      4/7
  high day 6
  updated  2026-07-15T10:30:00
```

## 配置

配置文件 `/etc/keepalive/config.json`,只需写要覆盖的字段(其余用默认)。改后 `systemctl restart keepalive`。

网络端点在 `network_endpoints` 里,可全部换成你自己的域名。旧版字段(`cpu_target_min_pct` 等)仍作兜底,已部署机器升级不受影响。

网络流量可能产生服务商费用。默认平常日 0.3–2GB、周期内随机一天 5–13GB(均为硬上限,按性格在带内缩放),整机约 46–69GB/月(轻→重性格)。按月总流量计费的机器(如 RackNerd),可在 `config.json` 里调小 `high_network_max_gb` / `normal_network_max_gb`。Oracle 回收只硬性看 CPU 的 7 天 p95,砍网络配额不影响保活。

## 一键卸载

```bash
curl -fsSL https://raw.githubusercontent.com/zhangchedan000/keepalive/main/uninstall.sh | sudo bash
```

卸载会删除主脚本、systemd 单元、配置、状态数据库、Profile、日志和下载缓存。
