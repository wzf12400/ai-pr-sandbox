import { useEffect, useState } from "react";
import {
  Bot,
  Building2,
  Check,
  Code2,
  Github,
  KeyRound,
  LoaderCircle,
  MessagesSquare,
  Plus,
  RotateCcw,
  Search,
  Trash2,
  TriangleAlert,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Sheet,
  SheetContent,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import type {
  JiraConnectResult,
  ProjectSettings,
  SettingsSaveInput,
} from "@/lib/project-config";

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  settings: ProjectSettings;
  loading: boolean;
  loadError: string;
  onRefresh: () => Promise<void>;
  onSave: (input: SettingsSaveInput) => Promise<ProjectSettings>;
  onConnectJira: (input: {
    baseUrl: string;
    username: string;
    password: string;
  }) => Promise<JiraConnectResult>;
};

const MODEL_META: Record<string, { name: string; detail: string }> = {
  "claude-sonnet-4.6": { name: "Sonnet 4.6", detail: "速度与质量" },
  "claude-opus-4.6": { name: "Opus 4.6", detail: "复杂任务" },
  "gpt-5.2-codex": { name: "GPT-5.2 Codex", detail: "代码专项" },
  "gpt-5.3-codex": { name: "GPT-5.3 Codex", detail: "代码专项" },
  "gpt-5.4": { name: "GPT-5.4", detail: "均衡 · 默认" },
  "claude-sonnet-4.5": { name: "Sonnet 4.5", detail: "Anthropic" },
  "claude-opus-4.5": { name: "Opus 4.5", detail: "Anthropic" },
};

function EnvInput({
  environment,
  name,
  label,
  onChange,
  type = "text",
}: {
  environment: Record<string, string>;
  name: string;
  label: string;
  onChange: (name: string, value: string) => void;
  type?: string;
}) {
  return (
    <div>
      <Label htmlFor={name} className="text-[10px] text-muted-foreground">
        {label}
      </Label>
      <Input
        id={name}
        name={name}
        type={type}
        value={environment[name] ?? ""}
        onChange={(event) => onChange(name, event.target.value)}
        className="mt-1 h-8 bg-white font-mono text-[11px]"
        autoComplete="off"
        spellCheck={false}
      />
    </div>
  );
}

function EnvSwitch({
  environment,
  name,
  label,
  onChange,
}: {
  environment: Record<string, string>;
  name: string;
  label: string;
  onChange: (name: string, value: string) => void;
}) {
  return (
    <div className="flex items-center justify-between py-1">
      <Label htmlFor={name} className="text-[11px]">
        {label}
      </Label>
      <Switch
        id={name}
        checked={environment[name] === "true"}
        onCheckedChange={(checked) => onChange(name, String(checked))}
      />
    </div>
  );
}

