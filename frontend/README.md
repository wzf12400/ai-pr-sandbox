# AI 任务控制台

这是 GitHub AI Agent 的本机测试前端。它支持：

- 提交自然语言任务或模拟的已脱敏日志故障；
- 查看目标仓库匹配结果；
- 查看日志故障的首次/最近出现时间、本次/历史次数和影响接口；
- 查看任务状态、测试结果和事件时间线；
- 手动刷新 Worker 执行后的最新状态。

前端通过同源代理连接 `http://127.0.0.1:8080`，浏览器不会直接跨域访问 Java。
页面只负责提交和展示任务，不会绕过后端策略。任务仍记录为
`executionMode=MOCK`，Issue、公司自动审批、Copilot 和 Draft PR 写入门禁默认
关闭；只有显式启用并通过固定策略校验后，后端才可能写入授权的公开测试仓库。
页面中的日志表单仍是合成契约测试；已配置日志平台的单批次采集请通过仓库
根目录的 `./bin/log-platform-to-tasks --once --prompt-password` 运行，采集结果会
自动显示在同一个任务列表中。该适配器不是持续 watcher。

## 本机运行

先启动 Java 控制面和本机 MySQL、Redis，然后在本目录运行：

```bash
npm ci
npm run dev
```

浏览器访问 `http://localhost:3000/`。

创建任务后，在仓库根目录单次运行 Mock Worker：

```bash
.venv/bin/python -m src.mock_task_worker --once
```

回到页面点击“刷新状态”即可看到执行结果。控制面地址可通过
`CONTROL_PLANE_URL` 修改，默认只连接本机 `127.0.0.1:8080`。

## 检查

```bash
npm run lint
npm test
```
