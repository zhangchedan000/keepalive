#!/bin/bash
set -euo pipefail

RAW_BASE="https://raw.githubusercontent.com/zhangchedan000/keepalive/main"

SVC="keepalive"

APP_DIR="/opt/keepalive"
PY="${APP_DIR}/keepalive.py"

UNIT="/etc/systemd/system/${SVC}.service"

CONFIG_DIR="/etc/keepalive"
CONFIG="${CONFIG_DIR}/config.json"

STATE_DIR="/var/lib/keepalive"
LOG_DIR="/var/log/keepalive"


if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 运行。"
  exit 1
fi


echo "[*] Installing Keepalive..."


# -------------------------------
# Dependencies
# -------------------------------

need_pkgs=""

command -v python3 >/dev/null 2>&1 || need_pkgs="${need_pkgs} python3"
command -v curl >/dev/null 2>&1 || need_pkgs="${need_pkgs} curl"


if [ -n "${need_pkgs}" ]; then

  echo "[*] Installing:${need_pkgs}"

  if command -v apt-get >/dev/null 2>&1; then

    apt-get update -y
    apt-get install -y ${need_pkgs}

  elif command -v dnf >/dev/null 2>&1; then

    dnf install -y ${need_pkgs}

  elif command -v yum >/dev/null 2>&1; then

    yum install -y ${need_pkgs}

  elif command -v apk >/dev/null 2>&1; then

    apk add --no-cache ${need_pkgs}

  else

    echo "无法识别包管理器"
    exit 1

  fi

fi



# -------------------------------
# Directories
# -------------------------------

mkdir -p \
"${APP_DIR}" \
"${CONFIG_DIR}" \
"${STATE_DIR}" \
"${STATE_DIR}/cache" \
"${LOG_DIR}"



# -------------------------------
# Download Script
# -------------------------------

echo "[*] Download keepalive.py"


curl -fsSL \
"${RAW_BASE}/keepalive.py" \
-o "${PY}"


chmod 755 "${PY}"



# Python check

python3 -m py_compile "${PY}"

echo "[OK] Python syntax check passed"



# -------------------------------
# Default Config
# -------------------------------

if [ ! -f "${CONFIG}" ]; then


cat > "${CONFIG}" <<'JSON'
{
  "cycle_days": 7,

  "cpu_pause_pct": 85.0,

  "min_available_memory_pct": 10.0,

  "normal_network_min_gb": 1.0,
  "normal_network_max_gb": 5.0,

  "high_network_min_gb": 10.0,
  "high_network_max_gb": 50.0,

  "write_downloads_to_disk": false,

  "log_backup_days": 7,
  "log_gzip": true
}
JSON


echo "[OK] Config created"

else

echo "[*] Keep existing config"

fi



# -------------------------------
# Stop old service
# -------------------------------

systemctl stop "${SVC}" >/dev/null 2>&1 || true



# -------------------------------
# Systemd
# -------------------------------


cat > "${UNIT}" <<EOF

[Unit]
Description=Keepalive Adaptive Workload Controller
After=network-online.target
Wants=network-online.target


[Service]
Type=simple

ExecStart=/usr/bin/python3 ${PY}

Restart=always
RestartSec=15


Nice=10

IOSchedulingClass=best-effort
IOSchedulingPriority=7


CPUQuota=360%

MemoryMax=35%


NoNewPrivileges=true

PrivateTmp=true


ProtectKernelTunables=true
ProtectControlGroups=true
ProtectSystem=full


ReadWritePaths=${STATE_DIR} ${CONFIG_DIR} ${LOG_DIR}


TimeoutStopSec=20



[Install]
WantedBy=multi-user.target

EOF



# -------------------------------
# Start
# -------------------------------


systemctl daemon-reload

systemctl enable "${SVC}" >/dev/null 2>&1

systemctl restart "${SVC}"



echo ""
echo "================================="
echo " Keepalive installed successfully"
echo "================================="
echo ""

echo "Status:"
echo " python3 ${PY} --status"

echo ""

echo "Service:"
echo " systemctl status ${SVC}"

echo ""

echo "Log:"
echo " tail -f ${LOG_DIR}/keepalive.log"

echo ""

echo "Profile:"
echo " ${STATE_DIR}/profile.json"
