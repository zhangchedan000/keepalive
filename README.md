# System Metrics Collector

轻量综合负载服务，用于保持云实例活跃。产生 CPU / 网络 / 磁盘 IO / 内存的自然波动曲线。

## 一键安装

> 先把本仓库文件里的 `zhangchedan000/keepalive` 替换成你实际的 GitHub 仓库路径（仅 `install.sh` 顶部一行）。

```bash
curl -fsSL https://raw.githubusercontent.com/zhangchedan000/keepalive/main/install.sh | bash
```

## 常用命令

```bash
systemctl status keepalive          # 运行状态
journalctl -u keepalive -f          # 实时日志
ps aux | grep python3-worker        # 查看进程（显示为 python3-worker）
```

## 卸载

```bash
curl -fsSL https://raw.githubusercontent.com/zhangchedan000/keepalive/main/uninstall.sh | bash
```

## 参数调节

编辑 `keepalive.py`：

- CPU 偏低 → 把主循环 `r < 0.55` 调高到 `0.7`，或调大 `cpu_burst(random.uniform(3, 12))` 上限
- CPU 偏高浪费 → 反向下调
- 网络站点 → 改 `URLS` 列表（可换成自己的服务地址）
- 资源上限 → 在 `keepalive.service` 里改 `CPUQuota` / `MemoryMax`

改完重新 push，机器上重跑一次安装命令即可覆盖更新。

## 适用配置

默认参数针对 Oracle ARM A1（4 OCPU / 24G）。小内存实例请下调 `mem_churn` 的分配量和 `MemoryMax`。
