import { useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  KanbanSquare,
  Maximize2,
  Play,
  RefreshCw,
  ShieldAlert,
  Zap,
} from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import { formatTime, timeAgo } from "@/lib/status";
import { useJiraMonitor } from "@/hooks/useJiraMonitor";
import type {
  JiraMonitorStatus,
  JiraProjectView,
  JiraScannedIssue,
} from "@/types/jira";

const DECISION_META: Record<string, { label: string; cls: string }> = {
  RESOLVED: { label: "已匹配仓库", cls: "bg-emerald-50 text-emerald-700" },
  NEEDS_CONTEXT: { label: "待人工分诊", cls: "bg-amber-50 text-amber-700" },
  BLOCKED_SENSITIVE: { label: "敏感拦截", cls: "bg-red-50 text-red-700" },
};

export function JiraMonitor() {
  const { status, reachable, scan, dispatch, saveRules } = useJiraMonitor();
  const [expanded, setExpanded] = useState(
    () => window.location.hash === "#jira-monitor"
  );
  const [scanning, setScanning] = useState(false);
  const [scanError, setScanError] = useState<string | null>(null);

  async function runScan(e?: React.MouseEvent) {
    e?.stopPropagation();
    setScanning(true);
    setScanError(null);
    try {
      await scan();
    } catch (err) {
      setScanError(err instanceof Error ? err.message : "扫描失败");
    } finally {
      setScanning(false);
    }
  }

  const live = status?.status === "ok";
  const counts = status?.counts ?? {};
  const issueTotal = status?.issues?.length ?? 0;
  const enabledProjects = (status?.projects ?? []).filter((p) => p.enabled).length;

  return (
    <>
      {/* 悬浮小窗：位于日志监控卡片下方 */}
      {!expanded && (
        <div className="pointer-events-none w-full">
          <div
            onClick={() => setExpanded(true)}
            className="pointer-events-auto cursor-pointer select-none rounded-2xl border border-border/70 bg-white/85 shadow-lg shadow-zinc-900/5 backdrop-blur-md transition-shadow hover:shadow-xl hover:shadow-zinc-900/10"
          >
            <div className="flex items-center gap-1.5 px-3 pb-1 pt-2.5">
              <span
                className={cn(
                  "h-2 w-2 rounded-full",
                  live ? "bg-emerald-500" : "bg-zinc-300"
                )}
              />
              <span className="text-[11px] font-semibold tracking-tight">
                Jira 监控
              </span>
              <button
                onClick={runScan}
                title="立即扫描"
                className="ml-auto flex h-5 w-5 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
              >
                <RefreshCw className={cn("h-3 w-3", scanning && "animate-spin")} />
              </button>
              <Maximize2 className="h-3 w-3 text-muted-foreground/60" />
            </div>

            <div className="px-3 pb-2.5 pt-1">
              {reachable === false && (
                <p className="py-1 text-[11px] leading-snug text-muted-foreground">
                  监控服务未运行
                  <span className="mt-0.5 block font-mono text-[10px] text-muted-foreground/60">
                    python3 -m src.jira_monitor_api
                  </span>
                </p>
              )}
              {reachable !== false && !live && (
                <p className="py-1 text-[11px] leading-snug text-muted-foreground">
                  {status?.detail ?? "Jira 连接器未就绪"}
                </p>
              )}
              {reachable === null && !status && (
                <p className="py-1 text-[11px] text-muted-foreground">连接中…</p>
              )}
              {live && (
                <>
                  <div className="flex items-baseline gap-1">
                    <span className="text-xl font-semibold tabular-nums leading-none">
                      {issueTotal}
                    </span>
                    <span className="text-[10px] text-muted-foreground">
                      已扫描 / {enabledProjects} 个接线项目
                    </span>
                  </div>
                  <div className="mt-1.5 flex gap-2 text-[10px] text-muted-foreground">
                    <span className="flex items-center gap-0.5">
                      <Zap className="h-2.5 w-2.5 text-emerald-500" />
                      可自动 {counts["RESOLVED"] ?? 0}
                    </span>
                    <span className="flex items-center gap-0.5">
                      <ShieldAlert className="h-2.5 w-2.5 text-amber-500" />
                      待分诊 {counts["NEEDS_CONTEXT"] ?? 0}
                    </span>
                    {status?.autoScan?.lastRunAt && (
                      <span className="ml-auto">
                        {timeAgo(status.autoScan.lastRunAt)}自动扫
                      </span>
                    )}
                  </div>
                  {status?.autoScan?.lastError && (
                    <p className="mt-1 text-[10px] text-red-600">
                      自动扫描异常：{status.autoScan.lastError}
                    </p>
                  )}
                  {scanError && (
                    <p className="mt-1 text-[10px] text-red-600">{scanError}</p>
                  )}
                </>
              )}
            </div>
          </div>
        </div>
      )}

      {/* 展开大窗 */}
      <Dialog open={expanded} onOpenChange={setExpanded}>
        <DialogContent className="flex h-[85vh] w-[calc(100vw-2rem)] max-w-4xl flex-col gap-0 overflow-hidden p-0">
          <DialogHeader className="shrink-0 border-b border-border px-5 py-3.5">
            <DialogTitle className="flex items-center gap-2 text-sm">
              <KanbanSquare className="h-4 w-4 shrink-0 text-muted-foreground" />
              Jira 需求监控
              <span className="font-mono text-[11px] font-normal text-muted-foreground">
                jira.xinmei365.com
              </span>
              <button
                onClick={runScan}
                title="立即扫描"
                className="ml-auto mr-7 flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
              >
                <RefreshCw className={cn("h-3.5 w-3.5", scanning && "animate-spin")} />
              </button>
            </DialogTitle>
          </DialogHeader>
          <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden">
            <div className="p-5">
              {reachable === false && (
                <EmptyHint
                  title="监控服务未运行"
                  hint="python3 -m src.jira_monitor_api"
                />
              )}
              {reachable !== false && !live && (
                <EmptyHint title={status?.detail ?? "Jira 连接器未就绪"} />
              )}
              {live && status && (
                <LiveDetail
                  status={status}
                  scanning={scanning}
                  dispatch={dispatch}
                  saveRules={saveRules}
                />
              )}
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

function EmptyHint({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="rounded-lg border border-dashed border-border p-8 text-center">
      <p className="text-xs text-muted-foreground">{title}</p>
      {hint && (
        <p className="mt-2 break-all font-mono text-[11px] leading-relaxed text-muted-foreground/70">
          {hint}
        </p>
      )}
    </div>
  );
}

type DispatchFn = ReturnType<typeof useJiraMonitor>["dispatch"];
type SaveRulesFn = ReturnType<typeof useJiraMonitor>["saveRules"];

function LiveDetail({
  status,
  scanning,
  dispatch,
  saveRules,
}: {
  status: JiraMonitorStatus;
  scanning: boolean;
  dispatch: DispatchFn;
  saveRules: SaveRulesFn;
}) {
  const [projectFilter, setProjectFilter] = useState<string>("");
  const counts = status.counts ?? {};
  const allIssues = status.issues ?? [];
  const issues = projectFilter
    ? allIssues.filter((i) => i.project === projectFilter)
    : allIssues;
  const byProject = new Map<string, JiraScannedIssue[]>();
  for (const issue of issues) {
    const list = byProject.get(issue.project) ?? [];
    list.push(issue);
    byProject.set(issue.project, list);
  }
  const projectKeys = [...new Set(allIssues.map((i) => i.project))].sort();

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <DetailStat label="已扫描需求" value={issues.length} />
        <DetailStat label="已匹配仓库" value={counts["RESOLVED"] ?? 0} />
        <DetailStat label="待人工分诊" value={counts["NEEDS_CONTEXT"] ?? 0} />
        <DetailStat label="敏感拦截" value={counts["BLOCKED_SENSITIVE"] ?? 0} />
      </div>

      {status.lastScan && (
        <p className="text-[11px] text-muted-foreground">
          本次扫描新增 {status.lastScan.newIssues} 条 ·{" "}
          {scanning ? "扫描中…" : `服务时间 ${formatTime(status.servedAt)}`}
        </p>
      )}

      <ProjectRules
        projects={status.projects ?? []}
        watermarks={status.watermarks ?? {}}
        saveRules={saveRules}
      />

      <div className="flex items-center gap-2">
        <label className="text-[11px] text-muted-foreground">项目筛选</label>
        <select
          value={projectFilter}
          onChange={(e) => setProjectFilter(e.target.value)}
          className="h-7 rounded-md border border-input bg-white px-2 text-[12px] outline-none focus:border-zinc-400"
        >
          <option value="">全部项目（{allIssues.length}）</option>
          {projectKeys.map((key) => (
            <option key={key} value={key}>
              {key}（{allIssues.filter((i) => i.project === key).length}）
            </option>
          ))}
        </select>
        {projectFilter && (
          <button
            onClick={() => setProjectFilter("")}
            className="text-[11px] text-muted-foreground hover:text-foreground"
          >
            清除
          </button>
        )}
      </div>

      {[...byProject.entries()].map(([project, list]) => {
        const projectRepos =
          (status.projects ?? []).find((p) => p.key === project)?.repositories ?? [];
        return (
          <div key={project}>
            <h3 className="mb-2 flex items-center gap-2 text-xs font-medium uppercase tracking-wider text-muted-foreground">
              <span className="rounded bg-sky-50 px-1.5 py-0.5 font-mono text-[10px] text-sky-700">
                {project}
              </span>
              {list.length} 条
            </h3>
            <div className="space-y-1.5">
              {list.map((issue) => (
                <IssueRow
                  key={issue.issue}
                  issue={issue}
                  dispatch={dispatch}
                  fallbackRepo={projectRepos[0] ?? ""}
                />
              ))}
            </div>
          </div>
        );
      })}
      {issues.length === 0 && (
        <p className="text-xs text-muted-foreground">
          暂无扫描记录，点右上角「立即扫描」跑一轮影子扫描。
        </p>
      )}
    </div>
  );
}

function IssueRow({
  issue,
  dispatch,
  fallbackRepo,
}: {
  issue: JiraScannedIssue;
  dispatch: DispatchFn;
  fallbackRepo: string;
}) {
  const [open, setOpen] = useState(false);
  const [dispatching, setDispatching] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const meta = DECISION_META[issue.decision] ?? {
    label: issue.decision,
    cls: "bg-secondary text-muted-foreground",
  };
  const dispatched = issue.dispatch?.result === "created";
  const needsHuman = issue.decision === "NEEDS_CONTEXT";
  const dispatchable =
    !dispatched && (issue.decision === "RESOLVED" || (needsHuman && !!fallbackRepo));

  async function runDispatch(e: React.MouseEvent) {
    e.stopPropagation();
    setDispatching(true);
    setResult(null);
    try {
      const outcome = await dispatch(
        issue.issue,
        issue.decision === "RESOLVED" ? undefined : fallbackRepo
      );
      setResult(
        outcome.result === "created"
          ? `已建任务 ${outcome.taskId?.slice(0, 8) ?? ""}`
          : outcome.detail ?? outcome.result
      );
    } catch (err) {
      setResult(err instanceof Error ? err.message : "派发失败");
    } finally {
      setDispatching(false);
    }
  }

  return (
    <div className="overflow-hidden rounded-lg border border-border bg-white">
      <div
        role="button"
        tabIndex={0}
        onClick={() => setOpen(!open)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setOpen(!open);
          }
        }}
        className="flex w-full cursor-pointer items-center gap-3 px-3 py-2.5 text-left transition-colors hover:bg-accent/50"
      >
        {open ? (
          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        )}
        <span className="shrink-0 font-mono text-[11px] text-muted-foreground">
          {issue.issue}
        </span>
        <span className="min-w-0 flex-1 truncate text-[13px]">{issue.summary}</span>
        <span className={cn("shrink-0 rounded px-1.5 py-0.5 text-[10px]", meta.cls)}>
          {meta.label}
        </span>
        {issue.repository && (
          <span className="hidden max-w-44 truncate font-mono text-[11px] text-muted-foreground md:inline">
            → {issue.repository}
          </span>
        )}
        {dispatched ? (
          <span className="shrink-0 rounded bg-emerald-50 px-1.5 py-0.5 text-[10px] text-emerald-700">
            已建任务
          </span>
        ) : (
          dispatchable && (
            <button
              onClick={runDispatch}
              disabled={dispatching}
              title={
                needsHuman
                  ? `规则未匹配到仓库，由你人工指派到 ${fallbackRepo}`
                  : "创建任务并进入 Issue → Copilot → Draft PR 流水线"
              }
              className={cn(
                "flex shrink-0 items-center gap-1 rounded-md px-2 py-1 text-[10px] disabled:opacity-40",
                needsHuman
                  ? "border border-amber-200 bg-amber-50 text-amber-700"
                  : "bg-primary text-primary-foreground"
              )}
            >
              <Play className="h-2.5 w-2.5" />
              {dispatching ? "派发中…" : needsHuman ? "人工建任务" : "建任务"}
            </button>
          )
        )}
        <span className="shrink-0 text-[11px] text-muted-foreground">
          {timeAgo(issue.ts)}
        </span>
      </div>
      {open && (
        <div className="border-t border-border bg-secondary/30 px-4 py-3 text-[12px]">
          <div>
            <span className="text-muted-foreground">需求简介</span>
            <p className="mt-1 whitespace-pre-wrap break-all leading-relaxed">
              {issue.excerpt?.trim() || "（该需求在 Jira 中没有填写描述）"}
            </p>
          </div>
          {issue.url && (
            <a
              href={issue.url}
              target="_blank"
              rel="noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="mt-2 inline-block text-[11px] text-sky-700 hover:underline"
            >
              在 Jira 中查看原文 ↗
            </a>
          )}
          {issue.dispatch && issue.dispatch.result !== "shadow" && (
            <p className="mt-2 text-[11px] text-muted-foreground">
              派发：{issue.dispatch.result}
              {issue.dispatch.detail ? `（${issue.dispatch.detail}）` : ""}
            </p>
          )}
          {result && <p className="mt-2 text-[11px] text-sky-700">{result}</p>}
        </div>
      )}
    </div>
  );
}

