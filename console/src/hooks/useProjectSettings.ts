import { useCallback, useEffect, useState } from "react";
import {
  DEFAULT_PROJECT_SETTINGS,
  type JiraConnectInput,
  type JiraConnectResult,
  type ProjectSettings,
  type SettingsSaveInput,
} from "@/lib/project-config";

async function settingsRequest(
  path: string,
  init?: RequestInit
): Promise<ProjectSettings> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as {
      detail?: string;
    } | null;
    throw new Error(
      response.status === 409
        ? "配置已被其他用户更新，请点击“重新读取”后再保存。"
        : body?.detail || "配置请求失败"
    );
  }
  return (await response.json()) as ProjectSettings;
}

async function jiraConnectRequest(
  input: JiraConnectInput
): Promise<JiraConnectResult> {
  const response = await fetch("/console-settings/jira-connect", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as {
      detail?: string;
    } | null;
    throw new Error(body?.detail || "Jira 连接失败");
  }
  return (await response.json()) as JiraConnectResult;
}

export function useProjectSettings() {
  const [settings, setSettings] = useState<ProjectSettings>(
    DEFAULT_PROJECT_SETTINGS
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const refreshSettings = useCallback(async () => {
    try {
      setSettings(await settingsRequest("/console-settings"));
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "配置加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => void refreshSettings(), 0);
    return () => window.clearTimeout(timer);
  }, [refreshSettings]);

  const saveSettings = useCallback(async (input: SettingsSaveInput) => {
    const updated = await settingsRequest("/console-settings", {
      method: "PUT",
      body: JSON.stringify({ ...input, revision: settings.revision }),
    });
    setSettings(updated);
    setError("");
    return updated;
  }, [settings.revision]);

  const connectJira = useCallback(
    async (input: Omit<JiraConnectInput, "revision">) => {
      try {
        const result = await jiraConnectRequest({
          ...input,
          revision: settings.revision,
        });
        setSettings(result.settings);
        setError("");
        return result;
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : "Jira 连接失败");
        throw cause;
      }
    },
    [settings.revision]
  );

  return {
    settings,
    loading,
    error,
    saveSettings,
    connectJira,
    refreshSettings,
  };
}
