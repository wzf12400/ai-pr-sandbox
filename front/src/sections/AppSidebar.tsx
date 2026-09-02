import { useState } from "react";
import {
  Bot,
  ChevronRight,
  GitBranch,
  ListFilter,
  Settings,
  SquarePen,
  Trash2,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { STATUS_META, timeAgo } from "@/lib/status";
import type { Task } from "@/types/task";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
} from "@/components/ui/select";
import {
  repositoryDisplayName,
  type JiraConnectResult,
  type ProjectSettings,
  type SettingsSaveInput,
} from "@/lib/project-config";
import { SettingsPanel } from "@/sections/SettingsPanel";

type SourceFilter = "NATURAL_LANGUAGE" | "LOG" | "JIRA";
type ProgressFilter =
  | "ALL"
  | "ACTIVE"
  | "AWAITING_PR_REVIEW"
  | "NEEDS_CONTEXT"
  | "FAILED"
  | "COMPLETED";

const FILTER_TABS: { value: SourceFilter; label: string }[] = [
  { value: "NATURAL_LANGUAGE", label: "自然语言" },
  { value: "LOG", label: "日志" },
  { value: "JIRA", label: "Jira" },
];

const PROGRESS_FILTERS: {
  value: ProgressFilter;
  label: string;
  statuses: Task["status"][];
  dot: string;
}[] = [
  {
    value: "ALL",
    label: "全部",
    statuses: [],
    dot: "bg-zinc-400",
  },
  {
    value: "ACTIVE",
    label: "进行中",
    statuses: ["PENDING", "PROCESSING", "TESTING"],
    dot: "bg-sky-500",
  },
  {
    value: "AWAITING_PR_REVIEW",
    label: "待 PR",
    statuses: ["AWAITING_PR_REVIEW"],
    dot: "bg-amber-500",
  },
  {
    value: "NEEDS_CONTEXT",
    label: "需补充",
    statuses: ["NEEDS_CONTEXT"],
    dot: "bg-orange-500",
  },
  {
    value: "FAILED",
    label: "失败",
    statuses: ["FAILED"],
    dot: "bg-red-500",
  },
  {
    value: "COMPLETED",
    label: "已完成",
    statuses: ["COMPLETED"],
    dot: "bg-emerald-500",
  },
];

type Props = {
  tasks: Task[];
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onDelete: (id: string) => void;
  connected: boolean | null;
  settings: ProjectSettings;
  settingsLoading: boolean;
  settingsError: string;
  onRefreshSettings: () => Promise<void>;
  onSaveSettings: (settings: SettingsSaveInput) => Promise<ProjectSettings>;
  onConnectJira: (input: {
    baseUrl: string;
    username: string;
    password: string;
  }) => Promise<JiraConnectResult>;
  repositoryFilter: string;
  onRepositoryFilterChange: (repository: string) => void;
};