function ProjectRules({
  projects,
  watermarks,
  saveRules,
}: {
  projects: JiraProjectView[];
  watermarks: Record<string, string>;
  saveRules: SaveRulesFn;
}) {
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function toggle(project: string, patch: { enabled?: boolean; autoDispatch?: boolean }) {
    setSaving(project);
    setError(null);
    try {
      await saveRules(project, patch);
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(null);
    }
  }

  if (projects.length === 0) return null;
  return (
    <div className="rounded-lg border border-border bg-white p-4">
      <div className="flex items-center gap-2">
        <Zap className="h-4 w-4 text-amber-500" />
        <h3 className="text-[13px] font-semibold">接线项目</h3>
      </div>
      <p className="mt-1.5 text-[12px] leading-relaxed text-muted-foreground">
        「自动派发」开启后，命中路由且无歧义的需求会自动创建任务，进入 Issue →
        Copilot → Draft PR 流水线；关闭则只在列表里标注，由你逐条点「建任务」。
      </p>
      <div className="mt-3 space-y-2">
        {projects.map((p) => (
          <div
            key={p.key}
            className="flex flex-wrap items-center gap-2 rounded-md border border-border/70 px-3 py-2 text-[12px]"
          >
            <span className="rounded bg-sky-50 px-1.5 py-0.5 font-mono text-[10px] text-sky-700">
              {p.key}
            </span>
            <span className="text-muted-foreground">
              {(p.issueTypes ?? []).join(" / ") || "全部类型"}
            </span>
            <span className="font-mono text-[11px] text-muted-foreground/70">
              → {p.repositories.join(", ")}
            </span>
            {watermarks[p.key] && (
              <span className="text-[10px] text-muted-foreground/60">
                水位 {watermarks[p.key]}
              </span>
            )}
            <span className="ml-auto flex gap-1.5">
              <button
                onClick={() => toggle(p.key, { enabled: !p.enabled })}
                disabled={saving === p.key}
                className={cn(
                  "rounded-md px-2 py-1 text-[11px] disabled:opacity-40",
                  p.enabled
                    ? "bg-emerald-50 text-emerald-700"
                    : "bg-secondary text-muted-foreground"
                )}
              >
                {p.enabled ? "扫描中" : "已停用"}
              </button>
              <button
                onClick={() => toggle(p.key, { autoDispatch: !p.autoDispatch })}
                disabled={saving === p.key}
                className={cn(
                  "rounded-md px-2 py-1 text-[11px] disabled:opacity-40",
                  p.autoDispatch
                    ? "bg-amber-50 text-amber-700"
                    : "bg-secondary text-muted-foreground"
                )}
              >
                {p.autoDispatch ? "自动派发开" : "自动派发关"}
              </button>
            </span>
          </div>
        ))}
      </div>
      {error && <p className="mt-2 text-[11px] text-red-600">{error}</p>}
    </div>
  );
}

function DetailStat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg border border-border bg-white p-3">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="mt-1 text-base font-semibold tabular-nums">{value}</div>
    </div>
  );
}
