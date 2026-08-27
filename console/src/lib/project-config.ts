export type RepositorySetting = {
  repository: string;
  keywords: string[];
  dependencies: string[];
  jiraProjectName: string;
  jiraProjectKey: string;
  jiraBindingStatus: "bound" | "not_found" | "unavailable" | "unconfigured";
  jiraBindingDetail: string;
};

export type ProjectSettings = {
  activeAccount: string;
  accounts: string[];
  codeModels: {
    available: string[];
    defaultModel: string;
  };
  environment: Record<string, string>;
  secretConfigured: Record<string, boolean>;
  repositories: RepositorySetting[];
  repositoryWarnings: Record<string, string>;
  configurationWarnings: string[];
  revision: string;
};

export type SettingsSaveInput = {
  activeAccount: string;
  environment: Record<string, string>;
  secrets: Record<string, string>;
  repositories: RepositorySetting[];
  revision: string;
};

export type JiraConnectInput = {
  baseUrl: string;
  username: string;
  password: string;
  revision: string;
};

export type JiraConnectResult = {
  status: "connected";
  detail: string;
  settings: ProjectSettings;
};

export const DEFAULT_PROJECT_SETTINGS: ProjectSettings = {
  activeAccount: "",
  accounts: [],
  codeModels: {
    available: [],
    defaultModel: "",
  },
  environment: {},
  secretConfigured: {},
  repositories: [],
  repositoryWarnings: {},
  configurationWarnings: [],
  revision: "",
};

export function repositoryDisplayName(repository: string): string {
  return repository.split("/", 2)[1] || repository;
}