export function SettingsPanel({
  open,
  onOpenChange,
  settings,
  loading,
  loadError,
  onRefresh,
  onSave,
  onConnectJira,
}: Props) {
  const [draft, setDraft] = useState(settings);
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState(loadError);
  const [connectingJira, setConnectingJira] = useState(false);
  const [jiraConnection, setJiraConnection] = useState<{
    status: "success" | "error";
    detail: string;
  } | null>(null);
  const [modelQuery, setModelQuery] = useState("");
  const filteredModels = draft.codeModels.available.filter((model) => {
    const query = modelQuery.trim().toLowerCase();
    if (!query) return true;
    const meta = MODEL_META[model];
    return (
      model.toLowerCase().includes(query) ||
      meta?.name.toLowerCase().includes(query) ||
      meta?.detail.toLowerCase().includes(query)
    );
  });

  useEffect(() => {
    setDraft(settings);
    setSecrets({});
    setSaved(false);
    setError("");
    setJiraConnection(null);
  }, [settings]);

  useEffect(() => {
    if (loadError) setError(loadError);
  }, [loadError]);

  function updateEnvironment(name: string, value: string) {
    setDraft((current) => ({
      ...current,
      environment: { ...current.environment, [name]: value },
    }));
  }

  function updateRepository(
    index: number,
    patch: Partial<ProjectSettings["repositories"][number]>
  ) {
    setDraft((current) => ({
      ...current,
      repositories: current.repositories.map((repository, itemIndex) =>
        itemIndex === index ? { ...repository, ...patch } : repository
      ),
    }));
  }

  function removeRepository(index: number) {
    setDraft((current) => ({
      ...current,
      repositories: current.repositories.filter(
        (_repository, itemIndex) => itemIndex !== index
      ),
    }));
  }

  function addRepository() {
    setDraft((current) => ({
      ...current,
      repositories: [
        ...current.repositories,
        {
          repository: "",
          keywords: [""],
          dependencies: [],
          jiraProjectName: "",
          jiraProjectKey: "",
          jiraBindingStatus: "unconfigured",
          jiraBindingDetail: "",
        },
      ],
    }));
  }

  async function refresh() {
    await onRefresh();
  }

  async function save() {
    setSaving(true);
    setError("");
    try {
      const settingsSecrets = Object.fromEntries(
        Object.entries(secrets).filter(
          ([key]) => !["JIRA_USERNAME", "JIRA_PASSWORD"].includes(key)
        )
      );
      const updated = await onSave({
        activeAccount: draft.activeAccount,
        environment: draft.environment,
        secrets: settingsSecrets,
        repositories: draft.repositories,
        revision: draft.revision,
      });
      setDraft(updated);
      setSaved(true);
      const jiraNeedsAttention = updated.repositories.some((repository) =>
        ["not_found", "unavailable"].includes(repository.jiraBindingStatus)
      );
      if (
        Object.keys(updated.repositoryWarnings).length === 0 &&
        !jiraNeedsAttention
      ) {
        window.setTimeout(() => onOpenChange(false), 900);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "配置保存失败");
    } finally {
      setSaving(false);
    }
  }

  async function connectJira() {
    setConnectingJira(true);
    setJiraConnection(null);
    setError("");
    try {
      const result = await onConnectJira({
        baseUrl: draft.environment.JIRA_BASE_URL ?? "",
        username: secrets.JIRA_USERNAME ?? "",
        password: secrets.JIRA_PASSWORD ?? "",
      });
      setDraft(result.settings);
      setSecrets((current) => ({
        ...current,
        JIRA_USERNAME: "",
        JIRA_PASSWORD: "",
      }));
      setJiraConnection({ status: "success", detail: result.detail });
    } catch (cause) {
      const detail = cause instanceof Error ? cause.message : "Jira 连接失败";
      setJiraConnection({ status: "error", detail });
    } finally {
      setConnectingJira(false);
    }
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="w-[min(94vw,620px)] gap-0 overflow-hidden p-0 sm:max-w-[620px]">
        <SheetHeader className="border-b border-border px-6 py-4">
          <div className="flex items-center gap-3">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-sky-200 bg-sky-50 text-sky-700">
              <Bot className="h-4 w-4" />
            </div>
            <SheetTitle className="text-[15px]">配置</SheetTitle>
          </div>
        </SheetHeader>

        <Tabs defaultValue="account" className="min-h-0 flex-1">
          <div className="border-b border-border px-6">
            <TabsList className="h-10 w-full justify-start gap-5 rounded-none bg-transparent p-0">
              {[
                ["account", "账号"],
                ["repositories", `仓库 ${draft.repositories.length}`],
                ["models", "代码模型"],
                ["chat", "对话"],
              ].map(([value, label]) => (
                <TabsTrigger
                  key={value}
                  value={value}
                  className="h-10 rounded-none border-b-2 border-transparent px-0 text-xs shadow-none data-[state=active]:border-sky-600 data-[state=active]:bg-transparent data-[state=active]:shadow-none"
                >
                  {label}
                </TabsTrigger>
              ))}
            </TabsList>
          </div>

          <TabsContent
            value="account"
            className="m-0 h-[calc(100vh-174px)] overflow-y-auto overscroll-contain px-6 py-5"
          >
            <div className="space-y-4">
              <section className="overflow-hidden rounded-xl border border-sky-200 bg-white shadow-sm">
                <div className="flex items-center gap-3 border-b border-sky-100 bg-sky-50/70 px-4 py-3">
                  <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-sky-700 text-white">
                    <Github className="h-3.5 w-3.5" />
                  </div>
                  <h3 className="text-xs font-semibold">GitHub API</h3>
                </div>
                <div className="bg-white p-3">
                  <div className="flex items-center justify-between gap-2">
                    <Label htmlFor="GITHUB_API_TOKEN" className="text-[11px]">
                      Token
                    </Label>
                    <span className="text-[9px] text-muted-foreground">
                      {draft.secretConfigured.GITHUB_API_TOKEN
                        ? "已配置"
                        : "待配置"}
                    </span>
                  </div>
                  <Input
                    id="GITHUB_API_TOKEN"
                    name="GITHUB_API_TOKEN"
                    type="password"
                    value={secrets.GITHUB_API_TOKEN ?? ""}
                    onChange={(event) =>
                      setSecrets((current) => ({
                        ...current,
                        GITHUB_API_TOKEN: event.target.value,
                      }))
                    }
                    placeholder={
                      draft.secretConfigured.GITHUB_API_TOKEN
                        ? "留空保持现有值"
                        : "输入 Token"
                    }
                    className="mt-2 h-8 bg-zinc-50 font-mono text-[11px]"
                    autoComplete="new-password"
                  />
                </div>
              </section>

              <section className="rounded-xl border border-border bg-white p-4 shadow-sm">
                <div className="mb-3 flex items-center gap-3">
                  <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-violet-100 text-violet-700">
                    <Building2 className="h-3.5 w-3.5" />
                  </div>
                  <h3 className="text-xs font-semibold">Jira</h3>
                </div>
                <div className="space-y-2.5">
                  <EnvInput
                    environment={draft.environment}
                    name="JIRA_BASE_URL"
                    label="Jira 地址"
                    onChange={updateEnvironment}
                  />
                  <div className="grid gap-2.5 sm:grid-cols-2">
                    <div>
                      <Label
                        htmlFor="JIRA_USERNAME"
                        className="text-[10px] text-muted-foreground"
                      >
                        Jira 账号
                      </Label>
                      <Input
                        id="JIRA_USERNAME"
                        name="JIRA_USERNAME"
                        value={secrets.JIRA_USERNAME ?? ""}
                        onChange={(event) =>
                          setSecrets((current) => ({
                            ...current,
                            JIRA_USERNAME: event.target.value,
                          }))
                        }
                        placeholder={
                          draft.secretConfigured.JIRA_USERNAME
                            ? "输入账号以重新连接"
                            : "name@company.com"
                        }
                        className="mt-1 h-8 bg-zinc-50 text-[11px]"
                        autoComplete="username"
                      />
                    </div>
                    <div>
                      <Label
                        htmlFor="JIRA_PASSWORD"
                        className="text-[10px] text-muted-foreground"
                      >
                        Jira 密码
                      </Label>
                      <Input
                        id="JIRA_PASSWORD"
                        name="JIRA_PASSWORD"
                        type="password"
                        value={secrets.JIRA_PASSWORD ?? ""}
                        onChange={(event) =>
                          setSecrets((current) => ({
                            ...current,
                            JIRA_PASSWORD: event.target.value,
                          }))
                        }
                        placeholder={
                          draft.secretConfigured.JIRA_PASSWORD
                            ? "输入密码以重新连接"
                            : "输入密码"
                        }
                        className="mt-1 h-8 bg-zinc-50 text-[11px]"
                        autoComplete="current-password"
                      />
                    </div>
                  </div>
                  <div className="flex items-center justify-between gap-3 pt-1">
                    <div
                      aria-live="polite"
                      className={
                        jiraConnection?.status === "error"
                          ? "text-[10px] leading-4 text-red-600"
                          : "text-[10px] leading-4 text-emerald-700"
                      }
                    >
                      {jiraConnection?.detail ??
                        (draft.secretConfigured.JIRA_USERNAME &&
                        draft.secretConfigured.JIRA_PASSWORD
                          ? "已配置"
                          : "")}
                    </div>
                    <Button
                      type="button"
                      size="sm"
                      onClick={() => void connectJira()}
                      disabled={
                        connectingJira ||
                        !draft.environment.JIRA_BASE_URL?.trim() ||
                        !secrets.JIRA_USERNAME?.trim() ||
                        !secrets.JIRA_PASSWORD
                      }
                      className="shrink-0 bg-violet-700 text-xs hover:bg-violet-800"
                    >
                      {connectingJira ? (
                        <LoaderCircle className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <KeyRound className="h-3.5 w-3.5" />
                      )}
                      {connectingJira ? "连接中…" : "连接 Jira"}
                    </Button>
                  </div>
                </div>
              </section>

              <section className="rounded-xl border border-border bg-white p-4 shadow-sm">
                <div className="mb-2 flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <KeyRound className="h-3.5 w-3.5 text-muted-foreground" />
                    <h3 className="text-xs font-semibold">AI 服务</h3>
                  </div>
                  {draft.secretConfigured.AI_API_KEY && (
                    <span className="flex items-center gap-1 text-[9px] text-emerald-600">
                      <Check className="h-2.5 w-2.5" />
                      已配置
                    </span>
                  )}
                </div>
                <Input
                  id="AI_API_KEY"
                  name="AI_API_KEY"
                  type="password"
                  value={secrets.AI_API_KEY ?? ""}
                  onChange={(event) =>
                    setSecrets((current) => ({
                      ...current,
                      AI_API_KEY: event.target.value,
                    }))
                  }
                  placeholder={
                    draft.secretConfigured.AI_API_KEY
                      ? "留空保持现有密钥"
                      : "输入 AI API Key"
                  }
                  className="h-8 bg-zinc-50 font-mono text-[11px]"
                  autoComplete="new-password"
                />
              </section>
            </div>
          </TabsContent>

          <TabsContent
            value="repositories"
            className="m-0 h-[calc(100vh-174px)] overflow-y-auto overscroll-contain px-6 py-4"
          >
            <div className="space-y-2">
              {draft.repositories.map((repository, index) => (
                <div
                  key={index}
                  className="rounded-lg border border-border bg-white p-3"
                >
                  <div className="flex gap-2">
                    <Input
                      name={`repository-${index}`}
                      aria-label={`仓库 ${index + 1}`}
                      value={repository.repository}
                      onChange={(event) =>
                        updateRepository(index, {
                          repository: event.target.value,
                        })
                      }
                      className="h-8 font-mono text-xs"
                      placeholder="例如 owner/repository…"
                      autoComplete="off"
                      spellCheck={false}
                    />
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-sm"
                      onClick={() => removeRepository(index)}
                      disabled={draft.repositories.length === 1}
                      aria-label={`删除 ${repository.repository || "仓库"}`}
                      className="text-muted-foreground hover:bg-red-50 hover:text-red-600"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                  <div className="mt-3 grid gap-3 sm:grid-cols-2">
                    <div>
                      <Label
                        htmlFor={`repository-keywords-${index}`}
                        className="text-[10px] text-muted-foreground"
                      >
                        路由关键词
                      </Label>
                      <Textarea
                        id={`repository-keywords-${index}`}
                        name={`repository-keywords-${index}`}
                        aria-label={`${repository.repository || `仓库 ${index + 1}`} 关键词`}
                        value={repository.keywords.join(", ")}
                        onChange={(event) =>
                          updateRepository(index, {
                            keywords: event.target.value.split(","),
                          })
                        }
                        className="mt-1 min-h-14 resize-none text-xs"
                        placeholder="例如 api, frontend…"
                        autoComplete="off"
                        spellCheck={false}
                      />
                    </div>
                    <div>
                      <Label
                        htmlFor={`repository-jira-${index}`}
                        className="text-[10px] text-muted-foreground"
                      >
                        Jira 项目名称
                      </Label>
                      <Input
                        id={`repository-jira-${index}`}
                        name={`repository-jira-${index}`}
                        aria-label={`${repository.repository || `仓库 ${index + 1}`} Jira 项目名称`}
                        value={repository.jiraProjectName}
                        onChange={(event) =>
                          updateRepository(index, {
                            jiraProjectName: event.target.value,
                            jiraBindingStatus: "unconfigured",
                            jiraBindingDetail: "",
                          })
                        }
                        className="mt-1 h-8 text-xs"
                        placeholder="输入 Jira 中的完整项目名称…"
                        autoComplete="off"
                      />
                      {repository.jiraBindingStatus !== "unconfigured" && (
                        <div
                          className={`mt-2 flex items-center gap-1.5 text-[10px] ${
                            repository.jiraBindingStatus === "bound"
                              ? "text-emerald-700"
                              : "text-amber-700"
                          }`}
                        >
                          {repository.jiraBindingStatus === "bound" ? (
                            <Check className="h-3 w-3 shrink-0" />
                          ) : (
                            <TriangleAlert className="h-3 w-3 shrink-0" />
                          )}
                          {repository.jiraBindingDetail}
                        </div>
                      )}
                    </div>
                  </div>
                  {draft.repositoryWarnings[repository.repository] && (
                    <div className="mt-2 flex items-center gap-1.5 text-[10px] text-amber-700">
                      <TriangleAlert className="h-3 w-3 shrink-0" />
                      {draft.repositoryWarnings[repository.repository]}
                    </div>
                  )}
                </div>
              ))}
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={addRepository}
                className="w-full border-dashed text-xs"
              >
                <Plus className="h-3.5 w-3.5" />
                添加仓库
              </Button>
              <p className="px-1 text-[10px] leading-relaxed text-muted-foreground">
                新增仓库会建立日志容器映射；填写 Jira 项目名称后会精确检索并绑定。
                日志或 Jira 未命中只提示，不影响仓库保存。
              </p>
            </div>
          </TabsContent>

          <TabsContent
            value="models"
            className="m-0 h-[calc(100vh-174px)] overflow-y-auto overscroll-contain px-6 py-5"
          >
            <div className="mb-4 flex items-center justify-between border-b border-border pb-3">
              <div>
                <h3 className="flex items-center gap-2 text-xs font-semibold">
                  <Code2 className="h-3.5 w-3.5 text-sky-700" />
                  修改代码
                </h3>
                <p className="mt-1 font-mono text-[10px] text-muted-foreground">
                  GitHub Copilot Cloud Agent
                </p>
              </div>
              <span className="rounded-full bg-sky-50 px-2 py-1 text-[10px] font-medium text-sky-700">
                {draft.environment.WORKER_CODE_MODEL ||
                  draft.codeModels.defaultModel}
              </span>
            </div>

            {draft.configurationWarnings.map((warning) => (
              <div
                key={warning}
                role="status"
                className="mb-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] leading-4 text-amber-800"
              >
                {warning}
              </div>
            ))}

            <div className="relative mb-3">
              <Search className="pointer-events-none absolute left-3 top-2.5 h-3.5 w-3.5 text-muted-foreground" />
              <Input
                name="model-search"
                aria-label="搜索代码模型"
                value={modelQuery}
                onChange={(event) => setModelQuery(event.target.value)}
                className="h-8 bg-white pl-9 text-xs"
                placeholder="搜索模型…"
                autoComplete="off"
                spellCheck={false}
              />
            </div>

            <div className="grid gap-2 sm:grid-cols-2">
              {filteredModels.map((model) => {
                const selected =
                  (draft.environment.WORKER_CODE_MODEL ||
                    draft.codeModels.defaultModel) === model;
                const meta = MODEL_META[model] ?? {
                  name: model,
                  detail: "仓库策略允许",
                };
                return (
                  <button
                    key={model}
                    type="button"
                    aria-pressed={selected}
                    onClick={() => updateEnvironment("WORKER_CODE_MODEL", model)}
                    className={`relative overflow-hidden rounded-lg border px-4 py-3 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 focus-visible:ring-offset-2 ${
                      selected
                        ? "border-sky-300 bg-sky-50/70"
                        : "border-border bg-white hover:border-sky-200 hover:bg-sky-50/30"
                    }`}
                  >
                    <span
                      className={`absolute inset-y-0 left-0 w-1 ${
                        selected ? "bg-sky-600" : "bg-transparent"
                      }`}
                    />
                    <span className="flex items-center justify-between gap-3">
                      <span className="text-xs font-semibold">{meta.name}</span>
                      {model === draft.codeModels.defaultModel && (
                        <span className="text-[9px] font-medium text-sky-700">
                          默认
                        </span>
                      )}
                    </span>
                    <span className="mt-1 block text-[10px] text-muted-foreground">
                      {meta.detail}
                    </span>
                    <span className="mt-2 block font-mono text-[9px] text-muted-foreground/80">
                      {model}
                    </span>
                  </button>
                );
              })}
            </div>

            {draft.codeModels.available.length === 0 && (
              <div className="rounded-lg border border-dashed border-border px-4 py-8 text-center text-xs text-muted-foreground">
                当前仓库策略没有共同允许的模型
              </div>
            )}
            {draft.codeModels.available.length > 0 &&
              filteredModels.length === 0 && (
                <div className="rounded-lg border border-dashed border-border px-4 py-8 text-center text-xs text-muted-foreground">
                  没有匹配的模型
                </div>
              )}
          </TabsContent>

          <TabsContent
            value="chat"
            className="m-0 h-[calc(100vh-174px)] overflow-y-auto overscroll-contain px-6 py-4"
          >
            <section>
              <h3 className="mb-2 flex items-center gap-2 text-xs font-semibold">
                <MessagesSquare className="h-3.5 w-3.5" />
                对话模型
              </h3>
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="sm:col-span-2">
                  <EnvInput environment={draft.environment} name="AI_BASE_URL" label="AI_BASE_URL" onChange={updateEnvironment} />
                </div>
                <EnvInput environment={draft.environment} name="AI_MODEL" label="AI_MODEL" onChange={updateEnvironment} />
                <EnvInput environment={draft.environment} name="AI_API_MODE" label="AI_API_MODE" onChange={updateEnvironment} />
                <EnvInput environment={draft.environment} name="AI_TIMEOUT_SECONDS" label="AI_TIMEOUT_SECONDS" type="number" onChange={updateEnvironment} />
                <EnvInput environment={draft.environment} name="AI_MAX_COMPLETION_TOKENS" label="AI_MAX_COMPLETION_TOKENS" type="number" onChange={updateEnvironment} />
                <div className="sm:col-span-2">
                  <EnvInput environment={draft.environment} name="AI_SAFETY_IDENTIFIER" label="AI_SAFETY_IDENTIFIER" onChange={updateEnvironment} />
                </div>
                <div className="rounded-md border border-border px-3 py-2 sm:col-span-2">
                  <EnvSwitch environment={draft.environment} name="AI_CHAT_ENABLED" label="启用对话" onChange={updateEnvironment} />
                  <EnvSwitch environment={draft.environment} name="AI_ASYNC_REPLY" label="异步回复" onChange={updateEnvironment} />
                </div>
              </div>
            </section>
          </TabsContent>
        </Tabs>

        <SheetFooter className="flex-row border-t border-border bg-zinc-50/80 px-6 py-3">
          <span className="sr-only" aria-live="polite">
            {saved ? "配置已保存" : error}
          </span>
          {error && (
            <span className="mr-auto max-w-72 text-[11px] leading-4 text-red-600">
              {error}
            </span>
          )}
          {!error && <span className="mr-auto" />}
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => void refresh()}
            disabled={loading || saving}
            className="text-xs text-muted-foreground"
          >
            <RotateCcw className="h-3.5 w-3.5" />
            重新读取
          </Button>
          <Button
            type="button"
            size="sm"
            onClick={() => void save()}
            disabled={
              loading ||
              saving ||
              draft.repositories.length === 0
            }
            className="min-w-24 bg-sky-700 text-xs hover:bg-sky-800"
          >
            {saved ? <Check className="h-3.5 w-3.5" /> : null}
            {saving ? "保存中…" : saved ? "已保存" : "保存"}
          </Button>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  );
}
