import { useEffect, useRef, useState } from "react";
import {
  ArrowUp,
  Bot,
  CircleAlert,
  ExternalLink,
  FileText,
  GitBranch,
  GitPullRequest,
  Loader2,
  User,
} from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { createTask, getTaskDetail, postTaskMessage } from "@/lib/api";
import { STATUS_META, formatTime } from "@/lib/status";
import type { Task, TaskDetail, TaskEvent, TaskStatus } from "@/types/task";

type Props = {
  selectedId: string | null;
  onSelect: (id: string) => void;
  connected: boolean | null;
  lastRefresh: Date | null;
  onRefresh: () => void;
};

export function ChatView({
  selectedId,
  onSelect,
  connected,
  lastRefresh,
  onRefresh,
}: Props) {
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [pendingMessages, setPendingMessages] = useState<string[]>([]);
  const [awaitingReplyAt, setAwaitingReplyAt] = useState<number | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const detailRef = useRef<TaskDetail | null>(null);
  detailRef.current = detail;

  useEffect(() => {
    setPendingMessages([]);
    setAwaitingReplyAt(null);
  }, [selectedId]);

  // AI 回复到达后（或 90 秒兜底）关闭「正在思考」指示
  useEffect(() => {
    if (awaitingReplyAt === null) return;
    const replied = detail?.events.some(
      (e) =>
        e.eventType === "AGENT_REPLY" &&
        Date.parse(e.createdAt) >= awaitingReplyAt - 5000
    );
    if (replied) {
      setAwaitingReplyAt(null);
      return;
    }
    const timer = setTimeout(() => setAwaitingReplyAt(null), 90_000);
    return () => clearTimeout(timer);
  }, [awaitingReplyAt, detail]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    // 只在切换线程时显示骨架屏；后台轮询静默更新，避免内容闪烁
    const switching = detailRef.current?.task.id !== selectedId;
    if (switching) setLoading(true);
    getTaskDetail(selectedId)
      .then((d) => {
        if (cancelled) return;
        setDetail(d);
        setLoading(false);
      })
      .catch(() => {
        if (cancelled) return;
        if (switching) setDetail(null);
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId, lastRefresh]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [
    selectedId,
    detail?.events.length,
    detail?.task.status,
    pendingMessages.length,
    awaitingReplyAt,
  ]);

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col bg-white">
      {connected === false && (
        <div className="flex items-center gap-2 border-b border-amber-200 bg-amber-50 px-5 py-2 text-xs text-amber-800">
          <CircleAlert className="h-3.5 w-3.5" />
          无法连接控制面。请先启动 MySQL、Redis，再运行 cd control-plane && mvn
          spring-boot:run
        </div>
      )}

      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-2xl px-4 py-6">
          {!selectedId && <Welcome />}
          {selectedId && loading && (
            <div className="space-y-3">
              <Skeleton className="h-10 w-3/4 rounded-2xl" />
              <Skeleton className="h-24 w-full rounded-2xl" />
            </div>
          )}
          {selectedId && !loading && detail && (
            <Conversation detail={detail} />
          )}
          {selectedId && !loading && !detail && (
            <p className="py-10 text-center text-xs text-muted-foreground">
              无法加载任务详情
            </p>
          )}
          <PendingBubbles detail={detail} pending={pendingMessages} />
          {awaitingReplyAt !== null && <ThinkingBubble />}
        </div>
      </div>

      <Composer
        disabled={connected === false}
        selectedId={selectedId}
        selectedStatus={detail?.task.status ?? null}
        onOptimistic={(text, expectReply) => {
          setPendingMessages((prev) => [...prev, text]);
          if (expectReply) setAwaitingReplyAt(Date.now());
        }}
        onOptimisticSettled={(text, ok) => {
          setPendingMessages((prev) => prev.filter((m) => m !== text));
          if (!ok) setAwaitingReplyAt(null);
        }}
        onSubmitted={(task) => {
          // NEEDS_CONTEXT 的追问回复由 AI 异步生成，显示思考指示
          if (task.status === "NEEDS_CONTEXT") setAwaitingReplyAt(Date.now());
          onRefresh();
          onSelect(task.id);
        }}
        onMessaged={(d) => {
          setDetail(d);
          onRefresh();
        }}
      />
    </div>
  );
}

function Welcome() {
  return (
    <div className="flex flex-col items-center pb-8 pt-16 text-center">
      <div className="flex h-12 w-12 items-center justify-center rounded-2xl border border-border bg-secondary">
        <Bot className="h-6 w-6 text-foreground" />
      </div>
      <h1 className="mt-4 text-lg font-semibold tracking-tight">
        有什么需要改的？
      </h1>
      <p className="mt-1.5 max-w-sm text-[13px] leading-relaxed text-muted-foreground">
        用自然语言描述代码变更，我会路由到授权仓库、生成 Issue
        并在门禁通过后提交 Draft PR。缺少信息时可以直接在对话里补充。
      </p>
    </div>
  );
}

