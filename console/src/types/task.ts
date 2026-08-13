export type TaskStatus =
  | "PENDING"
  | "PROCESSING"
  | "TESTING"
  | "AWAITING_PR_REVIEW"
  | "COMPLETED"
  | "FAILED"
  | "NEEDS_CONTEXT";

export type SourceType = "NATURAL_LANGUAGE" | "LOG" | "JIRA";

export type LogIncident = {
  sourceReference: string;
  firstSeenAt: string | null;
  lastSeenAt: string | null;
  currentScanEventCount: number | null;
  historicalEventCount: number | null;
  incidentGroupCount: number | null;
  affectedEndpoints: string[];
  affectedUserCountMin: number | null;
  affectedUserCountMax: number | null;
  userIdentifierEventCount: number | null;
  historicalCountComplete: boolean | null;
  aggregationBasis: string | null;
};

export type Task = {
  id: string;
  sourceType: SourceType;
  executionMode: string;
  issueProfile: string | null;
  inputSummary: string;
  normalizedRequirement: string | null;
  status: TaskStatus;
  matchedRepository: string | null;
  routingBasis: string | null;
  routingConfidence: number | null;
  routingCandidates: string[];
  issueNumber: number | null;
  issueUrl: string | null;
  prNumber: number | null;
  prUrl: string | null;
  testSummary: string | null;
  blockedReason: string | null;
  retryCount: number;
  submittedBy: string | null;
  policyId: string | null;
  logIncident: LogIncident | null;
  createdAt: string;
  updatedAt: string;
};

export type TaskEvent = {
  id: number;
  eventType: string;
  fromStatus: TaskStatus | null;
  toStatus: TaskStatus | null;
  actorType: string;
  detail: string | null;
  createdAt: string;
};

export type TaskDetail = {
  task: Task;
  events: TaskEvent[];
};

export type CreateTaskInput = {
  sourceType: SourceType;
  input: string;
  logIncident?: Partial<LogIncident>;
};
