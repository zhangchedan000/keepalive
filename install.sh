#!/bin/bash
# OCI Instance Activity Service - 一键安装
# 用法：
# curl -fsSL https://raw.githubusercontent.com/zhangchedan000/keepalive/main/install.sh | bash

set -euo pipefail

RAW_BASE="https://raw.githubusercontent.com/zhangchedan000/keepalive/main"
SVC="keepalive"
PY="/root/keepalive.py"
UNIT="/etc/systemd/system/${SVC}.service"

if [ "$(id -u)" -ne 0 ]; then
    echo "请使用 root 运行。"
    exit 1
fi

echo "[*] 检查依赖..."
if ! command -v python3 >/dev/null 2>&1; then
    apt-get update
    apt-get install -y python3
fi
if ! command -v curl >/dev/null 2>&1; then
    apt-get update
    apt-get install -y curl
fi

echo "[*] 下载脚本..."
curl -fsSL "${RAW_BASE}/keepalive.py" -o "${PY}"
chmod 755 "${PY}"

echo "[*] 安装 systemd 服务..."
curl -fsSL "${RAW_BASE}/keepalive.service" -o "${UNIT}"
mkdir -p /var/lib/oci-activity

systemctl daemon-reload
systemctl enable --now "${SVC}"

echo
echo "[OK] 安装完成"
echo "状态：systemctl status ${SVC} --no-pager"
echo "日志：journalctl -u ${SVC} -f"
echo "进程：pgrep -af keepalive.py"
echo "卸载：curl -fsSL ${RAW_BASE}/uninstall.sh | bash"
