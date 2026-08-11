"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";

type TaskStatus =
  | "PENDING"
  | "PROCESSING"
  | "TESTING"
  | "AWAITING_PR_REVIEW"
  | "COMPLETED"
  | "FAILED"
  | "NEEDS_CONTEXT";

type LogIncident = {
  firstSeenAt: string;
  lastSeenAt: string;
  currentScanEventCount: number;
  historicalEventCount: number;
  affectedEndpoints: string[];
};

type Task = {
  id: string;
  sourceType: "NATURAL_LANGUAGE" | "LOG";
  inputSummary: string;
  status: TaskStatus;
  matchedRepository: string | null;
  issueNumber: number | null;
  issueUrl: string | null;
  prUrl: string | null;
  testSummary: string | null;
  blockedReason: string | null;
  createdAt: string;
  logIncident: LogIncident | null;
};

type TaskEvent = {
  id: number;
  eventType: string;
  toStatus: TaskStatus;
  detail: string | null;
  createdAt: string;
};

type TaskDetail = { task: Task; events: TaskEvent[] };

const STATUS: Record<TaskStatus, string> = {
  PENDING: "等待执行",
  PROCESSING: "处理中",
  TESTING: "测试中",
  AWAITING_PR_REVIEW: "等待 PR 审核",
  COMPLETED: "已完成",
  FAILED: "失败",
  NEEDS_CONTEXT: "需要补充信息",
};

const DEMO =
  "计算器的 divide 遇到零时返回明确错误，并检查 src/calculator.py 和 tests/test_calculator.py";

function formatTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

async function errorMessage(response: Response) {
  try {
    const body = (await response.json()) as { detail?: string };
    return body.detail || "请求失败。";
  } catch {
    return "请求失败，请检查本机服务。";
  }
}

