export type JiraDecision =
  | "RESOLVED"
  | "NEEDS_CONTEXT"
  | "BLOCKED_SENSITIVE"
  | "WATERMARK_INIT";

export type JiraDispatchResult = {
  result: "created" | "held" | "shadow" | "over_budget" | "skipped" | "failed";
  taskId?: string;
  taskStatus?: string;
  detail?: string;
};

export type JiraScannedIssue = {
  ts: string;
  issue: string;
  project: string;
  summary: string;
  excerpt?: string;
  url?: string;
  severity: string;
  decision: JiraDecision;
  repository: string;
  repositories?: string[];
  basis: string;
  confidence: number;
  dispatch?: JiraDispatchResult;
  manual?: boolean;
};

export type JiraProjectView = {
  key: string;
  enabled: boolean;
  autoDispatch: boolean;
  issueTypes: string[];
  repositories: string[];
  maxDispatchPerPoll: number;
};

export type JiraMonitorStatus = {
  status: "ok" | "config_error" | "error";
  detail?: string;
  issues?: JiraScannedIssue[];
  projects?: JiraProjectView[];
  watermarks?: Record<string, string>;
  counts?: Record<string, number>;
  autoScan?: {
    lastRunAt: string | null;
    lastResult: string | null;
    lastError: string | null;
  };
  servedAt?: string;
  lastScan?: {
    newIssues: number;
    decisions: JiraScannedIssue[];
  };
};
