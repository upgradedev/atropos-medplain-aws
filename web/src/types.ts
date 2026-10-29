export type Role = "operator" | "reviewer" | "auditor" | "admin";
export type CaseOrigin = "prepared_demo" | "live_run" | "unknown";
export interface PreparedMetadata {
  version: string | number;
  prepared_at: string;
  source_commit: string;
  original_execution_id: string;
  [key: string]: unknown;
}
export type Status =
  | "queued"
  | "processing"
  | "review_required"
  | "released"
  | "failed"
  | "rejected";
export const STATUSES: Status[] = [
  "queued",
  "processing",
  "review_required",
  "released",
  "failed",
  "rejected",
];
export const label = (value: string) =>
  value.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());
export interface Session {
  session_id: string;
  role: Role;
  roles: Role[];
  demo: boolean;
  expires_at?: string | number;
}
export interface Facility {
  id: string;
  name: string;
  city: string;
  lat: number;
  lon: number;
  synthetic: boolean;
}
export interface Bootstrap {
  session: Session;
  workspaces: { id: string; name: string }[];
  facilities: Facility[];
  packets: {
    id: string;
    title: string;
    summary: string;
    stress_available: boolean;
  }[];
  runtime: {
    model_mode: string;
    transport_mode: string;
    commit: string;
    storage: string;
  };
  limits: { max_cases: number; daily_jobs?: number; daily_reset?: string };
  collection?: {
    mode: "prepared_demo" | "empty";
    initialized: boolean;
    prepared_count: number;
    live_count: number;
    seed_version: string | null;
  };
}
export interface IntakeTemplate {
  profile: string;
  source_text: string;
  limits: {
    max_source_chars: number;
    max_lines: number;
    max_medications: number;
    max_labs: number;
  };
  format_hints: string[];
  privacy_notice: string;
}
export interface SourceIntake {
  profile: string;
  source_text: string;
  source_sha256: string;
  ledger_sha256: string;
}
export type DocumentKind = "home_list" | "discharge" | "addendum" | "caregiver_note" | "lab_report";
export interface ReconciliationDocument {
  document_id: string;
  kind: DocumentKind;
  issued_at: string;
  effective_at: string | null;
  text: string;
}
export interface ReconciliationDossier {
  profile: "medplain-reconciliation-v1";
  patient_alias: string;
  language: "en" | "el";
  caregiver_concerns: string;
  documents: ReconciliationDocument[];
}
export interface ReconciliationExamples {
  profile: string;
  examples: { id: string; title: string; description: string; dossier: ReconciliationDossier }[];
  limits?: Record<string, number>;
}
export interface CaseSummary {
  source_profile?: string;
  parent_case_id?: string | null;
  origin?: CaseOrigin;
  prepared?: PreparedMetadata | null;
  source_kind?: "imported_note" | "sample";
  source_sha256?: string | null;
  id: string;
  packet_id: string;
  packet_title: string;
  facility_id: string;
  status: Status;
  version: number;
  generation: number;
  created_at: string;
  updated_at: string;
  duration_ms: number | null;
  correction_count: number;
  source_hash: string;
  artifact_hash: string | null;
  patient_url: string | null;
  error: string | null;
}
export interface Medication {
  id?: string;
  medication_id?: string;
  name: string;
  dose: string;
  unit: string;
  frequency: string;
  status: string;
  instruction?: string;
  text?: string;
}
export interface Lab {
  id?: string;
  lab_id?: string;
  name: string;
  value: string;
  unit: string;
  low: string;
  high: string;
  status?: string;
  text?: string;
}
export interface Explanation {
  summary: string;
  medications: Medication[];
  labs: Lab[];
  follow_up: string[];
  red_flags: string[];
}
export interface Packet {
  packet_id: string;
  title: string;
  subtitle: string;
  source_text: string;
  summary: string;
  medications: Medication[];
  labs: Lab[];
  follow_up: string[];
  red_flags: string[];
  fhir: unknown;
}
export interface Receipt {
  sequence: number;
  source_hash: string;
  artifact_hash: string;
  previous_hash: string;
  verdict: string;
  discrepancies: string[];
  timestamp: string;
  hash: string;
  [key: string]: unknown;
}
export interface Result {
  status: string;
  explanation: Explanation | null;
  explanation_text: string | null;
  trace: { agent?: string; node?: string; status: string; message: string }[];
  receipts: Receipt[];
  interactions: {
    title: string;
    message: string;
    severity: string;
    source: string;
    scope: string;
  }[];
  model_mode: string;
  correction_count: number;
  intake_validation?: {
    parser_verified?: boolean;
    [key: string]: unknown;
  } | null;
  runtime: {
    nodes_executed?: string[];
    tools_executed?: string[];
    orchestrator?: string;
    graph_status?: string;
    [key: string]: unknown;
  };
}
export interface CaseDetail extends CaseSummary {
  source: Packet;
  intake?: SourceIntake | null;
  result: Result | null;
  events: { id: string; type: string; at: string; message: string }[];
  review: {
    decision?: string;
    comment?: string;
    [key: string]: unknown;
  } | null;
}
export interface CaseList {
  items: CaseSummary[];
  total: number;
  next_cursor: string | null;
  as_of: string;
}
export interface Overview {
  counts: Record<Status | "total", number>;
  outcomes: { label: string; value: number }[];
  durations: { id: string; label: string; duration_ms: number }[];
  as_of: string;
  scope: string;
  origin_counts?: { prepared_demo: number; live_run: number; unknown?: number };
  by_facility?: {
    facility_id: string;
    name: string;
    city: string;
    count: number;
  }[];
}
export const isActive = (status: Status) =>
  status === "queued" || status === "processing";
export const clock = (date?: string) =>
  date
    ? new Date(date).toLocaleString([], {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "Not recorded";
export const duration = (ms: number | null) =>
  ms === null ? "Not measured" : `${(ms / 1000).toFixed(1)}s`;
