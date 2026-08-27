#!/bin/bash
# 前端控制台（7100）启动包装。
if lsof -iTCP:7100 -sTCP:LISTEN >/dev/null 2>&1; then
    exit 0
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/console" || exit 1
exec npm run dev -- --host 127.0.0.1 --port 7100 --strictPort