export function AppSidebar({
  tasks,
  selectedId,
  onSelect,
  onDelete,
  connected,
  settings,
  settingsLoading,
  settingsError,
  onRefreshSettings,
  onSaveSettings,
  onConnectJira,
  repositoryFilter,
  onRepositoryFilterChange,
}: Props) {
  const [filter, setFilter] = useState<SourceFilter>("NATURAL_LANGUAGE");
  const [progressFilter, setProgressFilter] =
    useState<ProgressFilter>("ALL");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const repositories = settings.repositories;
  const selectedRepository =
    repositoryFilter === "all"
      ? "全部仓库"
      : repositoryDisplayName(repositoryFilter);
  const repositoryTasks =
    repositoryFilter === "all"
      ? tasks
      : tasks.filter((task) => task.matchedRepository === repositoryFilter);
  const sourceTasks = repositoryTasks.filter(
    (task) => task.sourceType === filter
  );
  const selectedProgress =
    PROGRESS_FILTERS.find((item) => item.value === progressFilter) ??
    PROGRESS_FILTERS[0];
  const filtered =
    progressFilter === "ALL"
      ? sourceTasks
      : sourceTasks.filter((task) =>
          selectedProgress.statuses.includes(task.status)
        );
  const progressCounts = Object.fromEntries(
    PROGRESS_FILTERS.map((item) => [
      item.value,
      item.value === "ALL"
        ? sourceTasks.length
        : sourceTasks.filter((task) => item.statuses.includes(task.status))
            .length,
    ])
  ) as Record<ProgressFilter, number>;
  const counts: Record<SourceFilter, number> = {
    NATURAL_LANGUAGE: repositoryTasks.filter(
      (task) => task.sourceType === "NATURAL_LANGUAGE"
    ).length,
    LOG: repositoryTasks.filter((task) => task.sourceType === "LOG").length,
    JIRA: repositoryTasks.filter((task) => task.sourceType === "JIRA").length,
  };

  async function handleSaveSettings(next: SettingsSaveInput) {
    const updated = await onSaveSettings(next);
    if (
      repositoryFilter !== "all" &&
      !updated.repositories.some(
        (repository) => repository.repository === repositoryFilter
      )
    ) {
      onRepositoryFilterChange("all");
    }
    return updated;
  }
  return (
    <aside className="flex h-full w-full flex-col bg-sidebar-background">
      <div className="flex items-center gap-2 px-3 pb-1 pt-3">
        <div className="flex h-7 w-7 items-center justify-center rounded-md border border-border bg-white">
          <Bot className="h-4 w-4 text-foreground" />
        </div>
        <span className="text-[13px] font-semibold tracking-tight">AI Agent</span>
        <button
          onClick={() => onSelect(null)}
          title="新任务"
          aria-label="新任务"
          className="ml-auto flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-foreground"
        >
          <SquarePen className="h-4 w-4" />
        </button>
      </div>

      <div className="px-3 pb-1 pt-2">
        <Select
          value={repositoryFilter}
          onValueChange={onRepositoryFilterChange}
        >
          <SelectTrigger
            size="sm"
            aria-label="按仓库筛选任务"
            className="h-8 w-full min-w-0 overflow-hidden border-sky-100 bg-sky-50/70 px-2.5 text-xs shadow-none hover:bg-sky-50"
          >
            <div className="flex min-w-0 flex-1 items-center gap-2 overflow-hidden">
              <GitBranch className="h-3.5 w-3.5 shrink-0 text-sky-700" />
              <span className="min-w-0 truncate">{selectedRepository}</span>
            </div>
          </SelectTrigger>
          <SelectContent
            position="popper"
            align="start"
            className="w-[min(260px,calc(100vw-16px))]"
          >
            <SelectItem value="all">
              <span className="flex w-full items-center justify-between gap-5">
                <span>全部仓库</span>
                <span className="text-[10px] text-muted-foreground">
                  {tasks.length}
                </span>
              </span>
            </SelectItem>
            {repositories.map((repository) => {
              const count = tasks.filter(
                (task) => task.matchedRepository === repository.repository
              ).length;
              return (
                <SelectItem
                  key={repository.repository}
                  value={repository.repository}
                >
                  <span className="flex min-w-0 flex-col overflow-hidden">
                    <span className="truncate">
                      {repositoryDisplayName(repository.repository)}
                    </span>
                    <span className="truncate font-mono text-[9px] text-muted-foreground">
                      {repository.repository.split("/")[0]} · {count} 个任务
                    </span>
                  </span>
                </SelectItem>
              );
            })}
          </SelectContent>
        </Select>
      </div>

      <div className="px-3 pb-1 pt-2">
        <div className="flex rounded-lg bg-secondary/80 p-0.5">
          {FILTER_TABS.map((tab) => (
            <button
              key={tab.value}
              onClick={() => setFilter(tab.value)}
              className={cn(
                "flex h-6 flex-1 items-center justify-center gap-1 rounded-md text-[11px] transition-colors",
                filter === tab.value
                  ? "bg-white font-medium text-foreground shadow-sm"
                  : "text-muted-foreground hover:text-foreground"
              )}
            >
              {tab.label}
              {counts[tab.value] > 0 && (
                <span
                  className={cn(
                    "text-[10px] tabular-nums",
                    filter === tab.value
                      ? "text-muted-foreground"
                      : "text-muted-foreground/60"
                  )}
                >
                  {counts[tab.value]}
                </span>
              )}
            </button>
          ))}
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="px-2 py-2">
          <div className="flex items-center justify-between gap-2 px-2 pb-1.5">
            <span className="text-[11px] font-medium text-muted-foreground">
              任务线程
            </span>
            <Select
              value={progressFilter}
              onValueChange={(value) =>
                setProgressFilter(value as ProgressFilter)
              }
            >
              <SelectTrigger
                size="sm"
                aria-label="按任务进度筛选"
                className="h-6 w-auto min-w-0 gap-1.5 border-0 bg-transparent px-1.5 text-[10px] text-muted-foreground shadow-none hover:bg-sidebar-accent hover:text-foreground"
              >
                <ListFilter className="h-3 w-3" />
                <span>{selectedProgress.label}</span>
                <span className="tabular-nums text-muted-foreground/70">
                  {progressCounts[progressFilter]}
                </span>
              </SelectTrigger>
              <SelectContent position="popper" align="end" className="min-w-36">
                {PROGRESS_FILTERS.map((item) => (
                  <SelectItem key={item.value} value={item.value}>
                    <span className="flex w-full items-center gap-2">
                      <span
                        className={cn(
                          "h-1.5 w-1.5 shrink-0 rounded-full",
                          item.dot
                        )}
                      />
                      <span className="flex-1">{item.label}</span>
                      <span className="ml-3 tabular-nums text-[10px] text-muted-foreground">
                        {progressCounts[item.value]}
                      </span>
                    </span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          {filtered.length === 0 && (
            <p className="px-2 py-6 text-center text-[11px] text-muted-foreground">
              {tasks.length === 0 ? "暂无任务" : "当前仓库与分类下暂无任务"}
            </p>
          )}
          {filtered.map((t) => {
            const meta = STATUS_META[t.status];
            return (
              <div
                key={t.id}
                role="button"
                tabIndex={0}
                onClick={() => onSelect(t.id)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onSelect(t.id);
                  }
                }}
                className={cn(
                  "group mb-0.5 block w-full cursor-pointer rounded-md px-2 py-2 text-left transition-colors",
                  selectedId === t.id
                    ? "bg-sidebar-accent text-sidebar-accent-foreground"
                    : "text-sidebar-foreground hover:bg-sidebar-accent/70"
                )}
              >
                <div className="flex items-center gap-1.5">
                  <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", meta.dot)} />
                  <span className="min-w-0 flex-1 truncate text-[13px]">
                    {t.inputSummary}
                  </span>
                  <button
                    title="删除该任务线程"
                    aria-label={`删除任务：${t.inputSummary}`}
                    onClick={(e) => {
                      e.stopPropagation();
                      onDelete(t.id);
                    }}
                    className="flex h-5 w-5 shrink-0 items-center justify-center rounded text-muted-foreground/50 opacity-0 transition-opacity hover:bg-red-50 hover:text-red-600 group-hover:opacity-100"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
                <div className="mt-0.5 pl-3 text-[11px] text-muted-foreground">
                  {meta.label} · {timeAgo(t.createdAt)}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <div className="border-t border-border p-2">
        <button
          type="button"
          onClick={() => setSettingsOpen(true)}
          disabled={settingsLoading}
          className="group flex w-full items-center gap-2 rounded-md px-2 py-2 text-left transition-colors hover:bg-sidebar-accent"
        >
          <span className="flex h-7 w-7 items-center justify-center rounded-md border border-border bg-white text-muted-foreground group-hover:text-foreground">
            <Settings className="h-3.5 w-3.5" />
          </span>
          <span className="min-w-0 flex-1">
            <span className="block text-[12px] font-medium text-sidebar-foreground">
              配置
            </span>
            <span className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
              <span
                className={cn(
                  "h-1.5 w-1.5 rounded-full",
                  connected === null
                    ? "bg-zinc-400"
                    : connected
                      ? "bg-emerald-500"
                      : "bg-red-500"
                )}
              />
              {connected === null
                ? "连接控制面…"
                : connected
                  ? "控制面已连接"
                  : "控制面未连接"}
            </span>
          </span>
          <ChevronRight className="h-3.5 w-3.5 text-muted-foreground/60 transition-transform group-hover:translate-x-0.5" />
        </button>
      </div>
      {settingsOpen && !settingsLoading && (
        <SettingsPanel
          open
          onOpenChange={setSettingsOpen}
          settings={settings}
          loading={settingsLoading}
          loadError={settingsError}
          onRefresh={onRefreshSettings}
          onSave={handleSaveSettings}
          onConnectJira={onConnectJira}
        />
      )}
    </aside>
  );
}
