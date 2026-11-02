import React from "react";
import { createRoot } from "react-dom/client";
import ClarificationView, { ReconciliationSources, sourceElementId } from "./ReconciliationView";
import { ReadingPreferences } from "./ReadingControls";
import type { ClarificationArtifact, ReconciliationDossier } from "./types";
import replay from "./motion-replay-data.json";
import "./styles.css";

const artifact = replay.artifact as ClarificationArtifact;
const dossier = replay.source as ReconciliationDossier;
if (replay.schema !== "medplain-historical-english-replay-v1" ||
    replay.provenance.recorded_status !== "review_required" ||
    artifact.language !== "en" || dossier.language !== "en" ||
    artifact.patient_regimen !== null) throw new Error("Unsupported historical replay projection");
const onSource = (id: string) => {
  const target = document.getElementById(sourceElementId(id));
  target?.scrollIntoView({ block: "center", behavior: "auto" });
  target?.focus({ preventScroll: true });
};
function HistoricalReplay() {
  return <ReadingPreferences>
    <style>{`
      html { scroll-padding-top: 150px; }
      .motion-replay-band { position:sticky; top:0; z-index:100; padding:14px 28px; background:#111b29; border-bottom:2px solid #f1c27b; box-shadow:0 10px 30px #0008; }
      .motion-replay-band strong { display:block; font-size:19px; color:#f1c27b; letter-spacing:.02em; }
      .motion-replay-band p { margin:5px 0 0; font-size:14px; line-height:1.5; }
      .motion-replay-shell { max-width:1440px; padding:28px; margin:auto; }
      .motion-replay-title { font-size:28px; margin:0 0 8px; }
      .motion-replay-grid { display:grid; grid-template-columns:minmax(0,1.25fr) minmax(0,1fr); gap:28px; align-items:start; }
      .motion-replay-section-title { font-size:18px; margin:20px 0 10px; color:#7ce4c5; }
      .motion-replay-proof { margin:18px 0; padding:14px; background:#171e28; border:1px solid #2b3645; border-radius:12px; }
      .motion-replay-proof p { margin:6px 0; overflow-wrap:anywhere; }
      .motion-replay-shell .reconciliation-source-card { margin-top:12px; }
      @media(max-width:900px) { .motion-replay-grid{grid-template-columns:1fr;} .motion-replay-shell{padding:16px;} .motion-replay-band{padding:12px 16px;} }
    `}</style>
    <header className="motion-replay-band" data-testid="historical-replay-band">
      <strong>ATROPOS MEDPLAIN | HISTORICAL ACTUAL BEDROCK RESULT | READ-ONLY REPLAY</strong>
      <p>Original source 38313f8 | Recorded run 41029208154 | Recorded status: review_required | NOT RELEASED</p>
      <p>Current EFE display components. Transport disabled. No new model call, review, release or patient publication.</p>
    </header>
    <main className="motion-replay-shell">
      <h1 className="motion-replay-title">Atropos MedPlain: inspect the recorded clarification</h1>
      <p className="muted">Original English source and card, unchanged. Historical operator inspection, not a current live console or a patient view.</p>
      <details className="motion-replay-proof">
        <summary>Original verification metadata</summary>
        <p><strong>Medication regimen remains withheld. Human clarification is still required.</strong></p>
        <p>Historical verification: {replay.receipts.map(row => row.verdict).join(" then ")}. Recorded correction count: {replay.provenance.recorded_correction_count}.</p>
        <p>Source SHA-256: <span className="mono">{replay.provenance.recorded_source_hash}</span></p>
        <p>Artifact SHA-256: <span className="mono">{replay.provenance.recorded_artifact_hash}</span></p>
        <p>Reference retrieval is disabled for this replay. Bound reference identifiers remain in the original artifact; no replacement metadata is supplied.</p>
      </details>
      <div className="motion-replay-grid">
        <section aria-label="Stored English clarification artifact" data-testid="replay-artifact">
          <h2 className="motion-replay-section-title">Original stored clarification card</h2>
          <ClarificationView artifact={artifact} onSource={onSource} />
        </section>
        <aside aria-label="Stored original source packet" data-testid="replay-sources">
          <h2 className="motion-replay-section-title">Original source records</h2>
          <ReconciliationSources dossier={dossier} />
        </aside>
      </div>
    </main>
  </ReadingPreferences>;
}
createRoot(document.getElementById("root")!).render(<HistoricalReplay />);
