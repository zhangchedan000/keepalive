#!/bin/bash
#
# System Metrics Collector - 一键安装
# 用法:
#   curl -fsSL https://raw.githubusercontent.com/zhangchedan000/keepalive/main/install.sh | bash
#
set -e

# ↓↓↓ push 前把这里改成你的 GitHub 用户名/仓库名 ↓↓↓
RAW_BASE="https://raw.githubusercontent.com/zhangchedan000/keepalive/main"
# ↑↑↑ 已填好你的用户名 ↑↑↑

SVC="keepalive"
PY="/root/keepalive.py"
UNIT="/etc/systemd/system/${SVC}.service"

echo "[*] 检查依赖..."
if ! command -v python3 >/dev/null 2>&1; then
    apt-get update && apt-get install -y python3
fi
if ! command -v curl >/dev/null 2>&1; then
    apt-get update && apt-get install -y curl
fi

echo "[*] 下载负载脚本..."
curl -fsSL "${RAW_BASE}/keepalive.py" -o "$PY"
chmod +x "$PY"

echo "[*] 安装 systemd 服务..."
curl -fsSL "${RAW_BASE}/keepalive.service" -o "$UNIT"

echo "[*] 启用并启动..."
systemctl daemon-reload
systemctl enable --now "$SVC"

echo
echo "[✓] 安装完成。"
echo "    状态:  systemctl status ${SVC}"
echo "    日志:  journalctl -u ${SVC} -f"
echo "    进程:  ps aux | grep python3-worker"
echo "    卸载:  curl -fsSL ${RAW_BASE}/uninstall.sh | bash"