function BotAvatar() {
  return (
    <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-border bg-white">
      <Bot className="h-4 w-4" />
    </div>
  );
}

function UserBubble({
  label,
  text,
  time,
}: {
  label: string;
  text: string;
  time: string;
}) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] rounded-2xl rounded-br-md bg-secondary px-4 py-2.5">
        <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <User className="h-3 w-3" />
          {label} · {formatTime(time)}
        </div>
        <p className="mt-1 whitespace-pre-wrap break-words text-[14px] leading-relaxed">
          {text}
        </p>
      </div>
    </div>
  );
}

function AgentBubble({ text, time }: { text: string; time: string }) {
  return (
    <div className="flex gap-3">
      <BotAvatar />
      <div className="max-w-[85%] rounded-2xl rounded-tl-md border border-border bg-card px-4 py-2.5">
        <p className="whitespace-pre-wrap break-words text-[13px] leading-relaxed">
          {text}
        </p>
        <span className="mt-1 block text-[11px] text-muted-foreground/60">
          {formatTime(time)}
        </span>
      </div>
    </div>
  );
}

function Conversation({ detail }: { detail: TaskDetail }) {
  const { task, events } = detail;
  return (
    <div className="space-y-5">
      {/* 初始需求 */}
      <UserBubble
        label={task.sourceType === "LOG" ? "日志故障" : "自然语言"}
        text={task.inputSummary}
        time={task.createdAt}
      />

      {/* 助手回复：当前状态卡 */}
      <div className="flex gap-3">
        <BotAvatar />
        <div className="min-w-0 flex-1 space-y-3">
          <TaskCard task={task} />
          {task.issueUrl && <IssueCard issueUrl={task.issueUrl} />}
        </div>
      </div>

      {/* 事件流：对话消息与状态事件按时间穿插 */}
      {events
        .filter((ev) => ev.eventType !== "TASK_CREATED")
        .map((ev) =>
          ev.eventType === "USER_MESSAGE" ? (
            <UserBubble
              key={ev.id}
              label="补充信息"
              text={ev.detail ?? ""}
              time={ev.createdAt}
            />
          ) : ev.eventType === "AGENT_REPLY" ? (
            <AgentBubble key={ev.id} text={ev.detail ?? ""} time={ev.createdAt} />
          ) : (
            <div key={ev.id} className="pl-10">
              <EventLine event={ev} />
            </div>
          )
        )}
    </div>
  );
}

function PendingBubbles({
  detail,
  pending,
}: {
  detail: TaskDetail | null;
  pending: string[];
}) {
  if (pending.length === 0) return null;
  // 服务端已入库的消息不再重复显示乐观气泡
  const known = new Set<string>();
  if (detail) {
    known.add(detail.task.inputSummary);
    for (const ev of detail.events) {
      if (ev.eventType === "USER_MESSAGE" && ev.detail) known.add(ev.detail);
    }
  }
  const visible = pending.filter((text) => !known.has(text));
  if (visible.length === 0) return null;
  return (
    <div className="mt-5 space-y-5">
      {visible.map((text) => (
        <div key={text} className="flex justify-end">
          <div className="max-w-[85%] rounded-2xl rounded-br-md bg-secondary px-4 py-2.5 opacity-70">
            <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <Loader2 className="h-3 w-3 animate-spin" />
              发送中…
            </div>
            <p className="mt-1 whitespace-pre-wrap break-words text-[14px] leading-relaxed">
              {text}
            </p>
          </div>
        </div>
      ))}
    </div>
  );
}

function ThinkingBubble() {
  return (
    <div className="mt-5 flex gap-3">
      <BotAvatar />
      <div className="flex items-center gap-2.5 rounded-2xl rounded-tl-md border border-border bg-card px-4 py-3">
        <span className="flex gap-1">
          <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted-foreground/50 [animation-delay:-0.3s]" />
          <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted-foreground/50 [animation-delay:-0.15s]" />
          <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted-foreground/50" />
        </span>
        <span className="text-[12px] text-muted-foreground">正在思考…</span>
      </div>
    </div>
  );
}

