import { useEffect, useState } from "react";
import { api, message } from "./api";
import type { CaseDetail } from "./types";
import { isReconciliationDossier } from "./types";
import { ExplanationView } from "./CaseDrawer";
import { Icon, State } from "./ui";
import ClarificationView from "./ReconciliationView";
import ReadingControls from "./ReadingControls";
import TeachbackPanel from "./TeachbackPanel";
type PatientPayload = Pick<CaseDetail, "source" | "result"> & {
  case: Pick<
    CaseDetail,
    | "id"
    | "packet_title"
    | "status"
    | "source_hash"
    | "artifact_hash"
    | "updated_at"
    | "origin"
    | "prepared"
    | "source_profile"
  >;
};
export default function Patient({ share }: { share: string }) {
  const [data, setData] = useState<PatientPayload | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setError("");
    void api<PatientPayload>(
      `/patient/${encodeURIComponent(share)}`,
      undefined,
      controller.signal,
    )
      .then(setData)
      .catch((reason) => {
        if (!controller.signal.aborted) setError(message(reason));
      });
    return () => controller.abort();
  }, [share, revision]);
  return (
    <main className="patient-page">
      <header>
        <a href="/console/" className="brand">
          <span className="brand-mark">
            <Icon name="shield" />
          </span>
          <span>
            Atropos <strong>MedPlain</strong>
          </span>
        </a>
        <span className="subtle-tag">Read-only patient view</span>
      </header>
      {error ? (
        <State
          kind="error"
          title="Patient artifact unavailable"
          retry={() => setRevision((value) => value + 1)}
        >
          {error}
        </State>
      ) : !data ? (
        <State kind="loading" title="Reading released explanation" />
      ) : data.case.status !== "released" ||
        !data.result ||
        data.result.status !== "verified" ||
        (isReconciliationDossier(data.source)
          ? !data.result.artifact ||
            data.result.artifact_kind !== "clarification_plan"
          : !data.result.explanation) ? (
        <State kind="empty" title="No released explanation is available" />
      ) : (
        <article className="card patient-document">
          <div className="eyebrow">SYNTHETIC EXAMPLE ONLY</div>
          {data.case.origin === "prepared_demo" ? (
            <p
              className="prepared-notice"
              data-testid="prepared-patient-notice"
            >
              Prepared synthetic example. This released artifact was prepared
              with an offline fixture model; it is not a fresh Bedrock run.
            </p>
          ) : data.case.origin === "live_run" ? (
            <p className="run-origin-note">
              {data.result.model_mode === "bedrock"
                ? "Released from a new Amazon Bedrock verification run."
                : data.result.model_mode === "fixture"
                  ? "Released from a new offline fixture verification run."
                  : "Released from a new verification run; its model mode was not recorded."}
            </p>
          ) : (
            <p className="run-origin-note">
              The origin of this legacy artifact was not recorded.
            </p>
          )}
          <h1>
            {data.case.packet_title ||
              (!isReconciliationDossier(data.source)
                ? data.source.title
                : "Your source-cited clarification card") ||
              "Your discharge explanation"}
          </h1>
          <p className="notice notice-success">
            <Icon name="shield" />
            {data.result.artifact_kind === "clarification_plan"
              ? "This released clarification card preserves source quotations and question citations. It is not a reconciled prescription. Medication decisions remain with your care team."
              : "The released explanation matched the supported source facts. This checks fidelity, not whether the original care plan is clinically appropriate."}
          </p>
          <ReadingControls
            text={data.result.explanation_text}
            language={data.result.artifact?.language || "en"}
          />
          {data.result.artifact_kind === "clarification_plan" &&
          data.result.artifact ? (
            <ClarificationView artifact={data.result.artifact} />
          ) : data.result.explanation ? (
            <ExplanationView explanation={data.result.explanation} />
          ) : null}
          {isReconciliationDossier(data.source) ? (
            <TeachbackPanel key={share} share={share} />
          ) : null}
          <p className="muted">
            No real patient data is accepted. Ask your clinician or pharmacist
            about your own medicines or symptoms.
          </p>
        </article>
      )}
    </main>
  );
}
