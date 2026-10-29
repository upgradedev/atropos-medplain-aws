import { useEffect, useState } from "react";
import { Hash, State } from "./ui";

const PROFILE = "medplain-reconciliation-v1";
type Arm = Record<string, unknown>;
interface Comparison {
  source_commit: string;
  profile: string;
  mode: "fixture" | "bedrock";
  arms: { strands: Arm; baseline: Arm };
  report_sha256: string;
  corpus_sha256?: string;
  ruler_sha256?: string;
  scope?: string;
  limitations?: string[];
  corpus?: { version?: string; count?: number; sha256?: string };
  ruler?: { version?: string; sha256?: string };
}
type LiveComparison = { status: "not_measured"; source_commit: string; reason: string } | (Comparison & { status: "published"; report_url: string; manifest_sha256: string });
const numeric = (value: unknown) => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
const count = (value: unknown) => numeric(value)?.toLocaleString() ?? "Not reported";
const fraction = (value: unknown, eligible: unknown) => numeric(value) !== null && numeric(eligible) !== null ? `${count(value)} / ${count(eligible)}` : "Not reported";
const group = (value: unknown): Arm => typeof value === "object" && value !== null && !Array.isArray(value) ? value as Arm : {};
const percentage = (value: unknown) => numeric(value) !== null && (value as number) <= 1 ? `${((value as number) * 100).toFixed(1)}%` : "Not reported";

function validComparison(value: Comparison, expectedCommit: string, mode: string) {
  return value && value.profile === PROFILE && value.mode === mode && value.source_commit === expectedCommit && value.arms?.strands && value.arms?.baseline && /^[a-f0-9]{64}$/.test(value.report_sha256);
}

function ResultTable({ report, caption }: { report: Comparison; caption: string }) {
  const rows: { label: string; value: (arm: Arm) => string }[] = [
    { label: "Cases evaluated", value: (arm) => count(arm.case_count) },
    { label: "Valid outcomes / total", value: (arm) => fraction(arm.valid_outcome_count, arm.case_count) },
    { label: "Verified clarification cards", value: (arm) => count(arm.verified_count) },
    { label: "Workflow refusals", value: (arm) => count(arm.workflow_refused_count) },
    { label: "Runtime errors", value: (arm) => count(arm.runtime_error_count) },
    { label: "Not executed", value: (arm) => count(arm.not_executed_count) },
    { label: "Complete source coverage / total", value: (arm) => fraction(arm.source_coverage_count, arm.case_count) },
    { label: "Exact original citations / total", value: (arm) => fraction(arm.source_citations_count, arm.case_count) },
    { label: "Exact supported atoms / total", value: (arm) => fraction(arm.supported_atoms_exact_count, arm.case_count) },
    { label: "Regimen properly withheld / total", value: (arm) => fraction(arm.proper_withholding_count, arm.case_count) },
    { label: "Corpus question-focus precision", value: (arm) => percentage(group(arm.question_focus).precision) },
    { label: "Corpus question-focus recall", value: (arm) => percentage(group(arm.question_focus).recall) },
    { label: "Question focus TP / FP / FN", value: (arm) => { const focus = group(arm.question_focus); return [focus.true_positive, focus.false_positive, focus.false_negative].every((value) => numeric(value) !== null) ? `${count(focus.true_positive)} / ${count(focus.false_positive)} / ${count(focus.false_negative)}` : "Not reported"; } },
    { label: "Selected-label review TP / expected", value: (arm) => { const selected = group(arm.selected_interaction_review); return fraction(selected.true_positive, selected.expected); } },
    { label: "Selected-label review false positives", value: (arm) => count(group(arm.selected_interaction_review).false_positive) },
  ];
  return <><div className="evaluation-table-scroll"><table className="evaluation-table"><caption>{caption}</caption><thead><tr><th scope="col">Measure</th><th scope="col">Strands</th><th scope="col">Complete deterministic baseline</th></tr></thead><tbody>{rows.map((row) => <tr key={row.label}><th scope="row">{row.label}</th><td>{row.value(report.arms.strands)}</td><td>{row.value(report.arms.baseline)}</td></tr>)}<tr><th scope="row">Fresh tool trajectories / eligible</th><td>{fraction(report.arms.strands.fresh_trajectory_count, report.arms.strands.trajectory_eligible_count)}</td><td>Not applicable</td></tr></tbody></table></div><p className="muted">Missing measurements are shown as Not reported. Question-focus scores compare bounded question codes and citations with this synthetic corpus ruler. Counts are not evidence of clinical benefit, comprehension or comprehensive interaction coverage.</p><details><summary>Comparison provenance and limits</summary><p className="mono break">Source commit: {report.source_commit}</p><Hash title="Report SHA-256" value={report.report_sha256} /><Hash title="Corpus SHA-256" value={report.corpus_sha256 || report.corpus?.sha256} /><Hash title="Ruler SHA-256" value={report.ruler_sha256 || report.ruler?.sha256} />{report.scope ? <p>{report.scope}</p> : null}{report.limitations?.length ? <ul>{report.limitations.map((item) => <li key={item}>{item}</li>)}</ul> : null}</details></>;
}

