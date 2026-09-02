# Backend

后端包含 Java 控制面、Python Worker、Jira 与日志监控服务，以及部署脚本。

## 配置

```bash
cp .env.example .env.local
python3 -m venv .venv
.venv/bin/pip install -r requirements-worker.txt
```

## 启动

```bash
./scripts/run-control-plane.sh
./scripts/run-worker.sh
./scripts/run-jira-monitor.sh
./scripts/run-log-monitor.sh
```

## 构建

```bash
python3 -m compileall -q src
mvn -q -f control-plane/pom.xml -DskipTests package
```

测试与生产环境使用相同启动脚本，通过 `APP_ENV=staging` 或
`APP_ENV=production` 选择对应环境文件。
