export type LogMonitorStatus =
  | "ok"
  | "not_configured"
  | "no_hmac_key"
  | "no_credentials"
  | "error";

export type NamedCount = {
  name: string;
  errors: number;
};

export type IncidentMember = {
  timestamp: string;
  level: string;
  summary: string;
  traceRef: string;
};

export type IncidentView = {
  incidentRef: string;
  eventCount: number;
  firstSeenAt: string;
  lastSeenAt: string;
  strategy: string;
  services: string[];
  affectedEndpoints: string[];
  affectedUserCount: number | null;
  summary: string;
  members: IncidentMember[];
};

export type AutomationDispatch = {
  incidentRef: string;
  result:
    | "created"
    | "already_dispatched"
    | "over_budget"
    | "skipped"
    | "failed";
  taskId?: string;
  taskStatus?: string;
  matchedRepository?: string;
  detail?: string;
};

export type AutomationInfo = {
  rules: {
    enabled: boolean;
    minGroupEvents: number;
    maxTasksPerScan: number;
  };
  overThreshold: number;
  dispatched: AutomationDispatch[];
};

export type LogMonitorScan = {
  status: LogMonitorStatus;
  detail?: string;
  configure?: string;
  scannedAt?: string;
  indexPattern?: string;
  fetchSize?: number;
  projectsScanned?: number;
  namespaces?: NamedCount[];
  services?: NamedCount[];
  errorEvents?: number;
  blockedEvents?: number;
  skippedNonError?: number;
  incidentGroups?: number;
  window?: { from: string | null; to: string | null };
  incidents?: IncidentView[];
  automation?: AutomationInfo;
};
