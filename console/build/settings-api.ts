import { spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import {
  chmodSync,
  existsSync,
  readFileSync,
  readdirSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import type { IncomingMessage, ServerResponse } from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { Plugin } from "vite";
import { parse, parseDocument } from "yaml";

const CONSOLE_ROOT = path.resolve(
  fileURLToPath(new URL("..", import.meta.url))
);
const REPOSITORY_ROOT = path.resolve(CONSOLE_ROOT, "..");
const APPLICATION_YML = path.join(
  REPOSITORY_ROOT,
  "control-plane/src/main/resources/application.yml"
);
const APP_ENV = (process.env.APP_ENV ?? "local").trim().toLowerCase();
if (!["local", "staging", "production"].includes(APP_ENV)) {
  throw new Error("APP_ENV must be local, staging, or production");
}
const ENV_FILE = path.join(REPOSITORY_ROOT, `.env.${APP_ENV}`);
const CODE_POLICY_DIR = path.join(
  REPOSITORY_ROOT,
  "control-plane/config/code-policies"
);
const REPOSITORY_SCOPE_FILE = path.join(
  REPOSITORY_ROOT,
  "control-plane/config/repository-search-scope.json"
);
const MAX_BODY_BYTES = 64_000;
const REPOSITORY_PATTERN = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const CONTROL_PLANE_ROUTES_URL =
  "http://127.0.0.1:8080/api/log-repository-routes/sync-repositories";
const CONTROL_PLANE_JIRA_ROUTES_URL =
  "http://127.0.0.1:8080/api/jira-repository-routes";
const CONTROL_PLANE_JIRA_BINDING_URL =
  "http://127.0.0.1:8080/api/jira-repository-routes/project-binding";
const CONTROL_PLANE_SETTINGS_URL =
  "http://127.0.0.1:8080/api/configuration-profiles/CONSOLE_SETTINGS";
const JIRA_MONITOR_RELOAD_URL =
  "http://127.0.0.1:8098/jira-monitor/session/reload";
const PYTHON = existsSync(path.join(REPOSITORY_ROOT, ".venv/bin/python3"))
  ? path.join(REPOSITORY_ROOT, ".venv/bin/python3")
  : "python3";

const ENV_DEFAULTS: Record<string, string> = {
  DB_URL:
    "jdbc:mysql://127.0.0.1:3306/github_ai_agent?useUnicode=true&characterEncoding=utf8&serverTimezone=Asia/Shanghai",
  DB_USER: "root",
  REDIS_URL: "redis://127.0.0.1:6379/0",
  SERVER_ADDRESS: "127.0.0.1",
  SERVER_PORT: "8080",
  AUTOMATION_POLICY_ID: "local-development-policy",
  WORKER_REDIS_ENABLED: "true",
  WORKER_QUEUE_KEY: "github-ai-agent:jobs:v2",
  WORKER_OUTBOX_BATCH_SIZE: "100",
  WORKER_OUTBOX_PUBLISH_DELAY: "2s",
  WORKER_RECONCILIATION_ENABLED: "false",
  WORKER_RECONCILIATION_BATCH_SIZE: "100",
  WORKER_RECONCILIATION_DELAY: "5m",
  WORKER_RECONCILIATION_REPUBLISH_AFTER: "10m",
  WORKER_STALE_TASK_TIMEOUT: "30m",
  WORKER_MAX_RETRIES: "3",
  AI_CHAT_ENABLED: "true",
  AI_BASE_URL: "",
  AI_MODEL: "ailemac/gpt-5-mini",
  AI_SAFETY_IDENTIFIER: "",
  AI_TIMEOUT_SECONDS: "30",
  AI_MAX_COMPLETION_TOKENS: "800",
  AI_API_MODE: "strict",
  AI_ASYNC_REPLY: "true",
  WORKER_CODE_MODEL: "",
  JIRA_BASE_URL: "",
};
const EDITABLE_ENV_KEYS = new Set(Object.keys(ENV_DEFAULTS));
const HIDDEN_ENV_KEYS = new Set([
  "DB_URL",
  "DB_USER",
  "REDIS_URL",
  "WORKER_REDIS_ENABLED",
  "WORKER_QUEUE_KEY",
  "SERVER_ADDRESS",
  "SERVER_PORT",
  "AUTOMATION_POLICY_ID",
  "WORKER_OUTBOX_BATCH_SIZE",
  "WORKER_OUTBOX_PUBLISH_DELAY",
  "WORKER_RECONCILIATION_ENABLED",
  "WORKER_RECONCILIATION_BATCH_SIZE",
  "WORKER_RECONCILIATION_DELAY",
  "WORKER_RECONCILIATION_REPUBLISH_AFTER",
  "WORKER_STALE_TASK_TIMEOUT",
  "WORKER_MAX_RETRIES",
]);
const SECRET_KEYS = new Set([
  "DB_PASSWORD",
  "AI_API_KEY",
  "GITHUB_API_TOKEN",
  "GITHUB_ISSUE_TOKEN",
  "GITHUB_ROUTING_TOKEN",
  "GITHUB_COPILOT_TOKEN",
  "JIRA_USERNAME",
  "JIRA_PASSWORD",
]);
const VISIBLE_SECRET_KEYS = new Set([
  "AI_API_KEY",
  "GITHUB_API_TOKEN",
  "JIRA_USERNAME",
  "JIRA_PASSWORD",
]);
const BOOLEAN_KEYS = new Set([
  "WORKER_REDIS_ENABLED",
  "WORKER_RECONCILIATION_ENABLED",
  "AI_CHAT_ENABLED",
  "AI_ASYNC_REPLY",
]);
const INTEGER_KEYS = new Set([
  "SERVER_PORT",
  "WORKER_OUTBOX_BATCH_SIZE",
  "WORKER_RECONCILIATION_BATCH_SIZE",
  "WORKER_MAX_RETRIES",
  "AI_TIMEOUT_SECONDS",
  "AI_MAX_COMPLETION_TOKENS",
]);
const DURATION_KEYS = new Set([
  "WORKER_OUTBOX_PUBLISH_DELAY",
  "WORKER_RECONCILIATION_DELAY",
  "WORKER_RECONCILIATION_REPUBLISH_AFTER",
  "WORKER_STALE_TASK_TIMEOUT",
]);

type RepositoryInput = {
  repository: string;
  keywords: string[];
  dependencies?: string[];
  jiraProjectName?: string;
  jiraProjectKey?: string;
  jiraBindingStatus?: "bound" | "not_found" | "unavailable" | "unconfigured";
  jiraBindingDetail?: string;
};

type SavePayload = {
  activeAccount: string;
  environment: Record<string, string>;
  secrets: Record<string, string>;
  repositories: RepositoryInput[];
  revision: string;
};

type JiraConnectPayload = {
  baseUrl: unknown;
  username: unknown;
  password: unknown;
  revision: unknown;
};

type StoredSettings = {
  activeAccount: string;
  environment: Record<string, string>;
  repositories: RepositoryInput[];
};

type ConfigurationProfile = {
  profileKey: string;
  payload: unknown;
  initialized: boolean;
  source: string;
  version: number;
  updatedAt: string;
};

class SettingsApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

class ControlPlaneRequestError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function parseEnv(raw: string): Record<string, string> {
  const values: Record<string, string> = {};
  for (const line of raw.split(/\r?\n/)) {
    const match = /^([A-Z][A-Z0-9_]*)=(.*)$/.exec(line);
    if (match) values[match[1]] = match[2];
  }
  return values;
}

function readEnvFile(): { raw: string; values: Record<string, string> } {
  const raw = existsSync(ENV_FILE) ? readFileSync(ENV_FILE, "utf8") : "";
  return { raw, values: parseEnv(raw) };
}

function updateEnv(
  raw: string,
  updates: Record<string, string>
): string {
  const remaining = new Map(Object.entries(updates));
  const lines = raw ? raw.split(/\r?\n/) : [];
  const output = lines.map((line) => {
    const match = /^([A-Z][A-Z0-9_]*)=/.exec(line);
    if (!match || !remaining.has(match[1])) return line;
    const value = remaining.get(match[1])!;
    remaining.delete(match[1]);
    return `${match[1]}=${value}`;
  });
  if (output.length > 0 && output.at(-1) === "") output.pop();
  for (const [key, value] of remaining) output.push(`${key}=${value}`);
  return `${output.join("\n")}\n`;
}

function sameRepositories(
  left: RepositoryInput[],
  right: RepositoryInput[]
): boolean {
  return JSON.stringify(
    left.map(({ repository, keywords, dependencies }) => ({
      repository,
      keywords,
      dependencies: dependencies ?? [],
    }))
  ) === JSON.stringify(
    right.map(({ repository, keywords, dependencies }) => ({
      repository,
      keywords,
      dependencies: dependencies ?? [],
    }))
  );
}

function mirrorCompatibilityFiles(
  settings: StoredSettings,
  secrets: Record<string, string> = {}
): void {
  const currentRepositories = readRepositories();
  if (!sameRepositories(currentRepositories, settings.repositories)) {
    const applicationYmlRaw = readFileSync(APPLICATION_YML, "utf8");
    const yml = parseDocument(applicationYmlRaw);
    yml.setIn(
      ["app", "repository-catalog"],
      settings.repositories.map(
        ({ repository, keywords, dependencies = [] }) => ({
          repository,
          keywords,
          ...(dependencies.length > 0 ? { dependencies } : {}),
        })
      )
    );
    const ymlBytes = yml.toString({ lineWidth: 120 });
    const ymlTemp = `${APPLICATION_YML}.${randomBytes(8).toString("hex")}.tmp`;
    writeFileSync(ymlTemp, ymlBytes, {
      encoding: "utf8",
      mode: 0o644,
      flag: "wx",
    });
    renameSync(ymlTemp, APPLICATION_YML);
  }

  const envFile = readEnvFile();
  const {
    GITHUB_API_TOKEN: githubApiToken,
    ...directSecrets
  } = secrets;
  const updates = {
    ...settings.environment,
    ...directSecrets,
    ...(githubApiToken
      ? {
          GITHUB_ROUTING_TOKEN: githubApiToken,
        }
      : {}),
  };
  const needsEnvUpdate = Object.entries(updates).some(
    ([key, value]) => envFile.values[key] !== value
  );
  if (needsEnvUpdate) {
    const envBytes = updateEnv(envFile.raw, updates);
    const envTemp = `${ENV_FILE}.${randomBytes(8).toString("hex")}.tmp`;
    writeFileSync(envTemp, envBytes, {
      encoding: "utf8",
      mode: 0o600,
      flag: "wx",
    });
    renameSync(envTemp, ENV_FILE);
    chmodSync(ENV_FILE, 0o600);
  }
}

function ghAccounts(): { activeAccount: string; accounts: string[] } {
  const result = spawnSync(
    "gh",
    [
      "auth",
      "status",
      "--json",
      "hosts",
      "--jq",
      '.hosts["github.com"]',
    ],
    {
      cwd: REPOSITORY_ROOT,
      encoding: "utf8",
      timeout: 10_000,
      maxBuffer: 64_000,
    }
  );
  if (result.status !== 0) return { activeAccount: "", accounts: [] };
  try {
    const rows = JSON.parse(result.stdout) as unknown;
    if (!Array.isArray(rows)) return { activeAccount: "", accounts: [] };
    const valid = rows.filter(
      (row): row is { login: string; active?: boolean } =>
        row !== null &&
        typeof row === "object" &&
        typeof row.login === "string" &&
        /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$/.test(row.login)
    );
    return {
      activeAccount: valid.find((row) => row.active === true)?.login ?? "",
      accounts: [...new Set(valid.map((row) => row.login))],
    };
  } catch {
    return { activeAccount: "", accounts: [] };
  }
}

function readRepositories(): RepositoryInput[] {
  const root = parse(readFileSync(APPLICATION_YML, "utf8")) as unknown;
  if (root === null || typeof root !== "object") return [];
  const app = (root as Record<string, unknown>).app;
  if (app === null || typeof app !== "object") return [];
  const catalog = (app as Record<string, unknown>)["repository-catalog"];
  if (!Array.isArray(catalog)) return [];
  return catalog.flatMap((item) => {
    const record = item as Record<string, unknown>;
    if (
      item === null ||
      typeof item !== "object" ||
      typeof record.repository !== "string" ||
      !Array.isArray(record.keywords)
    ) {
      return [];
    }
    const keywords = record.keywords;
    if (!keywords.every((keyword) => typeof keyword === "string")) return [];
    const dependencies = record.dependencies ?? [];
    if (
      !Array.isArray(dependencies) ||
      !dependencies.every((dependency) => typeof dependency === "string")
    ) {
      return [];
    }
    return [
      {
        repository: record.repository,
        keywords,
        dependencies,
      },
    ];
  });
}

function readCodeModels(): { available: string[]; defaultModel: string } {
  const policies = readdirSync(CODE_POLICY_DIR)
    .filter((name) => name.endsWith(".json"))
    .sort()
    .flatMap((name) => {
      const value = JSON.parse(
        readFileSync(path.join(CODE_POLICY_DIR, name), "utf8")
      ) as unknown;
      if (value === null || typeof value !== "object") return [];
      const policy = value as Record<string, unknown>;
      if (
        !Array.isArray(policy.allowed_models) ||
        !policy.allowed_models.every((model) => typeof model === "string") ||
        typeof policy.default_model !== "string"
      ) {
        return [];
      }
      return [
        {
          allowedModels: policy.allowed_models,
          defaultModel: policy.default_model,
        },
      ];
    });
  if (policies.length === 0) return { available: [], defaultModel: "" };

  const common = new Set(policies[0].allowedModels);
  for (const policy of policies.slice(1)) {
    const allowed = new Set(policy.allowedModels);
    for (const model of common) {
      if (!allowed.has(model)) common.delete(model);
    }
  }
  const available = policies[0].allowedModels.filter((model) =>
    common.has(model)
  );
  const preferred = policies[0].defaultModel;
  return {
    available,
    defaultModel: available.includes(preferred) ? preferred : available[0] ?? "",
  };
}

function readRepositoryWarnings(
  repositories: RepositoryInput[]
): Record<string, string> {
  const policyRepositories = new Set(
    readdirSync(CODE_POLICY_DIR)
      .filter((name) => name.endsWith(".json"))
      .flatMap((name) => {
        const value = JSON.parse(
          readFileSync(path.join(CODE_POLICY_DIR, name), "utf8")
        ) as Record<string, unknown>;
        return typeof value.repository === "string" ? [value.repository] : [];
      })
  );
  const scope = JSON.parse(
    readFileSync(REPOSITORY_SCOPE_FILE, "utf8")
  ) as Record<string, unknown>;
  const entries = Array.isArray(scope.repositories) ? scope.repositories : [];
  const routedRepositories = new Set(
    entries.flatMap((entry) => {
      if (entry === null || typeof entry !== "object") return [];
      const value = entry as Record<string, unknown>;
      return typeof value.repository === "string" && value.enabled === true
        ? [value.repository]
        : [];
    })
  );

  return Object.fromEntries(
    repositories.flatMap(({ repository }) => {
      const missing: string[] = [];
      if (!policyRepositories.has(repository)) missing.push("代码执行策略");
      if (!routedRepositories.has(repository)) missing.push("仓库路由");
      return missing.length > 0
        ? [[repository, `尚未接入${missing.join("和")}`]]
        : [];
    })
  );
}

function verifyNewRepositories(
  repositories: RepositoryInput[],
  existingRepositories: RepositoryInput[]
): void {
  const existing = new Set(
    existingRepositories.map(({ repository }) => repository.toLowerCase())
  );
  for (const { repository } of repositories) {
    if (existing.has(repository.toLowerCase())) continue;
    const result = spawnSync(
      "gh",
      ["api", `repos/${repository}`, "--jq", ".full_name"],
      {
        cwd: REPOSITORY_ROOT,
        encoding: "utf8",
        timeout: 10_000,
        maxBuffer: 16_000,
      }
    );
    if (
      result.status !== 0 ||
      result.stdout.trim().toLowerCase() !== repository.toLowerCase()
    ) {
      throw new SettingsApiError(
        422,
        `当前 GitHub 账号无法读取仓库 ${repository}，请检查仓库名称或访问权限。`
      );
    }
  }
}

async function requestJson(
    url: string,
    init: RequestInit
  ): Promise<Record<string, unknown> | unknown[]> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12_000);
    try {
      const response = await fetch(url, {
        ...init,
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
      });
      const payload = (await response.json().catch(() => null)) as unknown;
      if (!response.ok) {
        const rawDetail =
          payload !== null &&
          typeof payload === "object" &&
          !Array.isArray(payload) &&
          typeof (payload as Record<string, unknown>).detail === "string"
            ? (payload as Record<string, unknown>).detail
            : "本地服务请求失败";
        throw new ControlPlaneRequestError(response.status, String(rawDetail));
      }
      if (
        payload === null ||
        typeof payload !== "object"
      ) {
        throw new Error("本地服务响应无效");
      }
      return payload as Record<string, unknown> | unknown[];
    } finally {
      clearTimeout(timeout);
    }
}

