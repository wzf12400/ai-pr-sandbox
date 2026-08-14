import { useState } from "react";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import { useTasks } from "@/hooks/useTasks";
import { deleteTask } from "@/lib/api";
import { AppSidebar } from "@/sections/AppSidebar";
import { ChatView } from "@/sections/ChatView";
import { JiraMonitor } from "@/sections/JiraMonitor";
import { LogMonitor } from "@/sections/LogMonitor";

export default function App() {
  const [selectedId, setSelectedId] = useState<string | null>(() => {
    const hash = window.location.hash;
    return hash.startsWith("#task-") ? hash.slice(6) : null;
  });
  const { tasks, connected, lastRefresh, refresh } = useTasks();

  async function handleDelete(id: string) {
    const task = tasks.find((t) => t.id === id);
    const label = task ? task.inputSummary.slice(0, 40) : id.slice(0, 8);
    if (!window.confirm(`确定删除任务线程「${label}」吗？该操作不可恢复。`)) {
      return;
    }
    try {
      await deleteTask(id);
      if (selectedId === id) setSelectedId(null);
      refresh();
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "删除失败");
    }
  }

  return (
    <div className="h-screen w-screen overflow-hidden bg-background text-foreground">
      <ResizablePanelGroup orientation="horizontal" className="h-full">
        <ResizablePanel defaultSize="240px" minSize="180px" maxSize="45%" className="h-full">
          <AppSidebar
            tasks={tasks}
            selectedId={selectedId}
            onSelect={setSelectedId}
            onDelete={handleDelete}
            connected={connected}
          />
        </ResizablePanel>
        <ResizableHandle withHandle className="w-1 bg-transparent hover:bg-sky-200 data-[resize-handle-state=drag]:bg-sky-300" />
        <ResizablePanel defaultSize="auto" minSize="50%" className="h-full">
          <div className="relative flex h-full flex-col overflow-hidden">
            <ChatView
              selectedId={selectedId}
              onSelect={setSelectedId}
              connected={connected}
              lastRefresh={lastRefresh}
              onRefresh={refresh}
            />
            <div className="pointer-events-none absolute right-3 top-3 z-20 flex w-60 flex-col gap-2">
              <LogMonitor />
              <JiraMonitor />
            </div>
          </div>
        </ResizablePanel>
      </ResizablePanelGroup>
    </div>
  );
}
