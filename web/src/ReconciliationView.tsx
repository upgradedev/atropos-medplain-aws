import type {
  ClarificationArtifact,
  ReconciliationDossier,
  SourceStatement,
} from "./types";
import { DOCUMENT_LABELS } from "./ReconciliationForm";
import { Hash } from "./ui";
import ReferencePanel, {
  referenceCardId,
  referencePanelId,
} from "./ReferencePanel";

export const sourceElementId = (id: string) => `reconciliation-source-${id}`;

export function ReconciliationSources({
  dossier,
}: {
  dossier: ReconciliationDossier;
}) {
  return (
    <section
      className="reconciliation-sources"
      aria-label="Original reconciliation sources"
    >
      <div className="notice">
        These are original source records, not a reconciled prescription. A
        missing medicine in one list is not automatically a stop instruction.
      </div>
      <h3>Captured source packet</h3>
      <p>
        Invented alias: <strong>{dossier.patient_alias}</strong> · Clarification
        language: {dossier.language === "el" ? "Greek" : "English"}
      </p>
      <p>
        Original quotations are never translated. Source dates are displayed as
        recorded in UTC; the interface does not infer which document authorizes
        a change.
      </p>
      {dossier.caregiver_concerns ? (
        <section
          className="notice reconciliation-source-card"
          id={sourceElementId("caregiver-concerns")}
          tabIndex={-1}
        >
          <h4>Caregiver concerns</h4>
          <p className="muted">
            Issued and effective times were not recorded for this form field.
          </p>
          <p className="source-text">{dossier.caregiver_concerns}</p>
        </section>
      ) : null}
      {dossier.documents.map((doc) => (
        <section
          className="reconciliation-source-card"
          key={doc.document_id}
          id={sourceElementId(doc.document_id)}
          tabIndex={-1}
        >
          <h4>
            {DOCUMENT_LABELS[doc.kind]}{" "}
            <span className="mono">{doc.document_id}</span>
          </h4>
          <p className="muted">
            Issued: {doc.issued_at} · Effective:{" "}
            {doc.effective_at || "Not specified"}
          </p>
          <pre className="source-text">{doc.text}</pre>
        </section>
      ))}
    </section>
  );
}

function Quotation({
  statement,
  onSource,
}: {
  statement: SourceStatement;
  onSource?: (id: string) => void;
}) {
  return (
    <article
      className="reconciliation-quotation"
      data-testid="source-quotation"
    >
      <div className="eyebrow">ORIGINAL SOURCE QUOTATION</div>
      <p>
        <strong>
          {DOCUMENT_LABELS[statement.document_kind] || statement.document_kind}
        </strong>{" "}
        · {statement.citation.document_id}, line {statement.citation.line}
      </p>
      <blockquote>{statement.citation.quote}</blockquote>
      <p className="muted">
        Issued {statement.issued_at || "not recorded"}; effective{" "}
        {statement.effective_at || "not specified"}. A source quotation is not
        an authorized medication plan.
      </p>
      {onSource ? (
        <button
          type="button"
          className="text-button"
          onClick={() => onSource(statement.citation.document_id)}
        >
          Open original {statement.citation.document_id}
        </button>
      ) : null}
      <details>
        <summary>Source citation integrity</summary>
        <Hash
          title="Original document SHA-256"
          value={statement.citation.document_sha256}
        />
        <p>
          Recorded UTF-8 byte span: {statement.citation.byte_start} to{" "}
          {statement.citation.byte_end}.
        </p>
      </details>
    </article>
  );
}

function Citations({
  ids,
  statements,
  onSource,
}: {
  ids: string[];
  statements: SourceStatement[];
  onSource?: (id: string) => void;
}) {
  if (!ids.length)
    return (
      <p className="control-reason">
        No original source citation was recorded for this item.
      </p>
    );
  return (
    <details className="reconciliation-citations" data-testid="citation-group">
      <summary>Inspect cited original sources ({ids.length})</summary>
      {ids.map((id) => {
        const statement = statements.find((item) => item.statement_id === id);
        return statement ? (
          <Quotation key={id} statement={statement} onSource={onSource} />
        ) : (
          <p className="inline-alert" key={id}>
            The recorded citation {id} is unavailable. Do not infer an uncited
            clinical instruction.
          </p>
        );
      })}
    </details>
  );
}