function configurationProfile(value: unknown): ConfigurationProfile {
    if (
      value === null ||
      typeof value !== "object" ||
      Array.isArray(value)
    ) {
      throw new SettingsApiError(502, "配置中心响应无效");
    }
    const profile = value as Record<string, unknown>;
    if (
      profile.profileKey !== "CONSOLE_SETTINGS" ||
      typeof profile.initialized !== "boolean" ||
      typeof profile.source !== "string" ||
      typeof profile.version !== "number" ||
      !Number.isSafeInteger(profile.version) ||
      profile.version < 0 ||
      typeof profile.updatedAt !== "string" ||
      profile.payload === null ||
      typeof profile.payload !== "object" ||
      Array.isArray(profile.payload)
    ) {
      throw new SettingsApiError(502, "配置中心响应无效");
    }
    return profile as unknown as ConfigurationProfile;
}

async function readConfigurationProfile(): Promise<ConfigurationProfile> {
    try {
      return configurationProfile(
        await requestJson(CONTROL_PLANE_SETTINGS_URL, { method: "GET" })
      );
    } catch (cause) {
      if (cause instanceof SettingsApiError) throw cause;
      throw new SettingsApiError(
        502,
        cause instanceof Error
          ? `配置中心读取失败：${cause.message}`
          : "配置中心读取失败"
      );
    }
}

