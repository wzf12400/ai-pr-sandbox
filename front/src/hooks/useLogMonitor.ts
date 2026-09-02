import { useCallback, useEffect, useRef, useState } from "react";
import type { LogMonitorScan } from "@/types/logmonitor";

const POLL_INTERVAL = 30000;

async function fetchScan(refresh: boolean): Promise<LogMonitorScan> {
  const res = await fetch(refresh ? "/log-monitor/refresh" : "/log-monitor", {
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new Error(`${res.status}`);
  return (await res.json()) as LogMonitorScan;
}

export function useLogMonitor() {
  const [scan, setScan] = useState<LogMonitorScan | null>(null);
  const [reachable, setReachable] = useState<boolean | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async (refresh = false) => {
    try {
      const data = await fetchScan(refresh);
      setScan(data);
      setReachable(true);
    } catch {
      setReachable(false);
    }
  }, []);

  useEffect(() => {
    load();
    timer.current = setInterval(() => load(), POLL_INTERVAL);
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [load]);

  return { scan, reachable, reload: load };
}