function TaskCard({ task }: { task: Task }) {
  const meta = STATUS_META[task.status];
  return (
    <div className="rounded-xl border border-border bg-card p-4">
      <div className="flex items-center gap-2">
        <span className={cn("h-2 w-2 rounded-full", meta.dot)} />
        <span className="text-[13px] font-medium">{meta.label}</span>
        <span className="font-mono text-[11px] text-muted-foreground">
          {task.id.slice(0, 8)}
        </span>
        <span className="ml-auto rounded border border-border bg-secondary px-1.5 py-0.5 text-[10px] text-muted-foreground">
          {task.executionMode}
        </span>
      </div>

      <div className="mt-3 space-y-1.5 text-[13px]">
        {task.matchedRepository && (
          <div className="flex items-center gap-2">
            <GitBranch className="h-3.5 w-3.5 text-muted-foreground" />
            <span className="font-mono text-xs">{task.matchedRepository}</span>
            {task.routingConfidence != null && (
              <span className="text-[11px] text-muted-foreground">
                置信度 {task.routingConfidence}
              </span>
            )}
          </div>
        )}
        {task.prUrl && (
          <a
            href={task.prUrl}
            target="_blank"
            rel="noreferrer"
            className="flex items-center gap-2 text-violet-700 hover:underline"
          >
            <GitPullRequest className="h-3.5 w-3.5" />
            Draft PR #{task.prNumber}
            <ExternalLink className="h-3 w-3" />
          </a>
        )}
        {task.blockedReason && (
          <p className="text-xs text-orange-700">{task.blockedReason}</p>
        )}
        {task.status === "NEEDS_CONTEXT" && (
          <p className="rounded-md border border-orange-200 bg-orange-50 px-2.5 py-1.5 text-xs text-orange-800">
            缺少仓库路由信息：请在下方对话里补充该需求/故障所属的服务、模块或文件路径
          </p>
        )}
      </div>
    </div>
  );
}

type IssueData = {
  status: string;
  detail?: string;
  number?: number;
  title?: string;
  state?: string;
  url?: string;
  labels?: string[];
  body?: string;
  bodyTruncated?: boolean;
};

