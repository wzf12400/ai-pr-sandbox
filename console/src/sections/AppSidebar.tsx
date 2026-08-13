import { Bot, SquarePen, Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { STATUS_META, timeAgo } from "@/lib/status";
import type { Task } from "@/types/task";

type Props = {
  tasks: Task[];
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onDelete: (id: string) => void;
  connected: boolean | null;
};

export function AppSidebar({ tasks, selectedId, onSelect, onDelete, connected }: Props) {
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
          className="ml-auto flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-foreground"
        >
          <SquarePen className="h-4 w-4" />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="px-2 py-2">
          <div className="px-2 pb-1.5 text-[11px] font-medium text-muted-foreground">
            任务线程
          </div>
          {tasks.length === 0 && (
            <p className="px-2 py-6 text-center text-[11px] text-muted-foreground">
              暂无任务
            </p>
          )}
          {tasks.map((t) => {
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

      <div className="border-t border-border px-3 py-2.5">
        <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
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
          {connected === null ? "连接控制面…" : connected ? "控制面已连接" : "控制面未连接"}
          <span className="ml-auto font-mono text-[10px] text-muted-foreground/60">
            :8080
          </span>
        </div>
      </div>
    </aside>
  );
}