async function writeConfigurationProfile(
    expectedVersion: number,
    payload: StoredSettings,
    source: "CONSOLE" | "FILE_IMPORT"
): Promise<ConfigurationProfile> {
    try {
      return configurationProfile(
        await requestJson(CONTROL_PLANE_SETTINGS_URL, {
          method: "PUT",
          body: JSON.stringify({ expectedVersion, payload, source }),
        })
      );
    } catch (cause) {
      if (
        cause instanceof ControlPlaneRequestError &&
        cause.status === 409
      ) {
        throw new SettingsApiError(
          409,
          "配置已被其他用户更新，请重新读取后再保存。"
        );
      }
      if (cause instanceof SettingsApiError) throw cause;
      throw new SettingsApiError(
        502,
        cause instanceof Error
          ? `配置中心保存失败：${cause.message}`
          : "配置中心保存失败"
      );
    }
}

function repositoryName(repository: string): string {
    return repository.split("/", 2)[1].toLowerCase();
  }

  async function syncLogRoutes(repositories: RepositoryInput[]): Promise<void> {
    if (repositories.length === 0) return;
    try {
      await requestJson(CONTROL_PLANE_ROUTES_URL, {
        method: "PUT",
        body: JSON.stringify({
          repositories: repositories.map(({ repository }) => repository),
        }),
      });
    } catch (cause) {
      throw new SettingsApiError(
        502,
        cause instanceof Error
          ? `日志映射保存失败：${cause.message}`
          : "日志映射保存失败"
      );
  }
}

