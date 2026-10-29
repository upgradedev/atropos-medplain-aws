import { useEffect, useState } from "react";
import { api, message } from "./api";
import type { ReconciliationExamples, ReferenceMaterial } from "./types";
import { Hash, State } from "./ui";

function checkedUrl(value: string): string | undefined {
  try { const url = new URL(value); return url.protocol === "https:" ? url.href : undefined; } catch { return undefined; }
}
export const referenceCardId = (id: string) => `pinned-reference-${id}`;
export const referencePanelId = (hash: string) => `pinned-reference-panel-${hash}`;

export default function ReferencePanel({ material, boundSnapshotSha, selectedReferenceIds }: { material?: ReferenceMaterial; boundSnapshotSha?: string; selectedReferenceIds?: string[] }) {
  const [fetched, setFetched] = useState<ReferenceMaterial | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(!material);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (material) { setLoading(false); setError(""); return; }
    const controller = new AbortController();
    setError(""); setFetched(null); setLoading(true);
    void api<ReconciliationExamples>("/reconciliation-examples", undefined, controller.signal).then((response) => {
      if (!controller.signal.aborted) setFetched(response.reference_material || null);
    }).catch((cause) => { if (!controller.signal.aborted) setError(message(cause)); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [material, boundSnapshotSha, revision]);
  const value = material || fetched;
  const mismatch = Boolean(boundSnapshotSha && value && value.snapshot_sha256 !== boundSnapshotSha);
  const ids = selectedReferenceIds ? [...new Set(selectedReferenceIds)] : undefined;
  const cards = value?.references.cards.filter((card) => !ids || ids.includes(card.id)) || [];
  const unavailable = ids?.filter((id) => !cards.some((card) => card.id === id)) || [];
  return <details className="reference-panel" data-testid="reference-panel" id={boundSnapshotSha ? referencePanelId(boundSnapshotSha) : undefined} tabIndex={-1}>
    <summary>{boundSnapshotSha ? "Bound primary reference cards" : "Primary reference and identity snapshots"}</summary>
    <p className="notice">Reference context is separate from patient-specific source proof. A reference or ingredient identity does not choose a dose, settle conflicting orders, or establish comprehensive interaction coverage.</p>
    {error ? <State kind="error" title="Reference metadata unavailable" retry={() => setRevision((current) => current + 1)}>{error} No reference card or link is substituted.</State> : loading && !material ? <State kind="loading" title="Reading pinned reference cards" /> : !value ? <p className="control-reason" role="status">No reference snapshot is available for this view. Reference links and excerpts are withheld.</p> : mismatch ? <div className="inline-alert" role="alert" data-testid="reference-snapshot-mismatch"><strong>Reference snapshot mismatch</strong><p>The available snapshot does not match this artifact. Reference links and excerpts are withheld; no newer source is substituted.</p><Hash title="Artifact-bound snapshot SHA-256" value={boundSnapshotSha} /><Hash title="Available snapshot SHA-256" value={value.snapshot_sha256} /></div> : <>
      <p><strong>Pinned reference snapshot</strong> · observed {value.references.observed_on}</p><p>{value.scope}</p>
      <Hash title="Reference snapshot SHA-256" value={value.snapshot_sha256} />
      {boundSnapshotSha ? <p className="control-reason" data-testid="reference-snapshot-matched">The pinned snapshot exactly matches this clarification card's bound reference hash.</p> : null}
      <p className="notice" data-testid="interaction-service-status">Interaction service: {value.interaction_service}. No unavailable interaction service is treated as a clearance.</p>
      <h4>Primary source cards</h4>
      {ids?.length === 0 ? <p className="control-reason">No primary reference identifiers were selected for this card.</p> : null}
      {unavailable.map((id) => <p className="control-reason" data-testid="reference-id-unavailable" key={id}>Reference {id} is unavailable in this exact pinned snapshot. No link or excerpt is substituted.</p>)}
      {cards.map((card) => <article className="reference-card" key={card.id} id={referenceCardId(card.id)} tabIndex={-1} data-reference-id={card.id}><h4>{card.title}</h4><p className="muted">{card.publisher} · {card.section} · observed {value.references.observed_on}</p><p className="eyebrow">Reference summary (paraphrased)</p><p className="reference-summary" data-testid="reference-summary">{card.excerpt}</p><p>{card.use}</p>{checkedUrl(card.url) ? <a className="text-button" href={checkedUrl(card.url)} target="_blank" rel="noreferrer">Open primary source: {card.publisher}</a> : <p className="control-reason">The recorded source URL is unavailable.</p>}</article>)}
      {!boundSnapshotSha ? <><h4>Recorded ingredient identity lookups</h4><p className="muted">{value.identity_snapshot.scope} {value.identity_snapshot.attribution}</p>
      {value.identity_snapshot.records.map((record, index) => <article className="reference-card" key={`${record.lookup_name}-${index}`}><strong>{record.lookup_name}</strong><p>Status: {record.status.replaceAll("_", " ")} · retrieved {record.retrieved_at}</p><p>Recorded RxNorm identifiers: {record.rxnorm_ids.length ? record.rxnorm_ids.join(", ") : "None recorded"}</p>{record.error_type ? <p className="control-reason">Lookup did not complete: {record.error_type}.</p> : null}{checkedUrl(record.url) ? <a href={checkedUrl(record.url)} className="text-button" target="_blank" rel="noreferrer">Inspect recorded identity source</a> : null}</article>)}</> : null}
    </>}
  </details>;
}
