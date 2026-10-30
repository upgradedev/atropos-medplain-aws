export interface DemoActor {
  id: string;
  label: string;
  kind: string;
  demo: true;
}
export interface ActorDirectory {
  actors: DemoActor[];
  selected_actor_id: string | null;
  actor_version: number;
  demo_notice: string;
}
export interface CareflowEvent {
  id: string;
  type: string;
  at: string;
  message: string;
  actor_id: string | null;
}
export interface ClarificationTask {
  id: string;
  section: "structural_issues" | "proposed_questions" | "teachback";
  code: string;
  statement_ids: string[];
  prompt: string;
  status:
    | "open"
    | "assigned"
    | "accepted"
    | "answered"
    | "revision_linked"
    | "closed";
  assignee_id: string | null;
  due_at: string | null;
  accepted_at: string | null;
  answer: {
    text: string;
    actor_id: string;
    at: string;
    authoritative: false;
  } | null;
  revision: {
    case_id: string;
    source_hash: string;
    artifact_hash: string;
    generation: number;
  } | null;
  closure: {
    actor_id: string;
    at: string;
    revision_case_id: string;
    revision_source_hash: string;
    revision_artifact_hash: string;
  } | null;
  events: CareflowEvent[];
}
export interface CandidateRevision {
  case_id: string;
  case_version: number;
  source_hash: string;
  artifact_hash: string;
  status: string;
  generation: number;
  patient_alias: string;
}
export interface TeachbackPrompt {
  prompt_id: string;
  code: "teachback";
  statement_ids: string[];
  text: string;
}
export interface TeachbackAssessment {
  status: "provisional" | "refused";
  requires_reviewer: true;
  patient_regimen: null;
  assessment: {
    covered_statement_ids: string[];
    missing_statement_ids: string[];
    misunderstood_statement_ids: string[];
    evidence_ids: string[];
    follow_up_code:
      | "repeat_in_own_words"
      | "review_misunderstanding"
      | "review_response";
  } | null;
  follow_up: {
    code: string;
    text: string;
    statement_ids: string[];
    source_quotes: { statement_id: string; quote: string }[];
  } | null;
}
export interface TeachbackAttempt {
  id: string;
  status: "queued" | "processing" | "assessed" | "failed" | "reviewed";
  created_at: string;
  response_text: string;
  response_sha256: string;
  assessment: TeachbackAssessment | null;
  error: string | null;
  review: {
    decision: "acknowledged" | "follow_up";
    actor_id?: string;
    at: string;
    comment?: string;
  } | null;
}
export interface CareflowView {
  case_id: string;
  case_version: number;
  source_hash: string;
  artifact_hash: string;
  generation: number;
  demo: true;
  notice: string;
  selected_actor_id: string | null;
  tasks: ClarificationTask[];
  teachback: {
    enabled: boolean;
    prompt: TeachbackPrompt | null;
    attempts: TeachbackAttempt[];
  };
  candidate_revisions: CandidateRevision[];
}
export interface PatientTeachbackView {
  case_id: string;
  case_version: number;
  source_hash: string;
  artifact_hash: string;
  prompt: TeachbackPrompt;
  attempts: TeachbackAttempt[];
  demo: true;
  notice: string;
}
export interface EvidenceResult {
  status: "supported" | "no_support";
  query: {
    query: string;
    language: "en" | "el";
    population: "adult" | "all";
    jurisdiction: "US" | "EU" | "GR" | "any";
    as_of: string;
    limit: number;
  };
  corpus: {
    version: string;
    sha256: string;
    scope: string;
    snapshot_date: string;
  };
  entries: {
    evidence_id: string;
    source_id: string;
    title: string;
    uri: string;
    publisher: string;
    source_version: string;
    document_sha256: string;
    section: string;
    population: string;
    jurisdiction: string;
    language: string;
    valid_from: string;
    valid_until: string | null;
    span: { start: number; end: number; quote: string; sha256: string };
    score: number;
  }[];
}
export const careflowBinding = (view: CareflowView) => ({
  expected_version: view.case_version,
  source_hash: view.source_hash,
  artifact_hash: view.artifact_hash,
  idempotency_key: crypto.randomUUID(),
});
