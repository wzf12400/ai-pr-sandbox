import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AI 任务控制台",
  description: "本机 GitHub AI Agent 的自然语言与日志故障任务测试页面。",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
