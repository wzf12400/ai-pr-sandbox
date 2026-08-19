# AI Agent 控制台 · 接力文档

> 更新时间：2026-08-19 11:55 ｜ 仓库：wzf12400/ai-pr-sandbox ｜ 工作区：/Users/zf/Desktop/ai-pr-sandbox
> 新对话第一句：「读一下 HANDOFF.md 继续工作」

## 这个项目是什么

企业内部 AI 代码变更 agent 的端到端闭环，两条输入线：

```
日志线：Kibana/ES 错误日志 → 聚类 → 锚点代码搜索路由 → 建任务
Jira 线：jira.xinmei365.com 新需求 → 确定性/锚点/AI 画像三级路由 → 建任务
        ↓
控制面（Java Spring，8080）→ Redis 队列 → worker → Issue → Copilot 改代码 → Draft PR
```

**当前处于「纯路由观察期」**：只验证路由准不准，不发 Issue、不改代码、不开 PR。
`.env` 里三个门禁都关着：`WORKER_ISSUE_PUBLICATION_ENABLED=false`、
`WORKER_CODE_MODE=disabled`、`WORKER_CODE_AUTO_APPROVAL_ENABLED=false`（三个都要改 worker 才起得来）。
任务走完路由后被 mock worker 安全拦截，状态 FAILED + "mock worker failed safely;
no external systems were called" —— **这是故意的，不是出错**。

## 待办（最重要的事）

1. **等用户审批 PR #67**：https://github.com/wzf12400/ai-pr-sandbox/pull/67
   「Jira 线 AI 画像分类路由 + 路由结论缓存」——PR #66 合并时这两个提交还没推上去，
   单独补开的 PR（`src/repo_profiler.py` + Jira 三级路由链 + 路由结论缓存 + 本文档），
   Python 437 个测试全绿。
2. 用户批完后候选下一步：加固 Jira 会话自动续期（2026-08-19 手动救过一次，见排障备忘）。

## PR 状态

- PR #64（Jira 接入）、#65（监控稳定性修复）、**#66（路由方案前三提交）**：**已合并**
- PR #66 只带走了前 3 个提交（锚点路由器 + 加权 + 通用词过滤），AI 画像分类和
  路由缓存 2 个提交当时在本地没推上去，已补开 **PR #67 待审批**（分支 feat/jira-ai-routing）

## 路由方案架构（核心，勿推翻重来）

用户明确否定关键词映射（Jira 24 中 1、日志 0% 命中），拍板「代码即真相」零配置方案：

### 日志线：锚点代码搜索（`src/repo_anchor_router.py`）

- 从脱敏聚类提取锚点：接口路径段 / 驼峰标识符 / 栈帧类名 / 服务名
  （GENERIC_SEGMENTS / GENERIC_CLASSES 过滤通用词）
- GitHub code search API（`{anchor} org:{org}`）在授权组织内搜代码
- **命中按文件类型加权**：实现代码(Controller/Service/Mapper/Model 等) 3 分、
  普通源码 2 分、配置(yml/pom/Config 类) 1 分、文档 0.5 分
- **采纳门槛**：总分 ≥5 且实现文件 ≥2，唯一领先才路由；平票/弱证据放弃（宁缺毋滥）
- 缓存 7 天：`.issue-entry-state/log-routing-cache.json`（v2 加权格式）
- 实测 8 条真实聚类：5 正确路由、3 合理放弃（属主未授权）、0 错判

### Jira 线：三级路由（`jira_connector.route_issue_with_fallbacks`）

```
① 确定性（组件/标签/关键词，单仓绑定）→ RESOLVED 置信度 100
② 锚点代码搜索（需求带技术线索时复用日志线路由器）→ 置信度 95
③ AI 画像分类（repo_profiler.classify_issue）→ 置信度 70-99
都不行 → NEEDS_CONTEXT（待人工）
```

- 兜底两级全 fail-open，异常绝不炸主流程；置信度 <100 不触发 auto_dispatch
- **路由结论缓存**：`.issue-entry-state/jira-routing-cache.json`，按 issue key +
  标题/描述 SHA 指纹；内容变更或项目换绑仓库自动作废（防 AI 波动双派）
- 实测 20 条 AI 项目真实需求：**19/20 = 95% 匹配率**（18 条 AI 分类、1 条关键词、
  1 条待人工）

### 仓库画像（`src/repo_profiler.py`）

- 抓 README + 目录结构 + 依赖清单 → 公司 AI 网关生成中文画像
  （summary/keywords/modules）
- 存 `.issue-entry-state/repo-profiles.json`（本地，不进 git），7 天自动刷新
- 4 个已授权仓库画像已生成，业务定位准确

## Java 控制面路由（已随 PR #66 提交）

- `CreateTaskRequest` 第 5 字段 `repositoryHint`（`org/repo` 格式校验）
- `TaskService.createTask` 三分支：jiraIssue 绑定 > repositoryHint（须
  `CatalogRepositoryMatcher.isAuthorized()` 通过）> 关键词兜底
- log_monitor 派单时带 hint（`src/log_monitor_api.py` 的 `_dispatch_incident_task`）

## 关键配置与密钥（值都在 .env）

- `GITHUB_ROUTING_TOKEN` — Beckham505 账号的 PAT，可见 4 个 KikaTech 私有仓
  （backend-aicompanion / frontend-aicompanion / kika-global-studio /
  kika-global-studio-front，push+triage+pull）。**用户说过"先不要动"这些仓**
