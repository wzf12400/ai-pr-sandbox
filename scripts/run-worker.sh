#!/bin/bash
# worker 启动包装：本地 / 服务器通用
# GITHUB_ISSUE_TOKEN 优先取环境配置；未设置时复用 routing token，再回退 gh 登录态
if ps -axo command= | grep -q '[s]rc.mock_task_worker'; then
    exit 0
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
PYTHON="$ROOT/.venv/bin/python3"
if [ ! -x "$PYTHON" ]; then
    PYTHON="$(command -v python3)"
fi
exec "$PYTHON" "$ROOT/scripts/run-with-env.py" --github-token-fallback -- \
    "$PYTHON" -m src.mock_task_worker --wait-timeout 5
