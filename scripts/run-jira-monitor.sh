#!/bin/bash
# Jira 监控（8098）启动包装：本地 / 服务器通用
# 幂等：端口已被占用说明已在运行，直接退出
if lsof -iTCP:8098 -sTCP:LISTEN >/dev/null 2>&1; then
    exit 0
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
set -a
. ./.env
set +a
exec .venv/bin/python3 -m src.jira_monitor_api
