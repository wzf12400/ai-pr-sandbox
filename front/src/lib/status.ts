import type { TaskStatus } from "@/types/task";

export const STATUS_META: Record<
  TaskStatus,
  { label: string; dot: string; badge: string }
> = {
  PENDING: {
    label: "排队中",
    dot: "bg-zinc-400",
    badge: "border-zinc-300 bg-zinc-100 text-zinc-600",
  },
  PROCESSING: {
    label: "处理中",
    dot: "bg-sky-500 animate-pulse",
    badge: "border-sky-200 bg-sky-50 text-sky-700",
  },
  TESTING: {
    label: "测试中",
    dot: "bg-violet-500 animate-pulse",
    badge: "border-violet-200 bg-violet-50 text-violet-700",
  },
  AWAITING_PR_REVIEW: {
    label: "待审 PR",
    dot: "bg-amber-500",
    badge: "border-amber-200 bg-amber-50 text-amber-700",
  },
  COMPLETED: {
    label: "已完成",
    dot: "bg-emerald-500",
    badge: "border-emerald-200 bg-emerald-50 text-emerald-700",
  },
  FAILED: {
    label: "失败",
    dot: "bg-red-500",
    badge: "border-red-200 bg-red-50 text-red-700",
  },
  NEEDS_CONTEXT: {
    label: "需补充",
    dot: "bg-orange-500",
    badge: "border-orange-200 bg-orange-50 text-orange-700",
  },
};

export function formatTime(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(
    d.getMinutes()
  )}:${pad(d.getSeconds())}`;
}

export function timeAgo(value: string | null | undefined): string {
  if (!value) return "—";
  const diff = Date.now() - new Date(value).getTime();
  if (diff < 0) return "刚刚";
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s} 秒前`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} 分钟前`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} 小时前`;
  return `${Math.floor(h / 24)} 天前`;
}
