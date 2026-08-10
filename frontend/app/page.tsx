import type { Metadata } from "next";
import { TaskConsole } from "./task-console";

export const metadata: Metadata = {
  title: "AI 任务控制台",
  description: "本机 GitHub AI Agent 的自然语言任务测试页面。",
};

export default function Home() {
  return <TaskConsole />;
}
