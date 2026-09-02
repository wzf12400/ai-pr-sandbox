#!/bin/bash
# Jira 监控（8098）启动包装：本地 / 服务器通用
# 幂等：端口已被占用说明已在运行，直接退出
if lsof -iTCP:8098 -sTCP:LISTEN >/dev/null 2>&1; then
    exit 0
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
if [[ "$OSTYPE" == darwin* ]] && [[ -d "/Applications/Google Chrome.app" ]]; then
    if ! open -gj -a "Google Chrome"; then
        echo "warning: unable to start Chrome for Jira session refresh" >&2
    fi
    sleep 2
fi
PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python3}"
if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN="$(command -v python3)"
fi
if [[ -z "$PYTHON_BIN" ]]; then
    echo "error: Python 3 is required to run the Jira monitor" >&2
    exit 1
fi
exec "$PYTHON_BIN" "$ROOT/scripts/run-with-env.py" -- \
    "$PYTHON_BIN" -m src.jira_monitor_api