async function verifyLogRoutes(
    repositories: RepositoryInput[]
  ): Promise<Record<string, string>> {
    const warnings: Record<string, string> = {};
    if (repositories.length === 0) return warnings;
    const result = spawnSync(PYTHON, ["-m", "src.log_route_verifier"], {
      cwd: REPOSITORY_ROOT,
      encoding: "utf8",
      input: JSON.stringify(repositories.map(({ repository }) =>
        repositoryName(repository)
      )),
      timeout: 120_000,
      maxBuffer: 64_000,
    });
    if (result.status !== 0) {
      for (const { repository } of repositories) {
        warnings[repository] =
          "日志映射已保存，但日志平台暂时无法验证，请人工检查映射。";
      }
      return warnings;
    }
    try {
      const payload = JSON.parse(result.stdout) as unknown;
      if (
        payload === null ||
        typeof payload !== "object" ||
        (payload as Record<string, unknown>).status !== "ok" ||
        !Array.isArray((payload as Record<string, unknown>).results)
      ) {
        throw new Error("invalid verifier response");
      }
      const found = new Map(
        ((payload as Record<string, unknown>).results as unknown[]).flatMap(
          (entry) => {
            if (
              entry === null ||
              typeof entry !== "object" ||
              typeof (entry as Record<string, unknown>).selectorValue !== "string" ||
              typeof (entry as Record<string, unknown>).found !== "boolean"
            ) {
              return [];
            }
            return [[
              (entry as Record<string, unknown>).selectorValue as string,
              (entry as Record<string, unknown>).found as boolean,
            ] as const];
          }
        )
      );
      for (const { repository } of repositories) {
        if (found.get(repositoryName(repository)) !== true) {
          warnings[repository] =
            "最近两小时未找到同名日志，请人工修改日志映射。";
        }
      }
    } catch {
      for (const { repository } of repositories) {
        warnings[repository] =
          "日志映射已保存，但日志平台暂时无法验证，请人工检查映射。";
      }
    }
    return warnings;
  }

type JiraBindingResult = {
  jiraProjectName: string;
  jiraProjectKey: string;
  jiraBindingStatus: "bound" | "not_found" | "unavailable" | "unconfigured";
  jiraBindingDetail: string;
};

async function readJiraBindings(): Promise<Map<string, JiraBindingResult>> {
  const payload = await requestJson(
    `${CONTROL_PLANE_JIRA_ROUTES_URL}?enabledOnly=true`,
    { method: "GET" }
  );
  if (!Array.isArray(payload)) {
    throw new Error("Jira 映射响应无效");
  }
  const grouped = new Map<string, JiraBindingResult[]>();
  for (const value of payload) {
    if (value === null || typeof value !== "object") continue;
    const route = value as Record<string, unknown>;
    if (
      route.enabled !== true ||
      route.matchType !== "PROJECT" ||
      route.matchValue !== "*" ||
      typeof route.projectKey !== "string" ||
      typeof route.projectName !== "string" ||
      typeof route.repository !== "string"
    ) {
      continue;
    }
    const item: JiraBindingResult = {
      jiraProjectName: route.projectName,
      jiraProjectKey: route.projectKey,
      jiraBindingStatus: "bound",
      jiraBindingDetail: `已绑定 · ${route.projectKey}`,
    };
    const key = route.repository.toLowerCase();
    grouped.set(key, [...(grouped.get(key) ?? []), item]);
  }
  return new Map(
    [...grouped].map(([repository, bindings]) => {
      const first = bindings[0];
      return [
        repository,
        {
          ...first,
          jiraBindingDetail:
            bindings.length === 1
              ? first.jiraBindingDetail
              : `已绑定 · ${bindings.length} 个 Jira 项目`,
        },
      ];
    })
  );
}

