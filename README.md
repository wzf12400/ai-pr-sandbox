# AI Agent Automation Platform

面向 Jira 需求和日志事件的自动化处理平台。系统负责采集、脱敏、仓库路由、创建
GitHub Issue，并在策略允许时调用 GitHub Copilot Cloud Agent 生成 Draft PR。

## 服务

| 服务 | 默认地址 | 作用 |
| --- | --- | --- |
| Console | `127.0.0.1:7100` | 任务、日志、Jira 和配置管理 |
| Control Plane | `127.0.0.1:8080` | 任务状态、路由、配置和审计 |
| Jira Monitor | `127.0.0.1:8098` | Jira 扫描与派发 |
| Log Monitor | `127.0.0.1:8099` | 日志扫描与聚合 |
| Worker | 后台进程 | Issue、Cloud Agent 和 Draft PR 流程 |

运行依赖 MySQL、Redis、Python 3、Java 21 和 Node.js 22。

## 本地启动

复制 `backend/.env.example` 为 `backend/.env.local`，填写数据库、Redis、GitHub、Jira 和 AI 服务配置，
然后分别启动：

```bash
./backend/scripts/run-control-plane.sh
./backend/scripts/run-worker.sh
./backend/scripts/run-jira-monitor.sh
./backend/scripts/run-log-monitor.sh
./front/scripts/run-console.sh
```

后端启动脚本默认读取 `backend/.env.local`。测试环境设置 `APP_ENV=staging` 后读取
`backend/.env.staging`，生产环境设置 `APP_ENV=production` 后读取
`backend/.env.production`。
敏感值只允许保存在这些已被 Git 忽略的环境文件或部署 Secret 管理系统中。

## 构建

```bash
python3 -m compileall -q backend/src
mvn -q -f backend/control-plane/pom.xml -DskipTests package
npm --prefix front ci
npm --prefix front run build
```

## 部署

前端位于 [`front/`](front/)，可独立构建并发布静态资源。后端位于
[`backend/`](backend/)，通过目录内启动脚本运行。对外只暴露 Console 的统一入口；
Control Plane、Jira Monitor、Log Monitor、MySQL 和 Redis 应保持在受控内网。

仓库授权、Issue 发布和代码执行分别受以下配置约束：

- `backend/control-plane/config/repository-search-scope.json`
- `backend/control-plane/config/repository-auto-publish-policy.json`
- `backend/control-plane/config/code-policies/`
- `backend/control-plane/config/code-preapproval-manifest.json`

所有代码修改只生成 Draft PR，合并仍需人工审核。
