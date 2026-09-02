#!/bin/bash
# 把四个 systemd 服务装进当前机器（Linux 服务器上线测试用）
# 用法：在 backend 目录运行 sudo bash scripts/install-systemd.sh staging
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICES="ai-pr-control-plane ai-pr-worker ai-pr-jira-monitor ai-pr-log-monitor"
APP_ENV="${1:-staging}"

case "$APP_ENV" in
    local|staging|production) ;;
    *)
        echo "环境必须是 local、staging 或 production" >&2
        exit 1
        ;;
esac

ENV_FILE="$ROOT/.env.$APP_ENV"
if [[ ! -f "$ENV_FILE" || -L "$ENV_FILE" ]]; then
    echo "缺少环境文件：$ENV_FILE" >&2
    exit 1
fi

for name in $SERVICES; do
    sed -e "s|@ROOT@|$ROOT|g" -e "s|@APP_ENV@|$APP_ENV|g" \
        "$ROOT/deploy/systemd/$name.service" \
        > "/etc/systemd/system/$name.service"
done

systemctl daemon-reload
for name in $SERVICES; do
    systemctl enable --now "$name"
done

echo "已安装并启用（APP_ENV=$APP_ENV）："
systemctl --no-pager --type=service --state=running | grep ai-pr || true
