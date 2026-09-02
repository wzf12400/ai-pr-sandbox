import type { CreateTaskInput, Task, TaskDetail } from "@/types/task";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}${text ? `: ${text}` : ""}`);
  }
  return (await res.json()) as T;
}

export function listTasks(): Promise<Task[]> {
  return request<Task[]>("/api/tasks");
}

export function getTaskDetail(taskId: string): Promise<TaskDetail> {
  return request<TaskDetail>(`/api/tasks/${encodeURIComponent(taskId)}`);
}

export function createTask(input: CreateTaskInput): Promise<Task> {
  return request<Task>("/api/tasks", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function postTaskMessage(
  taskId: string,
  content: string
): Promise<TaskDetail> {
  return request<TaskDetail>(`/api/tasks/${encodeURIComponent(taskId)}/messages`, {
    method: "POST",
    body: JSON.stringify({ content }),
  });
}

export async function deleteTask(taskId: string): Promise<void> {
  const res = await fetch(`/api/tasks/${encodeURIComponent(taskId)}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}${text ? `: ${text}` : ""}`);
  }
}