function unavailableJiraBindings(
  repositories: RepositoryInput[]
): Record<string, JiraBindingResult> {
  return Object.fromEntries(
    repositories
      .filter(({ jiraProjectName }) => Boolean(jiraProjectName?.trim()))
      .map(({ repository, jiraProjectName }) => [
        repository,
        {
          jiraProjectName: jiraProjectName!.trim(),
          jiraProjectKey: "",
          jiraBindingStatus: "unavailable" as const,
          jiraBindingDetail: "Jira 暂时无法检索",
        },
      ])
  );
}

async function verifyAndBindJiraProjects(
  repositories: RepositoryInput[]
): Promise<Record<string, JiraBindingResult>> {
  const requested = repositories.filter(({ jiraProjectName }) =>
    Boolean(jiraProjectName?.trim())
  );
  const removed = repositories.filter(
    ({ jiraProjectName, jiraProjectKey }) =>
      !jiraProjectName?.trim() && Boolean(jiraProjectKey)
  );
  const bindings: Record<string, JiraBindingResult> = {};
  await Promise.all(
    removed.map(async ({ repository }) => {
      try {
        await requestJson(
          `${CONTROL_PLANE_JIRA_BINDING_URL}?repository=${encodeURIComponent(repository)}`,
          { method: "DELETE" }
        );
      } catch {
        bindings[repository] = {
          jiraProjectName: "",
          jiraProjectKey: "",
          jiraBindingStatus: "unavailable",
          jiraBindingDetail: "Jira 映射暂时无法移除",
        };
      }
    })
  );
  if (requested.length === 0) return bindings;
  const names = [
    ...new Set(requested.map(({ jiraProjectName }) => jiraProjectName!.trim())),
  ];
  const localEnv = readEnvFile().values;
  const childEnv = { ...process.env };
  for (const key of ["JIRA_BASE_URL", "JIRA_SESSION_COOKIE"]) {
    if (localEnv[key]) childEnv[key] = localEnv[key];
  }
  const result = spawnSync(PYTHON, ["-m", "src.jira_project_verifier"], {
    cwd: REPOSITORY_ROOT,
    encoding: "utf8",
    input: JSON.stringify(names),
    env: childEnv,
    timeout: 30_000,
    maxBuffer: 64_000,
  });
  if (result.status !== 0) {
    return { ...bindings, ...unavailableJiraBindings(requested) };
  }

  let matches: Map<string, Record<string, unknown>>;
  try {
    const payload = JSON.parse(result.stdout) as unknown;
    if (
      payload === null ||
      typeof payload !== "object" ||
      (payload as Record<string, unknown>).status !== "ok" ||
      !Array.isArray((payload as Record<string, unknown>).results)
    ) {
      throw new Error("invalid Jira verifier response");
    }
    matches = new Map(
      ((payload as Record<string, unknown>).results as unknown[]).flatMap(
        (value) => {
          if (
            value === null ||
            typeof value !== "object" ||
            typeof (value as Record<string, unknown>).requestedName !== "string"
          ) {
            return [];
          }
          const row = value as Record<string, unknown>;
          return [[(row.requestedName as string).toLocaleLowerCase(), row]];
        }
      )
    );
  } catch {
    return { ...bindings, ...unavailableJiraBindings(requested) };
  }

  await Promise.all(
    requested.map(async ({ repository, jiraProjectName }) => {
      const requestedName = jiraProjectName!.trim();
      const match = matches.get(requestedName.toLocaleLowerCase());
      if (
        match?.found !== true ||
        typeof match.projectKey !== "string" ||
        typeof match.projectName !== "string"
      ) {
        bindings[repository] = {
          jiraProjectName: requestedName,
          jiraProjectKey: "",
          jiraBindingStatus: "not_found",
          jiraBindingDetail: "未检索到",
        };
        return;
      }
      try {
        await requestJson(CONTROL_PLANE_JIRA_BINDING_URL, {
          method: "PUT",
          body: JSON.stringify({
            projectKey: match.projectKey,
            projectName: match.projectName,
            repository,
          }),
        });
        bindings[repository] = {
          jiraProjectName: match.projectName,
          jiraProjectKey: match.projectKey,
          jiraBindingStatus: "bound",
          jiraBindingDetail: `已绑定 · ${match.projectKey}`,
        };
      } catch {
        bindings[repository] = {
          jiraProjectName: requestedName,
          jiraProjectKey: "",
          jiraBindingStatus: "unavailable",
          jiraBindingDetail: "Jira 映射暂时无法保存",
        };
      }
    })
  );
  return bindings;
}

function legacyStoredSettings(): StoredSettings {
  const env = readEnvFile().values;
  const identity = ghAccounts();
  return {
    activeAccount: identity.activeAccount,
    environment: Object.fromEntries(
      Object.entries(ENV_DEFAULTS)
        .filter(([key]) => !HIDDEN_ENV_KEYS.has(key))
        .map(([key, fallback]) => [
          key,
          env[key] ?? process.env[key] ?? fallback,
        ])
    ),
    repositories: readRepositories(),
  };
}

function storedSettings(
  value: unknown,
  origin = "数据库中的"
): StoredSettings {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new SettingsApiError(502, `${origin}控制台配置无效`);
  }
  const payload = value as Record<string, unknown>;
  if (typeof payload.activeAccount !== "string") {
    throw new SettingsApiError(502, `${origin} GitHub 账号配置无效`);
  }
  let environment: Record<string, string>;
  let repositories: RepositoryInput[];
  try {
    environment = validateEnvironment(payload.environment, false);
    repositories = validateRepositories(payload.repositories);
  } catch (cause) {
    throw new SettingsApiError(
      502,
      cause instanceof Error
        ? `${origin}控制台配置无效：${cause.message}`
        : `${origin}控制台配置无效`
    );
  }
  return {
    activeAccount: payload.activeAccount,
    environment,
    repositories,
  };
}

