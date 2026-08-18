# AI Agent 控制台 · 接力文档

> 更新时间：2026-08-14 14:35 ｜ 仓库：wzf12400/ai-pr-sandbox ｜ 工作区：/Users/zf/Desktop/ai-pr-sandbox

## 这个项目是什么

企业内部 AI 代码变更 agent 的端到端闭环：

```
自然语言 / 日志故障
  → 控制面（Java Spring，8080）：脱敏 → 授权仓库目录匹配 → 任务落库 → Redis 队列
  → worker（Python，持续模式）：GPT-5 mini 生成 Issue → 复核 → 真实发布到 GitHub
  → 自动审批标签（ai-code-approved，策略钉哈希，只对新建 Issue）
  → Copilot CLI（gpt-5.6-sol）在 .worker-repos 克隆里改代码 → 策略测试
  → Draft PR（不自动合并，人工 review 收尾）
```

## 目录结构

| 路径 | 说明 |
|---|---|
| `control-plane/` | Java 21 + Spring Boot 控制面，端口 8080，MySQL + Redis |
| `src/mock_task_worker.py` | Python worker（持续循环模式），跑完整流水线 |
| `src/ai_issue_generator.py` | GPT-5 mini Issue 生成 + 复核（复核已放宽：主需求明确即可） |
| `src/log_monitor_api.py` | 日志监控 API，端口 8099（Kibana/ES 扫描、聚类、自动化规则） |
| `src/approved_issue_dispatcher.py` / `copilot_code_modifier.py` | Copilot 调度器与门禁（一次性 CLI 设计） |
| `src/code_execution_preapproval.py` | 预审批策略（允许 LOG/JIRA/NATURAL_LANGUAGE） |
| `console/` | React + Vite 前端（Codex 风格），dev 端口 7100 |
| `.env` | 全部密钥（gitignored，600 权限），改它用 Bash printf |
| `.worker-repos/ai-pr-sandbox` | worker 专用克隆，调度后自动复位到干净的 main |

## 当前状态

- **PR #64（Jira 接入）已合并到 main**。当前修复分支 fix/monitor-stability 待开 PR。
- **2026-08-17/18 稳定性修复**（在本分支）：
  - log_monitor_api：缓存 TTL 30s→300s + 后台自动扫描线程（修复前端轮询把扫描堆死）；聚类展示层按 issue_signature 指纹跨 trace 合并（修复同一错误一请求一聚类）；合并 ref 保留 incident_ref: 前缀（修复控制面 400 拒单）；无 request_path 时展示 codeLocations（出错类.方法）
  - jira_connector：新增 JiraAuthError（401/403/SSO 重定向/非 JSON 统一归类）
  - jira_session_refresh（新）：会话过期自动续期——WebBridge 驱动 Chrome 走 Jira 原生表单登录（JIRA_USERNAME/JIRA_PASSWORD 在 .env），提取 Cookie 写回 .env 并热更新进程环境；jira_monitor_api 自动扫描遇 JiraAuthError 自动触发（冷却 10 分钟）
  - 关键发现：公网网关下 curl 自己表单登录的会话对 REST 无效，必须用浏览器建会话后导出 Cookie（指纹绑定，原因未深究）；Basic Auth 被网关拦截不可行
- **运行中的服务**：控制面 8080、worker、日志监控 8099（带自动扫描）、Jira 监控 8098（带自动扫描+自动续期）、vite 7100。
- **Issue 发布门禁已开启**；**Copilot 代码修改已开启**（`WORKER_CODE_MODE=publish_pr`）。
- **待处理的开放 PR（都是 Copilot 生成的测试 PR，等用户决定）**：
  - #49（power 幂运算，与 #48 关联，是调试期的重复产物）
  - #51（mod 取余，Issue #50）
  - #53（abs 绝对值，Issue #52）
  - #57（Jira 来源首单：KEYB-3784 direct boot 新需求，Issue #56，端到端自动化验证产物）
- **测试残留 Issue**：#44-#48 已于 2026-08-14 关闭清理完毕（#48 关闭后 PR #49 已无关联 Issue）。
- 测试基线：Java 38 个全绿；Python 407+ 个全绿。

## JIRA 接入（进行中，2026-08-14）

