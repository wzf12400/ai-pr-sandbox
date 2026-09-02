# Control Plane

Java 21 / Spring Boot 控制面，负责持久化任务、仓库路由、配置快照、审计事件和
Redis Stream Outbox。MySQL 是任务事实来源，Redis 仅用于唤醒 Worker。

## 配置

运行前通过环境变量提供：

- `DB_URL`、`DB_USER`、`DB_PASSWORD`
- `REDIS_URL`
- `AI_BASE_URL`、`AI_API_KEY`
- `GITHUB_ROUTING_TOKEN`

完整变量模板见 `backend/.env.example`。敏感值不得写入 Git、日志或接口响应。

## 启动

在仓库根目录运行：

```bash
./backend/scripts/run-control-plane.sh
```

默认监听 `127.0.0.1:8080`。健康状态和任务 API 应仅通过内网或统一网关访问。

## 构建

```bash
mvn -q -f backend/control-plane/pom.xml -DskipTests package
```

数据库结构由 `src/main/resources/db/migration/` 中的 Flyway 迁移管理。

## 安全边界

- 日志只接受已脱敏的结构化证据。
- Jira 与日志路由来自 MySQL 中启用的精确映射。
- GitHub Issue 发布和 Cloud Agent 执行分别受固定策略摘要约束。
- Cloud Agent 只能生成 Draft PR，不能自动合并或部署。
- 服务默认绑定回环地址；部署时由反向代理、网络策略和身份认证控制外部访问。
