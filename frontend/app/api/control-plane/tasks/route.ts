import { NextRequest } from "next/server";

const CONTROL_PLANE_URL = process.env.CONTROL_PLANE_URL || "http://127.0.0.1:8080";

export async function GET() {
  return forward("/api/tasks");
}

export async function POST(request: NextRequest) {
  return forward("/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: await request.text(),
  });
}

async function forward(path: string, init?: RequestInit) {
  try {
    const response = await fetch(CONTROL_PLANE_URL + path, {
      ...init,
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });
    return new Response(await response.text(), {
      status: response.status,
      headers: { "Content-Type": response.headers.get("Content-Type") || "application/json" },
    });
  } catch {
    return Response.json(
      { detail: "无法连接本机 Java 服务，请先启动控制面。" },
      { status: 503 },
    );
  }
}
