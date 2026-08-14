import { useState } from "react";
import {
  Activity,
  ChevronDown,
  ChevronRight,
  Maximize2,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
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
import { useLogMonitor } from "@/hooks/useLogMonitor";
import type { IncidentView, LogMonitorScan, NamedCount } from "@/types/logmonitor";

export function LogMonitor() {
  const { scan, reachable, reload } = useLogMonitor();
  const [expanded, setExpanded] = useState(
    () => window.location.hash === "#log-monitor"
  );
  const [refreshing, setRefreshing] = useState(false);

  async function refresh(e?: React.MouseEvent) {
    e?.stopPropagation();
    setRefreshing(true);
    await reload(true);
    setRefreshing(false);
  }

  const live = scan?.status === "ok";

  return (
    <>
      {/* 苹果风格悬浮小窗（展开大窗时隐藏，避免重叠） */}
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
              日志监控
            </span>
            <button
              onClick={refresh}
              title="重新扫描"
              className="ml-auto flex h-5 w-5 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
            >
              <RefreshCw
                className={cn("h-3 w-3", refreshing && "animate-spin")}
              />
            </button>
            <Maximize2 className="h-3 w-3 text-muted-foreground/60" />
          </div>

          <div className="px-3 pb-2.5 pt-1">
            {reachable === false && (
              <p className="py-1 text-[11px] leading-snug text-muted-foreground">
                监控服务未运行
                <span className="mt-0.5 block font-mono text-[10px] text-muted-foreground/60">
                  python3 -m src.log_monitor_api
                </span>
              </p>
            )}
            {reachable !== false && scan && scan.status !== "ok" && (
              <p className="py-1 text-[11px] leading-snug text-muted-foreground">
                {scan.detail ?? "日志平台未就绪"}
              </p>
            )}
            {reachable === null && !scan && (
              <p className="py-1 text-[11px] text-muted-foreground">连接中…</p>
            )}
            {live && (
              <>
                <div className="flex items-baseline gap-1">
                  <span className="text-xl font-semibold tabular-nums leading-none">
                    {scan.errorEvents ?? 0}
                  </span>
                  <span className="text-[10px] text-muted-foreground">
                    错误事件 / {scan.projectsScanned ?? 0} 个项目
                  </span>
                </div>
                <div className="mt-1.5 flex gap-2 text-[10px] text-muted-foreground">
                  <span className="flex items-center gap-0.5">
                    <Activity className="h-2.5 w-2.5 text-orange-500" />
                    {scan.incidentGroups ?? 0} 组故障
                  </span>
                  <span className="flex items-center gap-0.5">
                    {(scan.blockedEvents ?? 0) > 0 ? (
                      <ShieldAlert className="h-2.5 w-2.5 text-amber-500" />
                    ) : (
                      <ShieldCheck className="h-2.5 w-2.5 text-emerald-500" />
                    )}
                    拦截 {scan.blockedEvents ?? 0}
                  </span>
                  {scan.scannedAt && (
                    <span className="ml-auto">{timeAgo(scan.scannedAt)}</span>
                  )}
                </div>
                {(scan.namespaces ?? []).length > 0 && (
                  <div className="mt-2">
                    <CountBars items={(scan.namespaces ?? []).slice(0, 3)} compact />
                  </div>
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
              <Activity className="h-4 w-4 shrink-0 text-muted-foreground" />
              日志平台监控
              {scan?.indexPattern && (
                <span className="truncate font-mono text-[11px] font-normal text-muted-foreground">
                  {scan.indexPattern}
                </span>
              )}
              <button
                onClick={refresh}
                title="重新扫描"
                className="ml-auto mr-7 flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
              >
                <RefreshCw
                  className={cn("h-3.5 w-3.5", refreshing && "animate-spin")}
                />
              </button>
            </DialogTitle>
          </DialogHeader>
          <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden">
            <div className="p-5">
              {reachable === false && (
                <EmptyHint
                  title="监控服务未运行"
                  hint="python3 -m src.log_monitor_api"
                />
              )}
              {reachable !== false && scan && scan.status !== "ok" && (
                <EmptyHint
                  title={scan.detail ?? "日志平台未就绪"}
                  hint={scan.configure}
                />
              )}
              {live && <LiveDetail scan={scan} onRulesSaved={async () => { await reload(); }} />}
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

function LiveDetail({
  scan,
  onRulesSaved,
}: {
  scan: LogMonitorScan;
  onRulesSaved: () => Promise<void>;
}) {
  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <DetailStat label="扫描项目（命名空间）" value={scan.projectsScanned ?? 0} />
        <DetailStat label="错误事件（已脱敏）" value={scan.errorEvents ?? 0} />
        <DetailStat label="故障分组" value={scan.incidentGroups ?? 0} />
        <DetailStat label="脱敏拦截" value={scan.blockedEvents ?? 0} />
      </div>

      <div className="grid grid-cols-1 gap-2 text-[11px] text-muted-foreground sm:grid-cols-3">
        <div className="truncate rounded-md border border-border bg-white px-2.5 py-1.5">
          索引 <span className="font-mono">{scan.indexPattern}</span>
        </div>
        <div className="truncate rounded-md border border-border bg-white px-2.5 py-1.5">
          窗口 {formatTime(scan.window?.from)} ~ {formatTime(scan.window?.to)}
        </div>
        <div className="truncate rounded-md border border-border bg-white px-2.5 py-1.5">
          扫描于 {formatTime(scan.scannedAt)} · 拉取 {scan.fetchSize} 条 · 过滤非
          ERROR {scan.skippedNonError ?? 0} 条
        </div>
      </div>

      <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
        <div className="min-w-0">
          <h3 className="mb-2 text-xs font-medium uppercase tracking-wider text-muted-foreground">
            项目错误分布
          </h3>
          {(scan.namespaces ?? []).length === 0 ? (
            <p className="text-xs text-muted-foreground">暂无数据</p>
          ) : (
            <CountBars items={scan.namespaces ?? []} />
          )}
        </div>
        <div className="min-w-0">
          <h3 className="mb-2 text-xs font-medium uppercase tracking-wider text-muted-foreground">
            服务错误分布
          </h3>
          {(scan.services ?? []).length === 0 ? (
            <p className="text-xs text-muted-foreground">暂无数据</p>
          ) : (
            <CountBars items={scan.services ?? []} />
          )}
        </div>
      </div>

      <AutomationPanel scan={scan} onSaved={onRulesSaved} />

      <div>
        <h3 className="mb-2 text-xs font-medium uppercase tracking-wider text-muted-foreground">
          故障分组明细
        </h3>
        {(scan.incidents ?? []).length === 0 ? (
          <p className="text-xs text-muted-foreground">
            本批次未发现可分组的错误事件
          </p>
        ) : (
          <div className="space-y-1.5">
            {(scan.incidents ?? []).map((inc) => (
              <IncidentRow key={inc.incidentRef} incident={inc} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function IncidentRow({ incident }: { incident: IncidentView }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="overflow-hidden rounded-lg border border-border bg-white">
      <button
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-3 px-3 py-2.5 text-left transition-colors hover:bg-accent/50"
      >
        {open ? (
          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        )}
        <span className="min-w-0 flex-1 truncate text-[13px]">
          {incident.summary || incident.incidentRef}
        </span>
        <span className="hidden max-w-36 truncate font-mono text-[11px] text-muted-foreground md:inline">
          {incident.services.join(", ") || "—"}
        </span>
        <span className="shrink-0 rounded bg-secondary px-1.5 py-0.5 text-[10px] tabular-nums text-muted-foreground">
          {incident.eventCount} 条
        </span>
        <span className="shrink-0 text-[11px] text-muted-foreground">
          {timeAgo(incident.lastSeenAt)}
        </span>
      </button>
      {open && (
        <div className="border-t border-border bg-secondary/30 px-4 py-3">
          <div className="grid grid-cols-2 gap-x-6 gap-y-1.5 text-[12px] md:grid-cols-4">
            <div>
              <span className="text-muted-foreground">首次出现</span>
              <div className="mt-0.5">{formatTime(incident.firstSeenAt)}</div>
            </div>
            <div>
              <span className="text-muted-foreground">最近出现</span>
              <div className="mt-0.5">{formatTime(incident.lastSeenAt)}</div>
            </div>
            <div>
              <span className="text-muted-foreground">分组策略</span>
              <div className="mt-0.5 font-mono text-[11px]">{incident.strategy}</div>
            </div>
            <div>
              <span className="text-muted-foreground">影响用户数</span>
              <div className="mt-0.5">{incident.affectedUserCount ?? "—"}</div>
            </div>
          </div>
          {incident.affectedEndpoints.length > 0 && (
            <div className="mt-3">
              <span className="text-[12px] text-muted-foreground">影响接口</span>
              <div className="mt-1 flex flex-wrap gap-1.5">
                {incident.affectedEndpoints.map((ep) => (
                  <span
                    key={ep}
                    className="rounded border border-border bg-white px-1.5 py-0.5 font-mono text-[11px]"
                  >
                    {ep}
                  </span>
                ))}
              </div>
            </div>
          )}
          <div className="mt-3">
            <span className="text-[12px] text-muted-foreground">
              事件明细（{incident.members.length} 条，已脱敏）
            </span>
            <div className="mt-1.5 max-h-56 space-y-1 overflow-y-auto">
              {incident.members.map((m, i) => (
                <div
                  key={i}
                  className="rounded border border-border/60 bg-white px-2.5 py-1.5"
                >
                  <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
                    <span
                      className={cn(
                        "rounded px-1 py-px font-mono",
                        m.level === "ERROR"
                          ? "bg-red-50 text-red-700"
                          : "bg-secondary text-muted-foreground"
                      )}
                    >
                      {m.level || "—"}
                    </span>
                    <span>{formatTime(m.timestamp)}</span>
                    {m.traceRef && (
                      <span className="font-mono">trace {m.traceRef}</span>
                    )}
                  </div>
                  <p className="mt-0.5 break-all text-[12px] leading-relaxed">
                    {m.summary || "—"}
                  </p>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function AutomationPanel({
  scan,
  onSaved,
}: {
  scan: LogMonitorScan;
  onSaved: () => Promise<void>;
}) {
  const auto = scan.automation;
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [threshold, setThreshold] = useState(
    String(auto?.rules.minGroupEvents ?? 10)
  );
  if (!auto) return null;

  async function save(nextEnabled?: boolean) {
    setSaving(true);
    setSaveError(null);
    try {
      const res = await fetch("/log-monitor/rules", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          enabled: nextEnabled ?? auto!.rules.enabled,
          minGroupEvents: Number(threshold) || auto!.rules.minGroupEvents,
        }),
      });
      if (!res.ok) {
        throw new Error(`保存失败（${res.status}）`);
      }
      // 保存成功后立即刷新，让启用/停用状态即时生效
      await onSaved();
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  const RESULT_LABEL: Record<string, string> = {
    created: "已创建任务",
    already_dispatched: "已派发过",
    over_budget: "超出单次上限",
    skipped: "已跳过",
    failed: "失败",
  };

  return (
    <div className="rounded-lg border border-border bg-white p-4">
      <div className="flex items-center gap-2">
        <Zap className="h-4 w-4 text-amber-500" />
        <h3 className="text-[13px] font-semibold">自动化规则</h3>
        <span
          className={cn(
            "rounded-full px-2 py-0.5 text-[10px]",
            auto.rules.enabled
              ? "bg-emerald-50 text-emerald-700"
              : "bg-secondary text-muted-foreground"
          )}
        >
          {auto.rules.enabled ? "已启用" : "已停用"}
        </span>
      </div>
      <p className="mt-1.5 text-[12px] leading-relaxed text-muted-foreground">
        当某个故障聚类的事件数达到阈值时，自动向控制面提交任务，触发 Issue
        生成 → AI 修改代码 → Draft PR 的门禁流程（写入门禁仍需策略审批）。
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-2 text-[12px]">
        <span className="text-muted-foreground">聚类事件数 ≥</span>
        <input
          type="number"
          min={1}
          max={10000}
          value={threshold}
          onChange={(e) => setThreshold(e.target.value)}
          className="h-7 w-20 rounded-md border border-input bg-white px-2 text-[12px] tabular-nums outline-none focus:border-zinc-400"
        />
        <span className="text-muted-foreground">条时自动建任务</span>
        <button
          onClick={() => save()}
          disabled={saving}
          className="rounded-md bg-primary px-2.5 py-1 text-[11px] text-primary-foreground disabled:opacity-40"
        >
          {saving ? "保存中…" : "保存"}
        </button>
        <button
          onClick={() => save(!auto.rules.enabled)}
          disabled={saving}
          className="rounded-md border border-border px-2.5 py-1 text-[11px] text-muted-foreground hover:bg-accent disabled:opacity-40"
        >
          {auto.rules.enabled ? "停用规则" : "启用规则"}
        </button>
        {saveError && (
          <span className="text-[11px] text-red-600">{saveError}</span>
        )}
        <span className="ml-auto text-[11px] text-muted-foreground">
          本次超阈值 {auto.overThreshold} 组 · 单次最多 {auto.rules.maxTasksPerScan} 个任务
        </span>
      </div>
      {auto.dispatched.length > 0 && (
        <div className="mt-3 space-y-1 border-t border-border pt-2.5">
          {auto.dispatched.map((d) => (
            <div
              key={d.incidentRef}
              className="flex items-center gap-2 text-[11px]"
            >
              <span
                className={cn(
                  "rounded px-1.5 py-0.5",
                  d.result === "created"
                    ? "bg-emerald-50 text-emerald-700"
                    : d.result === "failed"
                      ? "bg-red-50 text-red-700"
                      : "bg-secondary text-muted-foreground"
                )}
              >
                {RESULT_LABEL[d.result] ?? d.result}
              </span>
              <span className="font-mono text-muted-foreground">
                {d.incidentRef}
              </span>
              {d.matchedRepository && (
                <span className="font-mono text-muted-foreground/70">
                  → {d.matchedRepository}
                </span>
              )}
              {d.detail && (
                <span className="text-muted-foreground/70">{d.detail}</span>
              )}
            </div>
          ))}
        </div>
      )}
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

function CountBars({
  items,
  compact,
}: {
  items: NamedCount[];
  compact?: boolean;
}) {
  const max = Math.max(...items.map((i) => i.errors), 1);
  return (
    <div className="space-y-1.5">
      {items.map((item) => (
        <div key={item.name} className="flex items-center gap-2 text-xs">
          <span
            className={cn(
              "shrink-0 truncate font-mono text-[11px]",
              compact ? "w-20 text-[10px]" : "w-24"
            )}
          >
            {item.name}
          </span>
          <div
            className={cn(
              "min-w-0 flex-1 overflow-hidden rounded bg-secondary",
              compact ? "h-2.5" : "h-3.5"
            )}
          >
            <div
              className="h-full rounded bg-sky-500/70"
              style={{ width: `${Math.max((item.errors / max) * 100, 4)}%` }}
            />
          </div>
          <span className="w-8 shrink-0 text-right text-[11px] tabular-nums text-muted-foreground">
            {item.errors}
          </span>
        </div>
      ))}
    </div>
  );
}