- `AI_BASE_URL=https://kika-airouter-test.kika-backend.com/api/v1`（公司 AI 网关，
  test 环境）/ `AI_API_KEY` / `AI_MODEL=ailemac/gpt-5-mini` /
  `AI_SAFETY_IDENTIFIER`（**请求体必带 safety_identifier 字段，否则 400**）
- `JIRA_BASE_URL=https://jira.xinmei365.com` / `JIRA_SESSION_COOKIE` /
  `JIRA_USERNAME=wzf` / `JIRA_PASSWORD=123456`
- 授权目录：`control-plane/config/repository-search-scope.json` +
  `application.yml` 的 `app.repository-catalog`（4 个 KikaTech 仓，默认分支：
  前三个 master、studio-front 是 main）
- Jira 项目配置：`control-plane/config/jira-projects.json` —— AI 项目绑
  aicompanion 前后端双仓 + `ai_routing: true`；其余 6 个项目（FF/AE/AF/CEL/MOD/KEYB）
  仍单绑沙箱仓库占位（真实仓库未授权）

## 运行中的服务与重启命令

```bash
cd /Users/zf/Desktop/ai-pr-sandbox

# 控制面 8080（必须 JAVA_HOME 21）
set -a && . ./.env && set +a && cd control-plane && \
  JAVA_HOME=/opt/homebrew/opt/openjdk@21 nohup mvn spring-boot:run > /tmp/control-plane.log 2>&1 &

# worker（纯路由期 mock 模式）
set -a && . ./.env && set +a && \
  GITHUB_ISSUE_TOKEN=$(gh auth token) nohup .venv/bin/python3 -m src.mock_task_worker \
  --wait-timeout 5 > /tmp/mock-worker.log 2>&1 &

# 日志监控 8099 / Jira 监控 8098（都必须先 source .env，否则没有路由 token 和 AI 配置）
pkill -f "src.jira_monitor_api"; sleep 2
set -a && . ./.env && set +a && \
  nohup .venv/bin/python3 -m src.jira_monitor_api > /tmp/jira-monitor-8098.log 2>&1 &
set -a && . ./.env && set +a && \
  nohup .venv/bin/python3 -m src.log_monitor_api > /tmp/log-monitor-8099.log 2>&1 &

# 前端 console/（Kimi Work 管生命周期，别自己留 dev server）
```

注意：Python 一律用 `.venv/bin/python3`（worker 需要 redis 依赖），别用系统 python。

## 测试基线

```bash
.venv/bin/python3 -m unittest discover -s tests      # Python 全绿（锚点 21 + Jira 35 等）
cd control-plane && JAVA_HOME=/opt/homebrew/opt/openjdk@21 mvn test   # Java 38 全绿
```

## Jira 认证备忘（重要，反复踩）

- 平台在钉钉 SSO 网关后面，**Basic Auth 和 curl 自建会话都被网关废掉**；
  唯一可行：浏览器登录 → CDP 提取 Cookie → 写回 .env
- `src/jira_session_refresh.py` 自动续期：WebBridge（127.0.0.1:10086）驱动 Chrome。
  jira_monitor 自动扫描遇 JiraAuthError 自动触发（冷却 10 分钟）
- **2026-08-19 续期翻车过一次**：login.jsp 被 SSO 重定向到钉钉授权页，
  原生表单选择器找不到。手动救援流程：先 `_click_dingtalk_login()` 点"立即登录"
  → 跳回 login.jsp → 再填原生表单（#login-form-username/password/submit）→
  跳到 Dashboard → `_extract_cookies()` → `_verify_cookie` → `_persist_env`。
  加固方向：把"SSO 点一下再回原生表单"编排进自动续期

## 已知待确认/未解

- backend-wallpaper 服务归属：锚点路由判给 kika-global-studio（配置+业务代码命中），
  但 backend-aicompanion 里有 `com.kikatech.aiapp.wallpaper` 包，真实归属待用户确认
- kika-global-studio 对应哪个 Jira 项目未知；IRL/IK/AT 等项目仓库未授权给 Beckham505
- 接口路径动态段被脱敏吃掉（`[REDACTED:path_segment]`），损失部分锚点；
  可考虑把路径段从用户数据里豁免（已向用户提议过，未拍板）
- 日志派单阈值 `minGroupEvents=8`（用户以为设的 5，实际配置是 8）
- 前端待优化（用户提过，未做）："mock worker failed safely" 显示成中文
  「已路由 · 安全拦截（测试模式）」；详情页加需求简介；去掉"严重程度裁决依据"展示

## 用户偏好（务必遵守）

- 不懂技术术语，回复用中文大白话，给链接，别堆术语
- 要全自动，"只有确实需要才要人参与"
- 界面中文简洁；测试 PR 让他批，别自己合并
- 密钥/账号写 .env 或配置文件，**绝不写进前端代码**
- git 提交用户是 wzf12400；真实公司仓库操作前必须确认授权范围

## 历史坑（排障备忘）

1. worker "unclassified high-entropy data"：生成 Issue 正文里的 64 位 hex 触发高熵拦截，已剥离
2. 重试重复建 Issue 会 attach 冲突；claim 响应带 issueNumber 复用
3. `save()` 预分配 UUID 实体走 merge 返回旧快照，create 后要 findJobForUpdate 重读
4. 前端轮询别 setLoading(true) 卸载内容（闪烁）
5. 改 `control-plane/config/*.json` 策略文件要重算 SHA256 同步 .env（fail-closed）
6. Jira JQL 里别过滤中文 issue 类型名（400 错误），用客户端 `issue_types` 过滤
7. 锚点路由缓存有两个版本：v1 计次、v2 加权；旧格式自动重搜