async function currentConfiguration(): Promise<{
  profile: ConfigurationProfile;
  settings: StoredSettings;
}> {
  let profile = await readConfigurationProfile();
  if (!profile.initialized) {
    const imported = storedSettings(legacyStoredSettings(), "本地文件中的");
    try {
      profile = await writeConfigurationProfile(
        profile.version,
        imported,
        "FILE_IMPORT"
      );
    } catch (cause) {
      if (!(cause instanceof SettingsApiError) || cause.status !== 409) {
        throw cause;
      }
      profile = await readConfigurationProfile();
    }
  }
  return { profile, settings: storedSettings(profile.payload) };
}

async function snapshotBase() {
  const { profile, settings } = await currentConfiguration();
  const codeModels = readCodeModels();
  const configuredCodeModel = settings.environment.WORKER_CODE_MODEL;
  const configurationWarnings =
    configuredCodeModel && !codeModels.available.includes(configuredCodeModel)
      ? [
          `代码模型 ${configuredCodeModel} 已不在全部仓库策略的允许范围内，请重新选择。`,
        ]
      : [];
  const effectiveSettings =
    configurationWarnings.length > 0
      ? {
          ...settings,
          environment: {
            ...settings.environment,
            WORKER_CODE_MODEL: "",
          },
        }
      : settings;
  try {
    mirrorCompatibilityFiles(effectiveSettings);
  } catch (cause) {
    throw new SettingsApiError(
      500,
      cause instanceof Error
        ? `数据库配置已读取，但本地运行配置同步失败：${cause.message}`
        : "数据库配置已读取，但本地运行配置同步失败"
    );
  }
  const env = readEnvFile().values;
  const identity = ghAccounts();
  return {
    activeAccount: identity.accounts.includes(settings.activeAccount)
      ? settings.activeAccount
      : identity.activeAccount,
    accounts: identity.accounts,
    codeModels,
    environment: Object.fromEntries(
      Object.entries(ENV_DEFAULTS)
        .filter(([key]) => !HIDDEN_ENV_KEYS.has(key))
        .map(([key, fallback]) => [
          key,
          effectiveSettings.environment[key] ?? fallback,
        ])
    ),
    secretConfigured: Object.fromEntries(
      [...VISIBLE_SECRET_KEYS].map((key) => [
        key,
        key === "GITHUB_API_TOKEN"
          ? Boolean(
              env.GITHUB_ROUTING_TOKEN ||
                env.GITHUB_COPILOT_TOKEN ||
                process.env.GITHUB_ROUTING_TOKEN ||
                process.env.GITHUB_COPILOT_TOKEN
            )
          : Boolean(env[key] || process.env[key]),
      ])
    ),
    repositories: effectiveSettings.repositories,
    repositoryWarnings: readRepositoryWarnings(effectiveSettings.repositories),
    configurationWarnings,
    revision: String(profile.version),
  };
}

async function snapshot() {
  const base = await snapshotBase();
  try {
    const bindings = await readJiraBindings();
    return {
      ...base,
      repositories: base.repositories.map((repository) => ({
        ...repository,
        jiraProjectName:
          bindings.get(repository.repository.toLowerCase())?.jiraProjectName ?? "",
        jiraProjectKey:
          bindings.get(repository.repository.toLowerCase())?.jiraProjectKey ?? "",
        jiraBindingStatus:
          bindings.get(repository.repository.toLowerCase())?.jiraBindingStatus ??
          "unconfigured",
        jiraBindingDetail:
          bindings.get(repository.repository.toLowerCase())?.jiraBindingDetail ?? "",
      })),
    };
  } catch {
    return {
      ...base,
      repositories: base.repositories.map((repository) => ({
        ...repository,
        jiraProjectName: "",
        jiraProjectKey: "",
        jiraBindingStatus: "unconfigured" as const,
        jiraBindingDetail: "",
      })),
    };
  }
}

function validateEnvironment(
  value: unknown,
  validateCodeModel = true
): Record<string, string> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("environment must be an object");
  }
  const validated: Record<string, string> = {};
  for (const [key, raw] of Object.entries(value)) {
    if (!EDITABLE_ENV_KEYS.has(key) || typeof raw !== "string") {
      throw new Error("environment contains an unsupported value");
    }
    const item = raw.trim();
    if (item.length > 2_048 || /[\r\n\0]/.test(item)) {
      throw new Error(`${key} is invalid`);
    }
    if (BOOLEAN_KEYS.has(key) && !["true", "false"].includes(item)) {
      throw new Error(`${key} must be true or false`);
    }
    if (INTEGER_KEYS.has(key) && !/^[0-9]{1,6}$/.test(item)) {
      throw new Error(`${key} must be an integer`);
    }
    if (DURATION_KEYS.has(key) && !/^[1-9][0-9]*(?:ms|s|m|h)$/.test(item)) {
      throw new Error(`${key} must be a positive duration`);
    }
    validated[key] = item;
  }
  if (validated.AI_API_MODE && !["strict", "compatible"].includes(validated.AI_API_MODE)) {
    throw new Error("AI_API_MODE is invalid");
  }
  if (
    validateCodeModel &&
    validated.WORKER_CODE_MODEL &&
    !readCodeModels().available.includes(validated.WORKER_CODE_MODEL)
  ) {
    throw new Error("WORKER_CODE_MODEL is not allowed by every repository policy");
  }
  return validated;
}

function validateSecrets(value: unknown): Record<string, string> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("secrets must be an object");
  }
  const validated: Record<string, string> = {};
  for (const [key, raw] of Object.entries(value)) {
    if (!SECRET_KEYS.has(key) || typeof raw !== "string") {
      throw new Error("secrets contain an unsupported value");
    }
    if (raw && (raw.length > 4_096 || /[\r\n\0]/.test(raw))) {
      throw new Error(`${key} is invalid`);
    }
    if (raw) validated[key] = raw;
  }
  return validated;
}