function parseIssueUrl(url: string): string | null {
  const m = url.match(
    /^https:\/\/github\.com\/([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+)\/issues\/(\d+)/
  );
  return m ? `/issue/${m[1]}/${m[2]}/${m[3]}` : null;
}

function IssueCard({ issueUrl }: { issueUrl: string }) {
  const [issue, setIssue] = useState<IssueData | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const path = parseIssueUrl(issueUrl);
    if (!path) {
      setFailed(true);
      return;
    }
    let cancelled = false;
    fetch(path, { headers: { Accept: "application/json" } })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((d: IssueData) => {
        if (cancelled) return;
        if (d.status === "ok") setIssue(d);
        else setFailed(true);
      })
      .catch(() => !cancelled && setFailed(true));
    return () => {
      cancelled = true;
    };
  }, [issueUrl]);

  if (failed) {
    return (
      <a
        href={issueUrl}
        target="_blank"
        rel="noreferrer"
        className="flex items-center gap-2 rounded-xl border border-border bg-card px-4 py-3 text-[13px] text-sky-700 hover:underline"
      >
        <FileText className="h-3.5 w-3.5" />
        在 GitHub 查看 Issue
        <ExternalLink className="h-3 w-3" />
      </a>
    );
  }
  if (!issue) {
    return <Skeleton className="h-24 w-full rounded-xl" />;
  }
  return (
    <div className="rounded-xl border border-border bg-card p-4">
      <div className="flex flex-wrap items-center gap-2">
        <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />
        <span className="text-[13px] font-semibold">Issue #{issue.number}</span>
        <span
          className={cn(
            "rounded-full px-2 py-0.5 text-[10px] font-medium",
            issue.state === "OPEN"
              ? "bg-emerald-50 text-emerald-700"
              : "bg-secondary text-muted-foreground"
          )}
        >
          {issue.state}
        </span>
        {(issue.labels ?? []).map((label) => (
          <span
            key={label}
            className="rounded-full border border-border px-2 py-0.5 text-[10px] text-muted-foreground"
          >
            {label}
          </span>
        ))}
        <a
          href={issue.url ?? issueUrl}
          target="_blank"
          rel="noreferrer"
          className="ml-auto flex shrink-0 items-center gap-1 text-[11px] text-sky-700 hover:underline"
        >
          在 GitHub 打开
          <ExternalLink className="h-3 w-3" />
        </a>
      </div>
      <h4 className="mt-2 text-[14px] font-medium leading-snug">{issue.title}</h4>
      {issue.body && (
        <div className="mt-2 max-h-72 overflow-y-auto rounded-lg border border-border/60 bg-secondary/40 px-3 py-2">
          <p className="whitespace-pre-wrap break-words text-[12px] leading-relaxed text-foreground/90">
            {issue.body}
          </p>
          {issue.bodyTruncated && (
            <p className="mt-1 text-[11px] text-muted-foreground">
              （正文过长已截断，点击右上角链接查看完整内容）
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function EventLine({ event }: { event: TaskEvent }) {
  return (
    <div className="flex gap-2 text-[13px]">
      <span className="mt-[7px] h-1.5 w-1.5 shrink-0 rounded-full bg-zinc-300" />
      <div className="min-w-0">
        <span className="font-medium">{event.eventType}</span>
        {event.toStatus && (
          <span
            className={cn(
              "ml-1.5 rounded border px-1 py-px text-[10px]",
              STATUS_META[event.toStatus]?.badge
            )}
          >
            {STATUS_META[event.toStatus]?.label ?? event.toStatus}
          </span>
        )}
        {event.detail && (
          <p className="mt-0.5 break-words text-xs leading-relaxed text-muted-foreground">
            {event.detail}
          </p>
        )}
        <span className="text-[11px] text-muted-foreground/60">
          {formatTime(event.createdAt)}
        </span>
      </div>
    </div>
  );
}

function composerPlaceholder(
  disabled: boolean,
  selectedId: string | null,
  status: TaskStatus | null
): string {
  if (disabled) return "控制面未连接…";
  if (!selectedId) return "描述一个代码变更，例如：给计算器加乘法功能";
  switch (status) {
    case "NEEDS_CONTEXT":
      return "补充信息，例如：这是 ai-pr-sandbox 仓库 calculator 模块的问题…";
    case "FAILED":
      return "补充信息或说明情况，我会重新路由并排队…";
    case "COMPLETED":
      return "任务已完成；可以继续询问，或点左上角新建对话描述新需求";
    default:
      return "继续补充信息或询问进度…";
  }
}

function Composer({
  disabled,
  selectedId,
  selectedStatus,
  onOptimistic,
  onOptimisticSettled,
  onSubmitted,
  onMessaged,
}: {
  disabled: boolean;
  selectedId: string | null;
  selectedStatus: TaskStatus | null;
  onOptimistic: (text: string, expectReply: boolean) => void;
  onOptimisticSettled: (text: string, ok: boolean) => void;
  onSubmitted: (task: Task) => void;
  onMessaged: (detail: TaskDetail) => void;
}) {
  const [value, setValue] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ref = useRef<HTMLTextAreaElement>(null);

  async function send() {
    const text = value.trim();
    if (!text || sending) return;
    setSending(true);
    setError(null);
    // 乐观发送：立即清空输入框并上屏，不等 AI 接口返回
    setValue("");
    onOptimistic(text, selectedId !== null);
    try {
      if (selectedId) {
        const d = await postTaskMessage(selectedId, text);
        onOptimisticSettled(text, true);
        onMessaged(d);
      } else {
        const task = await createTask({
          sourceType: "NATURAL_LANGUAGE",
          input: text,
        });
        onOptimisticSettled(text, true);
        onSubmitted(task);
      }
    } catch (e) {
      onOptimisticSettled(text, false);
      setValue(text);
      setError(e instanceof Error ? e.message : "提交失败");
    } finally {
      setSending(false);
      ref.current?.focus();
    }
  }

  return (
    <div className="shrink-0 border-t border-border bg-white px-4 py-3">
      <div className="mx-auto max-w-2xl">
        {error && (
          <p className="mb-2 rounded-md border border-red-200 bg-red-50 px-3 py-1.5 text-xs text-red-700">
            {error}
          </p>
        )}
        <div className="flex items-end gap-2 rounded-2xl border border-border bg-secondary/60 px-3 py-2 focus-within:border-zinc-400 focus-within:bg-white">
          <textarea
            ref={ref}
            rows={1}
            value={value}
            disabled={disabled}
            placeholder={composerPlaceholder(disabled, selectedId, selectedStatus)}
            className="max-h-40 min-h-[24px] flex-1 resize-none bg-transparent text-[14px] outline-none placeholder:text-muted-foreground disabled:opacity-50"
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                send();
              }
            }}
          />
          <button
            onClick={send}
            disabled={disabled || sending || !value.trim()}
            className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground transition-opacity disabled:opacity-30"
          >
            {sending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <ArrowUp className="h-4 w-4" />
            )}
          </button>
        </div>
        <p className="mt-1.5 text-center text-[10px] text-muted-foreground/70">
          对话经脱敏处理且只匹配授权仓库目录；Issue 自动发布，Copilot 仅生成 Draft
          PR，合并需人工审核
        </p>
      </div>
    </div>
  );
}
