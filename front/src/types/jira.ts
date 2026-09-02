export type JiraDecision =
  | "RESOLVED"
  | "NEEDS_CONTEXT"
  | "BLOCKED_SENSITIVE"
  | "NOT_OPEN"
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
  projectName?: string;
  summary: string;
  excerpt?: string;
  url?: string;
  severity: string;
  decision: JiraDecision;
  repository: string;
  basis: string;
  confidence: number;
  workflowStatus?: string;
  workflowStatusCategory?: string;
  automationEligible?: boolean;
  dispatch?: JiraDispatchResult;
  manual?: boolean;
};

export type JiraProjectView = {
  key: string;
  name: string;
  enabled: boolean;
  autoDispatch: boolean;
  issueTypes: string[];
  repositories: string[];
  maxDispatchPerPoll: number;
};

export type JiraMonitorStatus = {
  status: "ok" | "config_error" | "error";
  detail?: string;
  workflowStatusError?: string | null;
  taskHistoryError?: string | null;
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
