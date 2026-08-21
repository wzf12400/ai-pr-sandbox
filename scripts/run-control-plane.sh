#!/bin/bash
# 控制面（8080，Java Spring）启动包装：本地 / 服务器通用
# 需要 JDK 21：优先 JAVA_HOME，其次 macOS Homebrew 路径，最后依赖 PATH 里的 java
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
set -a
. ./.env
set +a
if [ -z "${JAVA_HOME:-}" ] && [ -d /opt/homebrew/opt/openjdk@21 ]; then
    export JAVA_HOME=/opt/homebrew/opt/openjdk@21
fi
cd control-plane || exit 1
exec mvn spring-boot:run
