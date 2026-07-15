#!/bin/bash
set -euo pipefail

RAW_BASE="https://raw.githubusercontent.com/zhangchedan000/keepalive/main"
SVC="keepalive"
PY="/root/keepalive.py"
UNIT="/etc/systemd/system/${SVC}.service"
CONFIG_DIR="/etc/keepalive"
CONFIG="${CONFIG_DIR}/config.json"
STATE_DIR="/var/lib/keepalive"
LOG_DIR="/var/log/keepalive"

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 运行。"
  exit 1
fi

# --- 依赖：python3 + curl（缺则按发行版安装） ------------------------------- #
need_pkgs=""
command -v python3 >/dev/null 2>&1 || need_pkgs="${need_pkgs} python3"
command -v curl    >/dev/null 2>&1 || need_pkgs="${need_pkgs} curl"
if [ -n "${need_pkgs}" ]; then
  echo "[*] 安装依赖:${need_pkgs}"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y && apt-get install -y ${need_pkgs}
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y ${need_pkgs}
  elif command -v yum >/dev/null 2>&1; then
    yum install -y ${need_pkgs}
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache ${need_pkgs}
  else
    echo "无法识别包管理器，请手动安装:${need_pkgs}"
    exit 1
  fi
fi

# --- 目录 ------------------------------------------------------------------- #
mkdir -p "${CONFIG_DIR}" "${STATE_DIR}" "${STATE_DIR}/cache" "${LOG_DIR}"

# --- 主脚本（本地存在则用本地，否则拉取） ----------------------------------- #
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "${SRC_DIR}" ] && [ -f "${SRC_DIR}/keepalive.py" ]; then
  install -m 0755 "${SRC_DIR}/keepalive.py" "${PY}"
  echo "[*] 使用本地 keepalive.py"
else
  echo "[*] 拉取 keepalive.py"
  curl -fsSL "${RAW_BASE}/keepalive.py" -o "${PY}"
  chmod 0755 "${PY}"
fi

# --- 默认配置（存在则保留，不覆盖用户改动） --------------------------------- #
if [ ! -f "${CONFIG}" ]; then
  echo "[*] 写入默认配置 ${CONFIG}"
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
else
  echo "[*] 已存在配置，保留不覆盖:${CONFIG}"
fi

# --- systemd 单元 ----------------------------------------------------------- #
echo "[*] 写入 systemd 单元 ${UNIT}"
cat > "${UNIT}" <<'UNITEOF'
[Unit]
Description=Adaptive Resource Workload Controller
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /root/keepalive.py
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
ReadWritePaths=/var/lib/keepalive /etc/keepalive /var/log/keepalive
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
UNITEOF

# --- 启动 / 重启 ------------------------------------------------------------ #
systemctl daemon-reload
systemctl enable "${SVC}" >/dev/null 2>&1 || true
systemctl restart "${SVC}"

echo ""
echo "[OK] 安装/更新完成。"
echo "     状态:python3 ${PY} --status"
echo "     日志:journalctl -u ${SVC} -f  或  tail -f ${LOG_DIR}/keepalive.log"
echo "     Profile:${STATE_DIR}/profile.json（首启自动生成，每台唯一）"
