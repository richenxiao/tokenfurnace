#!/usr/bin/env bash
# TokenFurnace 启动脚本（macOS / Linux）
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[ERROR] 找不到 python3，请先安装 Python 3.9+" >&2
  exit 1
fi

echo "启动 TokenFurnace…控制台会自动在浏览器打开，按 Ctrl+C 停止。"
exec python3 run.py "$@"