function validateRepositories(value: unknown): RepositoryInput[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > 50) {
    throw new Error("repositories must contain between 1 and 50 items");
  }
  const repositories: RepositoryInput[] = value.map((item) => {
    if (item === null || typeof item !== "object") {
      throw new Error("repository item is invalid");
    }
    const repository = (item as Record<string, unknown>).repository;
    const keywords = (item as Record<string, unknown>).keywords;
    const rawJiraProjectName =
      (item as Record<string, unknown>).jiraProjectName ?? "";
    const rawJiraProjectKey =
      (item as Record<string, unknown>).jiraProjectKey ?? "";
    if (
      typeof repository !== "string" ||
      !REPOSITORY_PATTERN.test(repository) ||
      !Array.isArray(keywords) ||
      keywords.length < 1 ||
      keywords.length > 50 ||
      !keywords.every(
        (keyword) =>
          typeof keyword === "string" &&
          keyword.trim().length > 0 &&
          keyword.length <= 80 &&
          !/[\r\n\0]/.test(keyword)
      ) ||
      typeof rawJiraProjectName !== "string" ||
      rawJiraProjectName.trim().length > 120 ||
      /[\r\n\0]/.test(rawJiraProjectName) ||
      typeof rawJiraProjectKey !== "string" ||
      !/^(?:[A-Z][A-Z0-9_]{0,19})?$/.test(rawJiraProjectKey)
    ) {
      throw new Error("repository configuration is invalid");
    }
    return {
      repository,
      keywords: [...new Set(keywords.map((keyword) => keyword.trim()))],
      dependencies: [],
      jiraProjectName: rawJiraProjectName.trim(),
      jiraProjectKey: rawJiraProjectKey,
    };
  });
  for (let index = 0; index < repositories.length; index += 1) {
    const item = value[index] as Record<string, unknown>;
    const rawDependencies = item.dependencies ?? [];
    if (
      !Array.isArray(rawDependencies) ||
      rawDependencies.length > 50 ||
      !rawDependencies.every(
        (dependency) =>
          typeof dependency === "string" &&
          REPOSITORY_PATTERN.test(dependency) &&
          dependency !== repositories[index].repository
      )
    ) {
      throw new Error("repository dependencies are invalid");
    }
    repositories[index].dependencies = [
      ...new Set(rawDependencies as string[]),
    ];
  }
  if (
    new Set(repositories.map((item) => item.repository.toLowerCase())).size !==
    repositories.length
  ) {
    throw new Error("repository configuration contains duplicates");
  }
  const repositoryNames = new Set(
    repositories.map(({ repository }) => repository)
  );
  if (
    repositories.some(({ dependencies = [] }) =>
      dependencies.some((dependency) => !repositoryNames.has(dependency))
    )
  ) {
    throw new Error("repository dependencies must reference configured repositories");
  }
  return repositories;
}

function switchAccount(login: string) {
  const identity = ghAccounts();
  if (!identity.accounts.includes(login)) {
    throw new Error("GitHub account is not available");
  }
  if (identity.activeAccount === login) return;
  const result = spawnSync(
    "gh",
    ["auth", "switch", "--hostname", "github.com", "--user", login],
    {
      cwd: REPOSITORY_ROOT,
      encoding: "utf8",
      timeout: 10_000,
      maxBuffer: 16_000,
    }
  );
  if (result.status !== 0) throw new Error("GitHub account switch failed");
}

async function save(payload: unknown) {
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("settings payload is invalid");
  }
  const value = payload as Partial<SavePayload>;
  if (typeof value.activeAccount !== "string") {
    throw new Error("activeAccount is required");
  }
  if (typeof value.revision !== "string") {
    throw new Error("revision is required");
  }
  const revision = Number(value.revision);
  if (
    !Number.isSafeInteger(revision) ||
    revision < 0 ||
    String(revision) !== value.revision
  ) {
    throw new Error("revision is invalid");
  }
  const current = await currentConfiguration();
  if (revision !== current.profile.version) {
    throw new SettingsApiError(
      409,
      "配置已被其他用户更新，请重新读取后再保存。"
    );
  }
  const environment = validateEnvironment(value.environment);
  const secrets = validateSecrets(value.secrets);
  const repositories = validateRepositories(value.repositories);
  const existingRepositories = current.settings.repositories;
  const existingNames = new Set(
    existingRepositories.map(({ repository }) => repository.toLowerCase())
  );
  const addedRepositories = repositories.filter(
    ({ repository }) => !existingNames.has(repository.toLowerCase())
  );
  verifyNewRepositories(repositories, existingRepositories);
  const stored = await writeConfigurationProfile(
    revision,
    {
      activeAccount: value.activeAccount,
      environment: {
        ...current.settings.environment,
        ...environment,
      },
      repositories: repositories.map(
        ({
          repository,
          keywords,
          dependencies,
          jiraProjectName,
          jiraProjectKey,
        }) => ({
          repository,
          keywords,
          dependencies,
          jiraProjectName,
          jiraProjectKey,
        })
      ),
    },
    "CONSOLE"
  );
  try {
    switchAccount(value.activeAccount);
    mirrorCompatibilityFiles(storedSettings(stored.payload), secrets);
  } catch (cause) {
    throw new SettingsApiError(
      500,
      cause instanceof Error
        ? `数据库配置已保存，但本地运行配置同步失败：${cause.message}`
        : "数据库配置已保存，但本地运行配置同步失败"
    );
  }
  const logRouteWarnings: Record<string, string> = {};
  try {
    await syncLogRoutes(addedRepositories);
  } catch (cause) {
    for (const { repository } of addedRepositories) {
      logRouteWarnings[repository] =
        cause instanceof Error
          ? cause.message
          : "日志映射保存失败，请稍后重试。";
    }
  }
  Object.assign(
    logRouteWarnings,
    await verifyLogRoutes(
      addedRepositories.filter(
        ({ repository }) => !logRouteWarnings[repository]
      )
    )
  );
  const jiraBindings = await verifyAndBindJiraProjects(repositories);
  const updated = await snapshot();
  if (updated.revision !== String(stored.version)) {
    throw new SettingsApiError(
      409,
      "配置保存后又被其他用户更新，请重新读取最新配置。"
    );
  }
  return {
    ...updated,
    repositories: updated.repositories.map((repository) => {
      const submitted = repositories.find(
        (item) =>
          item.repository.toLowerCase() === repository.repository.toLowerCase()
      );
      const binding = jiraBindings[repository.repository];
      if (binding) return { ...repository, ...binding };
      return {
        ...repository,
        jiraProjectName: submitted?.jiraProjectName ?? repository.jiraProjectName,
      };
    }),
    repositoryWarnings: {
      ...updated.repositoryWarnings,
      ...logRouteWarnings,
    },
  };
}

