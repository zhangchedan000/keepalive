#!/bin/bash
set -euo pipefail
RAW_BASE="https://raw.githubusercontent.com/zhangchedan000/keepalive/main"
SVC="keepalive"
PY="/root/keepalive.py"
UNIT="/etc/systemd/system/${SVC}.service"
CONFIG_DIR="/etc/keepalive"
STATE_DIR="/var/lib/keepalive"

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 运行。"
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
  apt