export default function ClarificationView({
  artifact,
  onSource,
}: {
  artifact: ClarificationArtifact;
  onSource?: (id: string) => void;
}) {
  if (
    artifact.kind !== "clarification_plan" ||
    artifact.patient_regimen !== null ||
    !Array.isArray(artifact.source_statements) ||
    !Array.isArray(artifact.structural_issues) ||
    !Array.isArray(artifact.proposed_questions) ||
    !Array.isArray(artifact.teachback)
  ) {
    return (
      <div className="inline-alert" role="alert">
        The clarification card has an unsupported shape. No medication regimen
        can be shown.
      </div>
    );
  }
  const openReference = (id: string) => {
    if (!artifact.references) return;
    const panel = document.getElementById(
      referencePanelId(artifact.references.snapshot_sha256),
    ) as HTMLDetailsElement | null;
    if (panel) panel.open = true;
    const target = document.getElementById(referenceCardId(id)) || panel;
    target?.scrollIntoView({ block: "nearest" });
    target?.focus();
  };
  return (
    <section className="clarification-card" data-testid="clarification-card">
      <div
        className="regimen-withheld"
        role="status"
        data-testid="regimen-withheld"
      >
        <strong>Medication regimen withheld</strong>
        <p>
          This is a clarification card, not a new prescription. Do not use
          source disagreements to choose or change a dose.
        </p>
      </div>
      <p className="notice" lang={artifact.language}>
        {artifact.notice}
      </p>
      <section aria-label="Confirmed structural checks">
        <h3>Confirmed structural checks</h3>
        <p className="muted">
          These checks compare recorded source relationships. They do not
          certify a clinical error or settle a medication decision.
        </p>
        {artifact.structural_issues.some(
          (issue) => issue.classification === "structural_check",
        ) ? (
          artifact.structural_issues
            .filter((issue) => issue.classification === "structural_check")
            .map((issue) => (
              <article
                className="reconciliation-issue"
                key={issue.issue_id}
                data-testid="structural-issue"
              >
                <span className="subtle-tag">Structural check</span>
                <h4>{issue.code.replaceAll("_", " ")}</h4>
                <p lang={artifact.language}>{issue.question_text}</p>
                {issue.requires_clarification ? (
                  <p className="control-reason">
                    Needs clarification from the care team. Releasing this
                    question does not resolve the source disagreement.
                  </p>
                ) : null}
                <Citations
                  ids={issue.statement_ids}
                  statements={artifact.source_statements}
                  onSource={onSource}
                />
              </article>
            ))
        ) : (
          <p>
            No structural issue was recorded. This does not establish that a
            regimen is clinically appropriate.
          </p>
        )}
      </section>
      {artifact.structural_issues.some(
        (issue) => issue.classification === "selected_label_reference",
      ) ? (
        <section aria-label="Selected label reference checks">
          <h3>Selected label reference checks</h3>
          <p className="muted">
            A bounded reference prompt about possible exposure. It does not
            establish concurrent use, a complete interaction screen, or a
            medication change.
          </p>
          {artifact.structural_issues
            .filter(
              (issue) => issue.classification === "selected_label_reference",
            )
            .map((issue) => (
              <article
                className="reconciliation-issue"
                key={issue.issue_id}
                data-testid="selected-label-reference"
              >
                <span className="subtle-tag">Selected label reference</span>
                <p lang={artifact.language}>{issue.question_text}</p>
                {issue.scope ? (
                  <p className="control-reason">{issue.scope}</p>
                ) : null}
                <p className="muted">
                  Reference identifiers:{" "}
                  {issue.reference_ids?.join(", ") || "None recorded"}
                </p>
                {artifact.references ? (
                  issue.reference_ids?.map((id) => (
                    <button
                      type="button"
                      className="text-button"
                      key={id}
                      onClick={() => openReference(id)}
                    >
                      Inspect pinned reference {id}
                    </button>
                  ))
                ) : (
                  <p className="control-reason">
                    The artifact-bound reference snapshot is unavailable.
                  </p>
                )}
                <Citations
                  ids={issue.statement_ids}
                  statements={artifact.source_statements}
                  onSource={onSource}
                />
              </article>
            ))}
        </section>
      ) : null}
      <section aria-label="AI context proposals">
        <h3>AI context proposals</h3>
        <p className="muted">
          Questions for discussion, separate from confirmed structural checks.
          They are not verified medication errors.
        </p>
        {artifact.proposed_questions.length ? (
          artifact.proposed_questions.map((question, index) => (
            <article
              className="context-proposal"
              key={`${question.code}-${index}`}
              data-testid="context-proposal"
            >
              <span className="subtle-tag" lang={artifact.language}>
                {question.label}
              </span>
              <p lang={artifact.language}>{question.text}</p>
              <Citations
                ids={question.statement_ids}
                statements={artifact.source_statements}
                onSource={onSource}
              />
            </article>
          ))
        ) : (
          <p>No extra context question was proposed.</p>
        )}
      </section>
      {artifact.teachback.length ? (
        <section aria-label="Caregiver understanding questions">
          <h3>Questions to check your understanding</h3>
          {artifact.teachback.map((question, index) => (
            <article
              className="context-proposal"
              key={`${question.code}-${index}`}
            >
              <p className="muted" lang={artifact.language}>
                {question.label}
              </p>
              <p lang={artifact.language}>{question.text}</p>
              <Citations
                ids={question.statement_ids}
                statements={artifact.source_statements}
                onSource={onSource}
              />
            </article>
          ))}
        </section>
      ) : null}
      <details>
        <summary>All recorded source quotations</summary>
        {artifact.source_statements.map((statement) => (
          <Quotation
            key={statement.statement_id}
            statement={statement}
            onSource={onSource}
          />
        ))}
      </details>
      {artifact.references ? (
        <details>
          <summary>Bound reference snapshot</summary>
          <p>{artifact.references.scope}</p>
          <p>
            Selected reference identifiers:{" "}
            {artifact.references.reference_ids.join(", ") || "None recorded"}
          </p>
          <p>
            Interaction service: {artifact.references.interaction_service}.
            Identity snapshot status:{" "}
            {artifact.references.identity_snapshot_status}.
          </p>
          <Hash
            title="Bound reference snapshot SHA-256"
            value={artifact.references.snapshot_sha256}
          />
        </details>
      ) : null}
      {artifact.references ? (
        <ReferencePanel
          boundSnapshotSha={artifact.references.snapshot_sha256}
          selectedReferenceIds={artifact.references.reference_ids}
        />
      ) : null}
    </section>
  );
}
