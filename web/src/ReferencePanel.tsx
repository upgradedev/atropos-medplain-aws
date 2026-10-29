import { useEffect, useState } from "react";
import { api, message } from "./api";
import type { ReconciliationExamples, ReferenceMaterial } from "./types";
import { Hash, State } from "./ui";

function checkedUrl(value: string): string | undefined {
  try { const url = new URL(value); return url.protocol === "https:" ? url.href : undefined; } catch { return undefined; }
}

export default function ReferencePanel({ material }: { material?: ReferenceMaterial }) {
  const [fetched, setFetched] = useState<ReferenceMaterial | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (material) return;
    const controller = new AbortController();
    setError("");
    void api<ReconciliationExamples>("/reconciliation-examples", undefined, controller.signal).then((response) => {
      if (!controller.signal.aborted) setFetched(response.reference_material || null);
    }).catch((cause) => { if (!controller.signal.aborted) setError(message(cause)); });
    return () => controller.abort();
  }, [material, revision]);
  const value = material || fetched;
  return <details className="reference-panel" data-testid="reference-panel">
    <summary>Primary reference and identity snapshots</summary>
    <p className="notice">Reference context is separate from patient-specific source proof. A reference or ingredient identity does not choose a dose, settle conflicting orders, or establish comprehensive interaction coverage.</p>
    {error ? <State kind="error" title="Reference metadata unavailable" retry={() => setRevision((current) => current + 1)}>{error}</State> : !value ? <p className="muted" role="status">No reference snapshot is available for this view.</p> : <>
      <p><strong>Pinned reference snapshot</strong> · observed {value.references.observed_on}</p><p>{value.scope}</p>
      <Hash title="Reference snapshot SHA-256" value={value.snapshot_sha256} />
      <p className="notice" data-testid="interaction-service-status">Interaction service: {value.interaction_service}. No unavailable interaction service is treated as a clearance.</p>
      <h4>Primary source cards</h4>
      {value.references.cards.map((card) => <article className="reference-card" key={card.id}><h4>{card.title}</h4><p className="muted">{card.publisher} · {card.section}</p><blockquote>{card.excerpt}</blockquote><p>{card.use}</p>{checkedUrl(card.url) ? <a className="text-button" href={checkedUrl(card.url)} target="_blank" rel="noreferrer">Open primary source: {card.publisher}</a> : <p className="control-reason">The recorded source URL is unavailable.</p>}</article>)}
      <h4>Recorded ingredient identity lookups</h4><p className="muted">{value.identity_snapshot.scope} {value.identity_snapshot.attribution}</p>
      {value.identity_snapshot.records.map((record, index) => <article className="reference-card" key={`${record.lookup_name}-${index}`}><strong>{record.lookup_name}</strong><p>Status: {record.status.replaceAll("_", " ")} · retrieved {record.retrieved_at}</p><p>Recorded RxNorm identifiers: {record.rxnorm_ids.length ? record.rxnorm_ids.join(", ") : "None recorded"}</p>{record.error_type ? <p className="control-reason">Lookup did not complete: {record.error_type}.</p> : null}{checkedUrl(record.url) ? <a href={checkedUrl(record.url)} className="text-button" target="_blank" rel="noreferrer">Inspect recorded identity source</a> : null}</article>)}
    </>}
  </details>;
}
