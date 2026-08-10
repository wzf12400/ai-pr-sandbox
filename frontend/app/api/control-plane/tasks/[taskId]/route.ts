const CONTROL_PLANE_URL = process.env.CONTROL_PLANE_URL || "http://127.0.0.1:8080";

export async function GET(
  _request: Request,
  context: { params: Promise<{ taskId: string }> },
) {
  const { taskId } = await context.params;
  if (!/^[0-9a-f-]{36}$/i.test(taskId)) {
    return Response.json({ detail: "任务编号格式不正确。" }, { status: 400 });
  }
  try {
    const response = await fetch(`${CONTROL_PLANE_URL}/api/tasks/${taskId}`, {
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
