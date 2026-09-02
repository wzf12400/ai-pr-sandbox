import { useCallback, useEffect, useRef, useState } from "react";
import { listTasks } from "@/lib/api";
import type { Task } from "@/types/task";

const POLL_INTERVAL = 5000;

export function useTasks() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [connected, setConnected] = useState<boolean | null>(null);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await listTasks();
      data.sort(
        (a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime()
      );
      setTasks(data);
      setConnected(true);
      setLastRefresh(new Date());
    } catch {
      setConnected(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    timer.current = setInterval(refresh, POLL_INTERVAL);
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [refresh]);

  return { tasks, connected, lastRefresh, refresh };
}