export function TaskConsole() {
  const [sourceType, setSourceType] = useState<"NATURAL_LANGUAGE" | "LOG">(
    "NATURAL_LANGUAGE",
  );
  const [input, setInput] = useState("");
  const [firstSeenAt, setFirstSeenAt] = useState("");
  const [lastSeenAt, setLastSeenAt] = useState("");
  const [currentCount, setCurrentCount] = useState("1");
  const [historicalCount, setHistoricalCount] = useState("1");
  const [endpoint, setEndpoint] = useState("");
  const [tasks, setTasks] = useState<Task[]>([]);
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [connected, setConnected] = useState<boolean | null>(null);
  const [message, setMessage] = useState("");

  const loadDetail = useCallback(async (taskId: string) => {
    const response = await fetch(`/api/control-plane/tasks/${taskId}`, {
      cache: "no-store",
    });
    if (!response.ok) throw new Error(await errorMessage(response));
    setDetail((await response.json()) as TaskDetail);
    setSelectedId(taskId);
  }, []);

  const refresh = useCallback(
    async (preferredId?: string) => {
      setLoading(true);
      setMessage("");
      try {
        const response = await fetch("/api/control-plane/tasks", {
          cache: "no-store",
        });
        if (!response.ok) throw new Error(await errorMessage(response));
        const next = (await response.json()) as Task[];
        setTasks(next);
        setConnected(true);
        const taskId = preferredId || next[0]?.id;
        if (taskId) await loadDetail(taskId);
        else setDetail(null);
      } catch (error) {
        setConnected(false);
        setMessage(error instanceof Error ? error.message : "无法连接本机服务。");
      } finally {
        setLoading(false);
      }
    },
    [loadDetail],
  );

  useEffect(() => {
    const timer = window.setTimeout(() => void refresh(), 0);
    return () => window.clearTimeout(timer);
  }, [refresh]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const description = input.trim();
    if (!description) {
      setMessage("请先输入任务。")
      return;
    }
    setSubmitting(true);
    setMessage("");
    try {
      let body: Record<string, unknown> = { sourceType, input: description };
      if (sourceType === "LOG") {
        const current = Number(currentCount);
        const historical = Number(historicalCount);
        if (!firstSeenAt || !lastSeenAt || current < 1 || historical < current) {
          throw new Error("请填写正确的时间和次数。");
        }
        const reference = Array.from(crypto.getRandomValues(new Uint8Array(10)))
          .map((value) => value.toString(16).padStart(2, "0"))
          .join("");
        body = {
          sourceType,
          input: description,
          logIncident: {
            dataSafetyStatus: "SANITIZED",
            sourceReference: `incident_ref:${reference}`,
            firstSeenAt: new Date(firstSeenAt).toISOString(),
            lastSeenAt: new Date(lastSeenAt).toISOString(),
            currentScanEventCount: current,
            historicalEventCount: historical,
            incidentGroupCount: 1,
            affectedEndpoints: endpoint.trim() ? [endpoint.trim()] : [],
            affectedUserCountMin: null,
            affectedUserCountMax: null,
            userIdentifierEventCount: 0,
            historicalCountComplete: true,
            aggregationBasis: "manual-safe-log-test",
          },
        };
      }
      const response = await fetch("/api/control-plane/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) throw new Error(await errorMessage(response));
      const created = (await response.json()) as Task;
      setInput("");
      await refresh(created.id);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "创建任务失败。");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <h1>GitHub AI Agent</h1>
          <p>本机流程测试</p>
        </div>
        <span className={`connection ${connected === false ? "offline" : ""}`}>
          {connected === null ? "连接中" : connected ? "已连接" : "未连接"}
        </span>
      </header>

      <div className="workspace">
        <section className="panel composer">
          <h2>创建任务</h2>
          <p className="muted">只运行 Mock，不会创建真实 Issue 或 PR。</p>
          <form onSubmit={submit}>
            <div className="source-switch">
              <button
                type="button"
                className={sourceType === "NATURAL_LANGUAGE" ? "active" : ""}
                onClick={() => setSourceType("NATURAL_LANGUAGE")}
              >
                自然语言
              </button>
              <button
                type="button"
                className={sourceType === "LOG" ? "active" : ""}
                onClick={() => setSourceType("LOG")}
              >
                日志故障
              </button>
            </div>

            <label htmlFor="task-input">任务描述</label>
            <textarea
              id="task-input"
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder="输入要完成的任务"
              maxLength={4000}
            />

            {sourceType === "LOG" && (
              <div className="log-fields">
                <label>第一次出现<input type="datetime-local" value={firstSeenAt} onChange={(event) => setFirstSeenAt(event.target.value)} /></label>
                <label>最近一次出现<input type="datetime-local" value={lastSeenAt} onChange={(event) => setLastSeenAt(event.target.value)} /></label>
                <label>本次次数<input type="number" min="1" value={currentCount} onChange={(event) => setCurrentCount(event.target.value)} /></label>
                <label>历史次数<input type="number" min="1" value={historicalCount} onChange={(event) => setHistoricalCount(event.target.value)} /></label>
                <label className="wide">影响接口<input value={endpoint} onChange={(event) => setEndpoint(event.target.value)} placeholder="/api/orders" /></label>
              </div>
            )}

            {sourceType === "NATURAL_LANGUAGE" && (
              <button className="text-button" type="button" onClick={() => setInput(DEMO)}>
                填入测试示例
              </button>
            )}
            <button className="primary-button" type="submit" disabled={submitting}>
              {submitting ? "提交中…" : "提交任务"}
            </button>
          </form>
          {message && <p className="notice" role="alert">{message}</p>}
        </section>

        <section className="panel tasks">
          <div className="task-heading">
            <h2>任务结果</h2>
            <button className="text-button" type="button" onClick={() => void refresh(selectedId || undefined)} disabled={loading}>
              {loading ? "刷新中…" : "刷新"}
            </button>
          </div>

          {tasks.length === 0 && !loading ? (
            <p className="empty">暂无任务</p>
          ) : (
            <div className="task-layout">
              <div className="task-list">
                {tasks.map((task) => (
                  <button
                    key={task.id}
                    type="button"
                    className={`task-card ${selectedId === task.id ? "selected" : ""}`}
                    onClick={() => void loadDetail(task.id)}
                  >
                    <span className="status">{STATUS[task.status]}</span>
                    <strong>{task.inputSummary}</strong>
                    <small>{task.matchedRepository || "待匹配仓库"}</small>
                  </button>
                ))}
              </div>
              <div className="detail-pane">
                {detail ? <TaskDetailView detail={detail} /> : <p className="empty">选择一条任务</p>}
              </div>
            </div>
          )}
        </section>
      </div>
    </main>
  );
}

function TaskDetailView({ detail }: { detail: TaskDetail }) {
  const { task, events } = detail;
  return (
    <div>
      <div className="detail-title">
        <h3>{task.inputSummary}</h3>
        <span className="status">{STATUS[task.status]}</span>
      </div>
      <dl className="facts">
        <div><dt>仓库</dt><dd>{task.matchedRepository || "等待补充"}</dd></div>
        <div><dt>Issue</dt><dd>{task.issueUrl ? <a href={task.issueUrl}>#{task.issueNumber}</a> : "尚未创建"}</dd></div>
        <div><dt>PR</dt><dd>{task.prUrl ? <a href={task.prUrl}>打开 PR</a> : "尚未创建"}</dd></div>
      </dl>

      {task.logIncident && (
        <p className="result">
          日志：{formatTime(task.logIncident.firstSeenAt)} 至 {formatTime(task.logIncident.lastSeenAt)}，
          历史 {task.logIncident.historicalEventCount} 次。
        </p>
      )}
      {(task.blockedReason || task.testSummary) && (
        <p className="result">{task.blockedReason || task.testSummary}</p>
      )}

      <h4>执行记录</h4>
      <ol className="timeline">
        {events.map((event) => (
          <li key={event.id}>
            <div><strong>{STATUS[event.toStatus] || event.eventType}</strong><time>{formatTime(event.createdAt)}</time></div>
            <p>{event.detail || event.eventType}</p>
          </li>
        ))}
      </ol>
    </div>
  );
}
