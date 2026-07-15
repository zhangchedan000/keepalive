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
  apt-get update
  apt-get install -y python3 curl
fi

mkdir -p "$CONFIG_DIR" "$STATE_DIR/cache"
curl -fsSL "${RAW_BASE}/keepalive.py" -o "$PY"
curl -fsSL "${RAW_BASE}/keepalive.service" -o "$UNIT"
chmod 755 "$PY"

if [ ! -f "$CONFIG_DIR/config.json" ]; then
cat > "$CONFIG_DIR/config.json" <<'JSON'
{
  "cycle_days": 7,
  "cpu_start_pct": 20.0,
  "cpu_target_min_pct": 22.0,
  "cpu_target_max_pct": 27.0,
  "memory_start_pct": 20.0,
  "memory_target_min_pct": 22.0,
  "memory_target_max_pct": 26.0,
  "normal_network_min_gb": 0.5,
  "normal_network_max_gb": 3.0,
  "high_network_min_gb": 5.0,
  "high_network_max_gb": 15.0,
  "write_downloads_to_disk": false
}
JSON
fi

systemctl daemon-reload
systemctl enable "$SVC" >/dev/null
systemctl restart "$SVC"

echo "[OK] 安装或更新完成"
echo "状态：python3 /root/keepalive.py --status"
echo "服务：systemctl status keepalive --no-pager"
echo "日志：journalctl -u keepalive -f"
echo "卸载：curl -fsSL ${RAW_BASE}/uninstall.sh | sudo bash"
