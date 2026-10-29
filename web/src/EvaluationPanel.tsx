import { useEffect, useState } from "react";
import { clock } from "./types";
import { State } from "./ui";

interface EvaluationArm {
  case_count?: number;
  verified_count?: number;
  refused_count?: number;
  runtime_error_count?: number;
  extraction_exact_count?: number;
  extraction_eligible_count?: number;
  field_fidelity_passed?: number;
  field_fidelity_eligible_count?: number;
  gate_false_accept_count?: number;
  gate_mutation_count?: number;
}
interface EvaluationReport {
  schema_version: number;
  source_commit: string;
  generated_at: string;
  mode: "fixture" | "bedrock";
  corpus: { version: string; count: number };
  arms: { strands: EvaluationArm; baseline: EvaluationArm };
  limitations: string[];
  report_sha256: string;
}
const count = (value: number | undefined) =>
  typeof value === "number" ? value.toLocaleString() : "Not reported";
const fraction = (value: number | undefined, eligible: number | undefined) =>
  typeof value === "number" && typeof eligible === "number"
    ? `${count(value)} / ${count(eligible)}`
    : "Not reported";

export default function EvaluationPanel() {
  const [report, setReport] = useState<EvaluationReport | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 25000);
    setError("");
    setReport(null);
    void fetch("/api/evaluations", {
      headers: { Accept: "application/json" },
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok)
          throw new Error(`Evaluation report unavailable (HTTP ${response.status}). No results are inferred.`);
        const value = await response.json() as EvaluationReport;
        if (value.schema_version !== 1 || !value.arms?.strands || !value.arms?.baseline || !value.corpus || !["fixture", "bedrock"].includes(value.mode))
          throw new Error("The evaluation report format is unavailable. No results are inferred.");
        if (active) setReport(value);
      })
      .catch((cause) => {
        if (active)
          setError(controller.signal.aborted
            ? "The evaluation report did not respond in time. Retry this read."
            : cause instanceof Error ? cause.message : "Evaluation report unavailable.");
      })
      .finally(() => window.clearTimeout(timer));
    return () => {
      active = false;
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [revision]);
  const rows: { label: string; value: (arm: EvaluationArm) => string }[] = [
    { label: "Cases evaluated", value: (arm) => count(arm.case_count) },
    { label: "Verified outputs", value: (arm) => count(arm.verified_count) },
    { label: "Exact extraction / eligible", value: (arm) => fraction(arm.extraction_exact_count, arm.extraction_eligible_count) },
    { label: "Source fidelity / eligible", value: (arm) => fraction(arm.field_fidelity_passed, arm.field_fidelity_eligible_count) },
    { label: "False accepts / gate mutations", value: (arm) => fraction(arm.gate_false_accept_count, arm.gate_mutation_count) },
    { label: "Refused outputs", value: (arm) => count(arm.refused_count) },
    { label: "Runtime errors", value: (arm) => count(arm.runtime_error_count) },
  ];
  return (
    <section className="card settings-card evaluation-panel" data-testid="evaluation-panel">
      <div className="eyebrow">PUBLIC EVALUATION EVIDENCE</div>
      <h2>Strands and deterministic baseline</h2>
      {error ? (
        <State kind="error" title="Comparison unavailable" retry={() => setRevision((value) => value + 1)}>
          {error}
        </State>
      ) : !report ? (
        <State kind="loading" title="Reading published evaluation report" />
      ) : (
        <>
          <p className="notice" data-testid="evaluation-scope">
            {report.mode === "fixture"
              ? "Offline SDK comparison in CI; not Bedrock performance or clinical benefit."
              : "Recorded Bedrock comparison on a synthetic corpus; not evidence of clinical benefit."}
          </p>
          <p className="muted">Counts describe the published synthetic corpus. Missing measurements are shown as not reported.</p>
          <div className="evaluation-table-scroll">
            <table className="evaluation-table">
              <caption>Published synthetic evaluation outcomes</caption>
              <thead>
                <tr><th scope="col">Measure</th><th scope="col">Strands</th><th scope="col">Baseline</th></tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.label}>
                    <th scope="row">{row.label}</th>
                    <td>{row.value(report.arms.strands)}</td>
                    <td>{row.value(report.arms.baseline)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <dl>
            <div><dt>Corpus</dt><dd>{report.corpus.version} · {count(report.corpus.count)} cases</dd></div>
            <div><dt>Generated</dt><dd>{clock(report.generated_at)}</dd></div>
            <div><dt>Source commit</dt><dd className="mono break">{report.source_commit}</dd></div>
            <div><dt>Report SHA-256</dt><dd className="mono break">{report.report_sha256}</dd></div>
          </dl>
          {Array.isArray(report.limitations) && report.limitations.length ? (
            <details>
              <summary>Evaluation limits</summary>
              <ul className="evaluation-limitations">
                {report.limitations.map((item) => <li key={item}>{item}</li>)}
              </ul>
            </details>
          ) : null}
          <a className="text-button" href="/api/evaluations" download="medplain-evaluation.json">Download full JSON report</a>
        </>
      )}
    </section>
  );
}
