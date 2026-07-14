#!/bin/bash
set -euo pipefail
SVC="keepalive"
if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 运行。"
  exit 1
fi
systemctl disable --now "$SVC" 2>/dev/null || true
pkill -f '/root/keepalive.py' 2>/dev/null || true
rm -f "/etc/systemd/system/${SVC}.service" /root/keepalive.py
rm -rf /etc/keepalive /var/lib/keepalive /var/log/keepalive /run/keepalive
systemctl daemon-reload
systemctl reset-failed "$SVC" 2>/dev/null || true
echo "[OK] 已卸载，脚本、配置、状态、日志和下载