function validateJiraConnectPayload(payload: unknown) {
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("Jira 连接参数无效");
  }
  const value = payload as Partial<JiraConnectPayload>;
  if (typeof value.baseUrl !== "string") {
    throw new Error("请输入 Jira 地址");
  }
  let parsed: URL;
  try {
    parsed = new URL(value.baseUrl.trim());
  } catch {
    throw new Error("Jira 地址格式无效");
  }
  if (
    parsed.protocol !== "https:" ||
    parsed.username ||
    parsed.password ||
    parsed.search ||
    parsed.hash
  ) {
    throw new Error("Jira 地址必须使用 HTTPS，且不能包含账号、查询参数或锚点");
  }
  const baseUrl = parsed.toString().replace(/\/$/, "");
  if (typeof value.username !== "string" || !value.username.trim()) {
    throw new Error("请输入 Jira 账号");
  }
  if (value.username.length > 256 || /[\r\n\0]/.test(value.username)) {
    throw new Error("Jira 账号格式无效");
  }
  if (typeof value.password !== "string" || !value.password) {
    throw new Error("请输入 Jira 密码");
  }
  if (value.password.length > 4096 || /[\r\n\0]/.test(value.password)) {
    throw new Error("Jira 密码格式无效");
  }
  if (typeof value.revision !== "string") {
    throw new Error("revision is required");
  }
  const revision = Number(value.revision);
  if (
    !Number.isSafeInteger(revision) ||
    revision < 0 ||
    String(revision) !== value.revision
  ) {
    throw new Error("revision is invalid");
  }
  return {
    baseUrl,
    username: value.username.trim(),
    password: value.password,
    revision,
  };
}

function jiraRefreshDetail(stdout: string): string {
  try {
    const parsed = JSON.parse(stdout) as { detail?: unknown };
    if (typeof parsed.detail === "string" && parsed.detail.trim()) {
      return parsed.detail;
    }
  } catch {
    // The child process may fail before it can produce its bounded JSON response.
  }
  return "无法建立 Jira 会话，请检查地址、账号、密码和本机浏览器登录状态";
}

async function connectJira(payload: unknown) {
  const { baseUrl, username, password, revision } =
    validateJiraConnectPayload(payload);
  const current = await currentConfiguration();
  if (revision !== current.profile.version) {
    throw new SettingsApiError(
      409,
      "配置已被其他用户更新，请重新读取后再连接 Jira。"
    );
  }
  const result = spawnSync(
    PYTHON,
    ["-m", "src.jira_session_refresh", "--env-file", ENV_FILE],
    {
      cwd: REPOSITORY_ROOT,
      encoding: "utf8",
      env: {
        ...process.env,
        JIRA_BASE_URL: baseUrl,
        JIRA_USERNAME: username,
        JIRA_PASSWORD: password,
      },
      timeout: 120_000,
      maxBuffer: 16_000,
    }
  );
  if (result.error || result.status !== 0) {
    throw new SettingsApiError(400, jiraRefreshDetail(result.stdout || ""));
  }
  const stored = await writeConfigurationProfile(
    revision,
    {
      ...current.settings,
      environment: {
        ...current.settings.environment,
        JIRA_BASE_URL: baseUrl,
      },
    },
    "CONSOLE"
  );
  try {
    mirrorCompatibilityFiles(storedSettings(stored.payload), {
      JIRA_USERNAME: username,
      JIRA_PASSWORD: password,
    });
    await requestJson(JIRA_MONITOR_RELOAD_URL, {
      method: "POST",
      body: "{}",
    });
  } catch (cause) {
    throw new SettingsApiError(
      500,
      cause instanceof Error
        ? `Jira 已连接，但运行中的监控刷新失败：${cause.message}`
        : "Jira 已连接，但运行中的监控刷新失败"
    );
  }
  return {
    status: "connected",
    detail: "Jira 已连接，后续扫描将使用当前账号。",
    settings: await snapshot(),
  };
}

function sendJson(response: ServerResponse, status: number, payload: unknown) {
  response.statusCode = status;
  response.setHeader("Content-Type", "application/json; charset=utf-8");
  response.setHeader("Cache-Control", "no-store");
  response.end(JSON.stringify(payload));
}

async function readBody(request: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += buffer.length;
    if (size > MAX_BODY_BYTES) throw new Error("settings request is too large");
    chunks.push(buffer);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function sameOrigin(request: IncomingMessage): boolean {
  const origin = request.headers.origin;
  if (!origin) return false;
  return /^http:\/\/(?:127\.0\.0\.1|localhost):7100$/.test(origin);
}

export function settingsApiPlugin(): Plugin {
  return {
    name: "local-project-settings-api",
    configureServer(server) {
      server.middlewares.use("/console-settings", async (request, response) => {
        try {
          const requestPath = (request.url ?? "/").split("?", 1)[0];
          if (request.method === "GET") {
            sendJson(response, 200, await snapshot());
            return;
          }
          if (request.method === "PUT") {
            if (!sameOrigin(request)) {
              sendJson(response, 403, { detail: "request origin is not allowed" });
              return;
            }
            sendJson(response, 200, await save(await readBody(request)));
            return;
          }
          if (
            request.method === "POST" &&
            requestPath === "/jira-connect"
          ) {
            if (!sameOrigin(request)) {
              sendJson(response, 403, { detail: "request origin is not allowed" });
              return;
            }
            sendJson(response, 200, await connectJira(await readBody(request)));
            return;
          }
          sendJson(response, 405, { detail: "method not allowed" });
        } catch (error) {
          sendJson(response, error instanceof SettingsApiError ? error.status : 400, {
            detail: error instanceof Error ? error.message : "settings request failed",
          });
        }
      });
    },
  };
}