目标平台：`https://jira.xinmei365.com`（钉钉 SSO 网关 + 后端就是 Kika JIRA 6.3.6，
与 `10.11.11.156` 同版本同 build，疑为同一实例的内外网两个入口）。
**未解之谜**：wzf/123456 网页登录成功，但 LAN 地址 Basic Auth 仍 401
（AUTHENTICATED_FAILED，非验证码锁定）——不影响，认证走 Cookie 方案。

认证方案：**浏览器 SSO 会话 Cookie**，已写入 `.env`
（`JIRA_BASE_URL` / `JIRA_SESSION_COOKIE`，值含空格分号必须加双引号）。
Cookie 失效后重新走 WebBridge 提取（Chrome 已装扩展，session 名 `jira-access-verify`）。

已完成（本工作区未提交）：

- **勘察已跑通**：`python3 -m src.jira_connector survey` → 45 个项目 → `.jira-survey.json`。
  优先级是中文：致命/严重/一般/提示；Issue 类型：缺陷/新需求/任务/Story/Epic 等。
  KEYB（Keyboard及App）有 20 个组件、3816 个 Issue。
- `src/jira_connector.py`：`survey` + `poll`（JQL watermark 增量 → intake 映射 →
  敏感扫描 fail-closed → 确定性路由 → 影子日志；`--dispatch` 且 auto_dispatch=true
  才真实建任务，仅 loopback）。**Issue 类型用客户端过滤**（`issue_types` 配置），
  JQL 里过滤中文类型名会 400（"字段中没有 '缺陷'"），勿用。附件只留元数据。
  中文类型映射已内置（缺陷→Bug、新需求→Feature）。
- **真实 Bug 全链路验证通过**：KEYB-3858 映射正确、截图附件只留元数据、
  敏感扫描 clean、单绑定路由 RESOLVED。
- `control-plane/config/jira-projects.json`：映射配置骨架（SANDBOX 示例 disabled，
  severity_map 已是中文优先级）。
- 控制面 JIRA 证据通道：`JiraIssueRequest`（SANITIZED + key 格式 + 项目归属 +
  https URL + resolvedRepository 必须在授权目录内），按 sourceReference 去重，
  显式绑定仓库直通（跳过文本匹配器），`IssueProfile.JIRA_ISSUE`。
- worker 接受 `JIRA` + `JIRA_ISSUE` profile。
- 测试：Python 402 全绿（新增 21）；Java 38 全绿（旧 `keepsJiraDisconnected` 已替换）。

待办：

- **端到端已验证（2026-08-14）**：KEYB-3784（新需求）→ 自动扫描 → 路由 → 任务
  → Issue #56（自动 ai-code-approved）→ Copilot → **Draft PR #57 → AWAITING_PR_REVIEW**。
  全程零人工。
- 接线更多项目：已接 7 个活跃项目（AI/FF/AE/AF/CEL/MOD/KEYB，均只扫「新需求」，
  单绑沙箱仓库；仅 KEYB 开了 auto_dispatch，其余纯扫描展示）。存量影子扫描已
  跑过一轮（17 条入列）。接真实仓库时改 `jira-projects.json` 的 repository 即可
  （真实仓库需先加进 `repository-search-scope.json` 授权目录，用户此前暂拒）
- 持续运行：`set -a && . ./.env && set +a && .venv/bin/python3 -m src.jira_connector poll
  --dispatch --interval-seconds 300`（首次启用项目自动跳过存量，防洪水；
  每轮派发上限 `max_dispatch_per_poll`）
- **前端已完成（2026-08-14）**：
  - `src/jira_monitor_api.py`（端口 8098，loopback）：`GET /jira-monitor`、
    `POST /jira-monitor/scan|dispatch|rules`；**内置自动扫描线程**，默认每 300s
    `poll(dispatch=True)`（尊重每项目 auto_dispatch），间隔用
    `JIRA_MONITOR_SCAN_INTERVAL` 调（下限 60s），状态暴露在 `autoScan` 字段
  - 启动：`set -a && . ./.env && set +a && nohup .venv/bin/python3 -m src.jira_monitor_api > /tmp/jira-monitor.log 2>&1 &`
  - console：`JiraMonitor.tsx` 悬浮卡片（日志监控下方，top-[132px]）+ 大窗
    （统计/接线项目规则开关/按项目分组的需求列表/单条「建任务」按钮）；
    侧边栏新增 Jira 线程栏；vite 代理 `/jira-monitor` → 8098（改过 vite.config，
    dev server 需重启生效）
  - 测试：Python 406 全绿（新增 test_jira_monitor_api.py 4 个）

