import { useCallback, useEffect, useRef, useState } from "react";
import type { JiraMonitorStatus } from "@/types/jira";

const POLL_INTERVAL = 30000;

async function fetchStatus(): Promise<JiraMonitorStatus> {
  const res = await fetch("/jira-monitor", { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`${res.status}`);
  return (await res.json()) as JiraMonitorStatus;
}

export function useJiraMonitor() {
  const [status, setStatus] = useState<JiraMonitorStatus | null>(null);
  const [reachable, setReachable] = useState<boolean | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await fetchStatus();
      setStatus(data);
      setReachable(true);
    } catch {
      setReachable(false);
    }
  }, []);

  const scan = useCallback(async () => {
    const res = await fetch("/jira-monitor/scan", { method: "POST" });
    if (!res.ok) throw new Error(`扫描失败（${res.status}）`);
    const data = (await res.json()) as JiraMonitorStatus;
    setStatus(data);
    setReachable(true);
  }, []);

  const dispatch = useCallback(async (issue: string, repository?: string) => {
    const res = await fetch("/jira-monitor/dispatch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(repository ? { issue, repository } : { issue }),
    });
    if (!res.ok) throw new Error(`派发失败（${res.status}）`);
    return (await res.json()) as { result: string; taskId?: string; detail?: string };
  }, []);

  const saveRules = useCallback(
    async (project: string, patch: { enabled?: boolean; autoDispatch?: boolean }) => {
      const res = await fetch("/jira-monitor/rules", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project, ...patch }),
      });
      if (!res.ok) throw new Error(`保存失败（${res.status}）`);
      const data = (await res.json()) as JiraMonitorStatus;
      setStatus(data);
    },
    []
  );

  useEffect(() => {
    load();
    timer.current = setInterval(() => load(), POLL_INTERVAL);
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [load]);

  return { status, reachable, reload: load, scan, dispatch, saveRules };
}