export default function ReconciliationEvaluationPanel({ expectedCommit }: { expectedCommit: string }) {
  const [fixture, setFixture] = useState<Comparison | null>(null);
  const [fixtureError, setFixtureError] = useState("");
  const [live, setLive] = useState<LiveComparison | null>(null);
  const [liveError, setLiveError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setFixture(null); setFixtureError(""); setLive(null); setLiveError("");
    const timer = window.setTimeout(() => controller.abort(), 25000);
    const read = async (path: string) => {
      const response = await fetch(path, { headers: { Accept: "application/json" }, signal: controller.signal });
      if (!response.ok) throw new Error(`Published comparison unavailable (HTTP ${response.status}). No result is inferred.`);
      return response.json();
    };
    void Promise.allSettled([
      read("/api/reconciliation-evaluations").then((value: Comparison) => {
        if (!validComparison(value, expectedCommit, "fixture")) throw new Error("The offline comparison is not bound to this source and profile. No result is inferred.");
        if (active) setFixture(value);
      }).catch((cause) => { if (active) setFixtureError(controller.signal.aborted ? "The offline comparison did not respond in time." : cause instanceof Error ? cause.message : "The offline comparison is unavailable."); }),
      read("/api/reconciliation-evaluations/live").then((value: LiveComparison) => {
        if (!value || value.source_commit !== expectedCommit || (value.status !== "not_measured" && (value.status !== "published" || !validComparison(value, expectedCommit, "bedrock") || !/^[a-f0-9]{64}$/.test(value.manifest_sha256) || !value.report_url.startsWith("https://")))) throw new Error("The Bedrock measurement is not published for this source. No result is inferred.");
        if (active) setLive(value);
      }).catch((cause) => { if (active) setLiveError(controller.signal.aborted ? "The Bedrock measurement status did not respond in time." : cause instanceof Error ? cause.message : "The Bedrock measurement status is unavailable."); }),
    ]).finally(() => window.clearTimeout(timer));
    return () => { active = false; controller.abort(); window.clearTimeout(timer); };
  }, [expectedCommit, revision]);
  const retry = () => setRevision((value) => value + 1);
  return <section className="card settings-card evaluation-panel" data-testid="reconciliation-evaluation-panel"><div className="eyebrow">MULTISOURCE RECONCILIATION EVIDENCE</div><h2>Complex discharge comparison</h2><p className="muted">A separate profile and corpus from the simple discharge-note comparison. Both arms receive the same captured sources, deterministic safety gate and clarification renderer.</p><p className="mono break">Source commit: {expectedCommit}</p><section data-testid="reconciliation-fixture-evidence"><h3>Offline fixture measurement</h3>{fixtureError ? <State kind="error" title="Offline comparison unavailable" retry={retry}>{fixtureError}</State> : !fixture ? <State kind="loading" title="Reading offline comparison" /> : <><p className="notice">Actual SDK execution with a fixture model in CI. The fixture planner replays the deterministic baseline selection. This checks integration and safety plumbing, not model quality, Bedrock performance or patient benefit.</p><ResultTable report={fixture} caption="Offline complex-discharge corpus outcomes" /><a className="text-button" href="/api/reconciliation-evaluations" download="medplain-reconciliation-fixture.json">Download full offline JSON report</a></>}</section><section data-testid="reconciliation-live-evidence"><h3>Actual Bedrock measurement</h3>{liveError ? <State kind="error" title="Bedrock comparison unavailable" retry={retry}>{liveError}</State> : !live ? <State kind="loading" title="Reading Bedrock measurement status" /> : live.status === "not_measured" ? <div className="notice"><strong>Not measured for this source</strong><p>{live.reason}</p><p>No Bedrock outcomes are inferred from fixture runs or earlier versions.</p></div> : <><p className="notice">Published actual Bedrock runs on the same synthetic corpus. Failures remain in the counts. These results do not establish clinical benefit.</p><ResultTable report={live} caption="Published Bedrock complex-discharge corpus outcomes" /><Hash title="Publication manifest SHA-256" value={live.manifest_sha256} /><a className="text-button" href={live.report_url} target="_blank" rel="noreferrer">Inspect full published Bedrock JSON report</a></>}</section></section>;
}