## 服务启动/重启命令

```bash
cd /Users/zf/Desktop/ai-pr-sandbox

# 控制面（必须 JAVA_HOME 指定 21）
set -a && . ./.env && set +a && cd control-plane && \
  JAVA_HOME=/opt/homebrew/opt/openjdk@21 nohup mvn spring-boot:run > /tmp/control-plane.log 2>&1 &

# worker（持续模式；--once 为单次）
set -a && . ./.env && set +a && \
  GITHUB_ISSUE_TOKEN=$(gh auth token) nohup .venv/bin/python3 -m src.mock_task_worker \
  --wait-timeout 5 > /tmp/mock-worker.log 2>&1 &

# 日志监控
nohup .venv/bin/python3 -m src.log_monitor_api > /tmp/log-monitor.log 2>&1 &

# 前端（Kimi Work 管理，不要自己留 dev server）
cd console && npm run dev   # 端口 7100
```

## 关键环境变量（值在 .env）

- `AI_BASE_URL` / `AI_API_KEY` / `AI_MODEL=ailemac/gpt-5-mini` / `AI_SAFETY_IDENTIFIER` — GPT-5 mini 网关
- `WORKER_ISSUE_PUBLICATION_ENABLED=true` + `WORKER_ISSUE_POLICY_SHA256` — Issue 发布门禁
- `WORKER_CODE_MODE=publish_pr`、`WORKER_CODE_AUTO_APPROVAL_ENABLED=true` + `WORKER_CODE_AUTO_APPROVAL_POLICY_SHA256` — Copilot 线与自动审批
- `AI_ASYNC_REPLY=true`（默认）— AI 回复异步化；测试配置里关掉
- `GITHUB_ISSUE_TOKEN=$(gh auth token)` 启动时注入，不写进文件

## 安全边界（不要无意中削弱）

- 路由最终由确定性授权目录匹配器决定，AI 只提供文本线索
- 预审批只对**本次新建**的 Issue 打 `ai-code-approved` 标签；复用/去重的 Issue 保持原审批状态
- Copilot 仅允许写 `src/**`、`tests/**`；禁改 `.github/`、部署、基础设施；只发 Draft PR，不自动合并
- 所有用户输入和模型输出过脱敏器；策略文件改动会使钉住的 SHA 失效（fail-closed）

## 踩过的坑（排障备忘）

1. **worker 报 "unclassified high-entropy data"**：Issue 正文里我们自己生成的 64 位 hex（策略 SHA/指纹注释）触发高熵拦截，`LocalRepositoryExecutionEngine` 已用 `_LOCATOR_NOISE_PATTERN` 剥离。
2. **"一任务一 Issue"**：重试时重复建 Issue 会导致 attach 冲突 FAILED。claim 响应已带 issueNumber/issueUrl，重试复用。
3. **worker 克隆停在 Issue 分支**：调度器一次性设计会保留分支；worker 已在 finally 里自动复位（fetch + checkout -f main + reset --hard origin/main + clean，保留 .issue-code-output 审计）。
4. **`save()` 返回旧快照**：预分配 UUID 的实体 save 走 merge，`create()` 返回前必须 `findJobForUpdate` 重读。
5. **前端闪烁**：轮询时不要 setLoading(true) 卸载内容，只在切换线程时显示骨架屏。
6. **规则启用"失效"假象**：后端正常，是前端保存后没刷新（30s 轮询）；已改为保存后立即 reload。
7. 改 `control-plane/config/*.json` 策略后必须重算 SHA256 同步进 `.env`，否则门禁 fail-closed。

## 下一步候选

- 用户审批/合并或关闭 PR #49、#51、#53（#49 关联 Issue 已关闭且是调试期重复产物，建议直接关闭）
- 日志自动化规则（聚类阈值建任务）已可真实跑通，可观察一轮真实故障的「日志 → Issue → Draft PR」
- JIRA 接入进行中，详见上方「JIRA 接入」节
- 多仓库目录：目前授权目录只有 wzf12400/ai-pr-sandbox 一个（用户曾拒绝加真实仓库，不要再主动提议）

## 测试命令

```bash
.venv/bin/python3 -m unittest discover -s tests        # Python 381 个
cd control-plane && JAVA_HOME=/opt/homebrew/opt/openjdk@21 mvn test   # Java 36 个
cd console && npm run build                            # 前端构建验证
```
