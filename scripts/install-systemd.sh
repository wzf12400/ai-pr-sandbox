#!/bin/bash
# 把四个 systemd 服务装进当前机器（Linux 服务器上线测试用）
# 用法：sudo bash scripts/install-systemd.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICES="ai-pr-control-plane ai-pr-worker ai-pr-jira-monitor ai-pr-log-monitor"

for name in $SERVICES; do
    sed "s|@ROOT@|$ROOT|g" "$ROOT/deploy/systemd/$name.service" \
        > "/etc/systemd/system/$name.service"
done

systemctl daemon-reload
for name in $SERVICES; do
    systemctl enable --now "$name"
done

echo "已安装并启用："
systemctl --no-pager --type=service --state=running | grep ai-pr || true
