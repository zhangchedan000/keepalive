# Adaptive Resource Workload Controller

面向自有 Linux 服务器的透明资源负载测试服务。它先读取整机 CPU、内存和网络使用量，已有业务达到目标时不额外补负载，低于目标时只补差额。

## 默认配置

- 7 天一个周期，计划会保存在本机，重启不会重新抽取。
- CPU：低于 20% 时开始补足，目标在 22%–27% 之间；整机 CPU 超过 85% 时暂停。
- 内存：低于 20% 时逐步建立缓存，目标在 22%–26%；可用内存低于 10% 或 Swap 增长时释放。
- 网络：普通日整机 1–5GB；每个周期随机一天 10–50GB。真实业务流量优先计入，只补剩余差额。
- 下载默认流式读取后丢弃，不写磁盘。可在配置中启用缓存落盘。
- CPU 工作由 gzip、SHA256、JSON 等普通计算任务组成。
- SQLite 保存最近 8 天的分钟级统计。

## 一键安装或更新

```bash
curl -fsSL https://raw.githubusercontent.com/zhangchedan000/keepalive/main/install.sh | sudo bash
```

重新运行安装命令会覆盖脚本并重启现有服务。

## 查看状态

```bash
python3 /root/keepalive.py --status
systemctl status keepalive --no-pager
journalctl -u keepalive -f
```

## 配置

配置文件：

```text
/etc/keepalive/config.json
```

修改后重启：

```bash
systemctl restart keepalive
```

网络流量可能产生服务商费用。默认高流量日上限为 50GB，部署前请确认服务器流量套餐。

## 一键卸载

```bash
curl -fsSL https://raw.githubusercontent.com/zhangchedan000/keepalive/main/uninstall.sh | sudo bash
```

卸载会删除主脚本、systemd 服务、配置、状态数据库、日志和下载缓存。
