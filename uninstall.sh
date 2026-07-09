#!/bin/bash
# System Metrics Collector - 卸载
set -e
SVC="keepalive"
systemctl disable --now "$SVC" 2>/dev/null || true
rm -f "/etc/systemd/system/${SVC}.service"
rm -f /root/keepalive.py
systemctl daemon-reload
echo "[✓] 已卸载。"
