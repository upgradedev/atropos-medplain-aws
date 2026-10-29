import type { CaseDetail } from "./types";
import { isActive, isReconciliationDossier } from "./types";
import { Hash } from "./ui";
import { ReconciliationSources } from "./ReconciliationView";

export default function SourceEvidence({ data }: { data: CaseDetail }) {
  if (isReconciliationDossier(data.source))
    return (
      <>
        <ReconciliationSources dossier={data.source} />
        <Hash title="Complete source packet SHA-256" value={data.source_hash} />
      </>
    );
  const imported = data.source_kind === "imported_note" || Boolean(data.intake);
  const parserVerified =
    data.result?.intake_validation?.parser_verified === true;
  return (
    <>
      <section className="source-provenance" data-testid="source-provenance">
        <div className="eyebrow">SOURCE PROVENANCE</div>
        <h3>
          {imported ? "Imported synthetic note" : "Captured synthetic packet"}
        </h3>
        <p>
          {imported
            ? "The original note and its deterministic normalization are retained separately. A queued case does not prove successful agent extraction."
            : "This source snapshot is captured when the case is created. Verification and review apply to this exact source."}
        </p>
      </section>
      {imported ? (
        data.intake ? (
          <section className="source-intake" data-testid="source-intake">
            <div
              className={`notice ${parserVerified ? "notice-success" : ""}`}
              data-testid="parser-verification-status"
              role="status"
            >
              {parserVerified
                ? "Parser Agent extraction matched the independent source ledger. The patient artifact still requires the full safety gate."
                : isActive(data.status)
                  ? "Source format accepted; parser Agent pending. No verified patient explanation is available yet."
                  : "Parser Agent verification did not complete. No unchecked explanation can be released."}
            </div>
            <h3>Original submitted note</h3>
            <p className="muted break">Profile: {data.intake.profile}</p>
            <pre className="source-text" data-testid="original-source-note">
              {data.intake.source_text}
            </pre>
            <Hash
              title="Submitted note SHA-256"
              value={data.intake.source_sha256}
            />
            <Hash
              title="Independent extraction ledger SHA-256"
              value={data.intake.ledger_sha256}
            />
            {parserVerified && data.result?.intake_validation?.extraction ? (
              <details>
                <summary>Inspect verified extraction citations</summary>
                <p className="muted">
                  The Agent proposes clinical fields and line citations. Code
                  copies quotes and narrative from those original lines. The
                  complete extraction must match the independent ledger.
                </p>
                <pre>
                  {JSON.stringify(
                    data.result.intake_validation.extraction,
                    null,
                    2,
                  )}
                </pre>
              </details>
            ) : null}
            <h3>Deterministic normalization</h3>
            <p className="muted">
              Accepted input fields normalized before the model runs. This view
              alone is not evidence that the parser Agent passed.
            </p>
          </section>
        ) : (
          <div className="inline-alert" role="alert">
            Original note evidence is unavailable. Do not infer successful
            extraction from the normalized source alone.
          </div>
        )
      ) : null}
      <div className="eyebrow">SYNTHETIC DISCHARGE PACKET</div>
      <pre className="source-text">
        {data.source?.source_text || "The source is unavailable."}
      </pre>
      <Hash title="Source SHA-256" value={data.source_hash} />
      <details>
        <summary>Inspect constrained FHIR bundle</summary>
        <pre>{JSON.stringify(data.source?.fhir, null, 2)}</pre>
      </details>
    </>
  );
}
