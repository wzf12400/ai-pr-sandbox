#!/bin/bash
# worker 启动包装：本地 / 服务器通用
# GITHUB_ISSUE_TOKEN 优先取 .env；没有则退回本机 gh 登录态
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
set -a
. ./.env
set +a
if [ -z "${GITHUB_ISSUE_TOKEN:-}" ]; then
    GITHUB_ISSUE_TOKEN="$(gh auth token 2>/dev/null)"
    export GITHUB_ISSUE_TOKEN
fi
exec .venv/bin/python3 -m src.mock_task_worker --wait-timeout 5
