import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, ApiError, key, message } from "./api";
import type { Bootstrap, CaseSummary, DocumentKind, ReconciliationDossier, ReconciliationExamples } from "./types";
import { Dialog, Icon, State } from "./ui";
import ReferencePanel from "./ReferencePanel";

const PROFILE = "medplain-reconciliation-v1" as const;
export const DOCUMENT_LABELS: Record<DocumentKind, string> = {
  home_list: "Home medicines before admission", discharge: "Discharge orders",
  addendum: "Addendum", caregiver_note: "Caregiver note", lab_report: "Lab report",
};
export type ReconciliationSeed = { dossier: ReconciliationDossier; parent_case_id: string };
const emptyDossier = (): ReconciliationDossier => ({ profile: PROFILE, patient_alias: "Demo-Complex", language: "en", caregiver_concerns: "", documents: [
  { document_id: "source-home", kind: "home_list", issued_at: "", effective_at: null, text: "" },
  { document_id: "source-discharge", kind: "discharge", issued_at: "", effective_at: null, text: "" },
] });
const inputTime = (value: string | null) => value ? value.replace(/Z$/, "") : "";
const utcTime = (value: string) => value ? `${value.length === 16 ? value + ":00" : value}Z` : "";

export default function ReconciliationForm({ bootstrap, workspace, count, canCreate, close, done, failed, back, seed }: {
  bootstrap: Bootstrap; workspace: string; count: number; canCreate: boolean; close: () => void;
  done: (id: string) => void; failed: (text: string) => void; back: () => void; seed?: ReconciliationSeed | null;
}) {
  const [dossier, setDossier] = useState<ReconciliationDossier>(() => seed ? structuredClone(seed.dossier) : emptyDossier());
  const [examples, setExamples] = useState<ReconciliationExamples | null>(null);
  const [readError, setReadError] = useState("");
  const [readRevision, setReadRevision] = useState(0);
  const [selectedExample, setSelectedExample] = useState("");
  const [facility, setFacility] = useState(workspace === "all" ? bootstrap.facilities[0]?.id || "" : workspace);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [stress, setStress] = useState(false);
  const idempotency = useRef(key());
  const submitting = useRef(false);
  useEffect(() => {
    const controller = new AbortController();
    setReadError("");
    void api<ReconciliationExamples>("/reconciliation-examples", undefined, controller.signal).then((data) => {
      if (!controller.signal.aborted) setExamples(data);
    }).catch((cause) => { if (!controller.signal.aborted) setReadError(message(cause)); });
    return () => controller.abort();
  }, [readRevision]);
  const edit = (change: (current: ReconciliationDossier) => ReconciliationDossier) => {
    setDossier(change); setConfirmed(false); setError(""); idempotency.current = key();
  };
  const updateDocument = (index: number, change: Partial<ReconciliationDossier["documents"][number]>) => edit((current) => ({ ...current, documents: current.documents.map((doc, i) => i === index ? { ...doc, ...change } : doc) }));
  const totalChars = dossier.documents.reduce((sum, doc) => sum + Array.from(doc.text).length, 0);
  const nonemptyLines = dossier.documents.reduce((sum, doc) => sum + doc.text.split(/\r\n|\r|\n/).filter((line) => line.trim()).length, 0);
  const maxChars = examples?.limits?.max_source_chars || examples?.limits?.max_total_chars || 16000;
  const maxDocumentChars = examples?.limits?.max_document_chars || 6000;
  const maxLines = examples?.limits?.max_lines || examples?.limits?.max_nonempty_lines || 60;
  const ids = dossier.documents.map((doc) => doc.document_id.trim());
  const inputReason = !/^Demo-[A-Za-z0-9-]{1,32}$/.test(dossier.patient_alias) ? "Use an invented Demo-... alias without personal details."
    : dossier.documents.length < 2 || dossier.documents.length > 4 ? "Include two to four original source documents."
    : ids.some((id) => !id) || new Set(ids).size !== ids.length ? "Each source needs a unique document identifier."
    : dossier.documents.some((doc) => !doc.text.trim()) ? "Enter text for every source, or remove an optional blank source."
    : dossier.documents.some((doc) => !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(doc.issued_at)) ? "Record the issued time for each source in UTC."
    : dossier.documents.some((doc) => Array.from(doc.text).length > maxDocumentChars) ? `One source exceeds ${maxDocumentChars.toLocaleString()} characters. Nothing is silently truncated.`
    : totalChars > maxChars ? `Source text exceeds ${maxChars.toLocaleString()} characters. Nothing is silently truncated.`
    : nonemptyLines > maxLines ? `Source text exceeds ${maxLines} nonempty lines. Shorten it before submitting.`
    : Array.from(dossier.caregiver_concerns).length > 1000 ? "Keep caregiver concerns within 1,000 characters."
    : !confirmed ? "Confirm that every document and concern is invented and contains no real patient information." : "";
  const reason = busy ? "One reconciliation request is in progress. Do not resubmit." : !canCreate ? "Switch to the operator or admin demo role to create a case." : count >= bootstrap.limits.max_cases ? "This session has reached its case limit. Start a new session in Settings." : !facility ? "Select a synthetic operations desk." : inputReason;
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); if (reason || submitting.current) return;
    submitting.current = true; setBusy(true); setError("");
    try {
      const response = await api<{ case: CaseSummary; created: boolean; job_id: string }>("/cases/reconcile", {
        dossier, synthetic_confirmed: confirmed, facility_id: facility, idempotency_key: idempotency.current, stress,
        ...(seed ? { parent_case_id: seed.parent_case_id } : {}),
      });
      done(response.case.id);
    } catch (cause) {
      const text = cause instanceof ApiError && cause.status === 413 ? "The source packet is too large. Nothing was silently truncated."
        : cause instanceof ApiError && cause.status >= 500 ? "The reconciliation service is unavailable. Check the case register before retrying." : message(cause);
      setError(`${text} Your sources remain here, and the same submission identifier is retained.`); failed(text);
    } finally { submitting.current = false; setBusy(false); }
  };
  return <Dialog title={seed ? "Revise sources and reverify" : "Reconcile synthetic source packet"} wide onClose={close}>
    <form className="new-case-body reconciliation-form" onSubmit={(event) => void submit(event)}>
      <div className="eyebrow">SYNTHETIC SOURCES ONLY</div>
      <p>Compare original home lists, discharge documents and caregiver context. The result is a source-cited clarification card, never a new prescription.</p>
      {seed ? <div className="notice">New source revision for case <span className="mono break">{seed.parent_case_id}</span>. Saving creates a new case and verification run. The previous source remains unchanged.</div> : <button type="button" className="text-button" disabled={busy} onClick={back}>Use a sample or single-note import instead</button>}
      {busy ? <p className="control-reason">Source controls are locked while this request is in progress.</p> : null}
      <div className="notice">Use invented information only. Do not paste actual names, birth dates, contact details or patient records. Confirmation does not detect or remove private information.</div>
      {!examples ? readError ? <State kind="error" title="Synthetic examples unavailable" retry={() => setReadRevision((value) => value + 1)}>{readError} You can still complete the source fields manually.</State> : <p className="muted" role="status">Loading complex synthetic examples...</p> : <>
        <label>Complex synthetic example<select aria-label="Complex synthetic example" value={selectedExample} disabled={busy} onChange={(event) => setSelectedExample(event.target.value)}><option value="">Choose an example</option>{examples.examples.map((example) => <option value={example.id} key={example.id}>{example.title}</option>)}</select></label>
        <button type="button" className="button secondary" disabled={busy || !selectedExample} onClick={() => {
          const example = examples.examples.find((item) => item.id === selectedExample);
          if (example) edit(() => structuredClone(example.dossier));
        }}>Load complex synthetic example</button>
        {!selectedExample ? <p className="control-reason">Select an example to enable loading. Loading replaces these form fields only.</p> : <p className="muted">{examples.examples.find((item) => item.id === selectedExample)?.description}</p>}
      </>}
      <div className="reconciliation-metadata">
        <label>Invented patient alias<input aria-label="Invented patient alias" autoComplete="off" value={dossier.patient_alias} disabled={busy} onChange={(event) => edit((current) => ({ ...current, patient_alias: event.target.value }))} /></label>
        <label>Clarification card language<select aria-label="Clarification card language" value={dossier.language} disabled={busy} onChange={(event) => edit((current) => ({ ...current, language: event.target.value as "en" | "el" }))}><option value="en">English</option><option value="el">Ελληνικά (Greek)</option></select></label>
      </div>
      <p className="muted">This language applies to clarification questions. Original source quotations remain in their original language.</p>
      {dossier.documents.map((document, index) => <section className="reconciliation-editor" key={index} aria-label={`Original source ${index + 1}`}>
        <div className="section-heading"><h3>Source {index + 1}: {DOCUMENT_LABELS[document.kind]}</h3><button type="button" className="button secondary" disabled={busy || dossier.documents.length <= 2} onClick={() => edit((current) => ({ ...current, documents: current.documents.filter((_, i) => i !== index) }))}>Remove source {index + 1}</button></div>
        {dossier.documents.length <= 2 ? <p className="control-reason">At least two original sources are required.</p> : null}
        <div className="reconciliation-metadata">
          <label>Document identifier<input aria-label={`Source ${index + 1} identifier`} value={document.document_id} disabled={busy} onChange={(event) => updateDocument(index, { document_id: event.target.value })} /></label>
          <label>Document kind<select aria-label={`Source ${index + 1} kind`} value={document.kind} disabled={busy} onChange={(event) => updateDocument(index, { kind: event.target.value as DocumentKind })}>{Object.entries(DOCUMENT_LABELS).map(([value, title]) => <option key={value} value={value}>{title}</option>)}</select></label>
          <label>Issued time (UTC)<input type="datetime-local" step="1" aria-label={`Source ${index + 1} issued time UTC`} value={inputTime(document.issued_at)} disabled={busy} onChange={(event) => updateDocument(index, { issued_at: utcTime(event.target.value) })} /></label>
          <label>Effective time (UTC, optional)<input type="datetime-local" step="1" aria-label={`Source ${index + 1} effective time UTC`} value={inputTime(document.effective_at)} disabled={busy} onChange={(event) => updateDocument(index, { effective_at: utcTime(event.target.value) || null })} /></label>
        </div>
        <label>Original source text<textarea aria-label={`Source ${index + 1} original text`} data-testid={`reconciliation-source-${index + 1}`} value={document.text} disabled={busy} autoComplete="off" spellCheck={false} onChange={(event) => updateDocument(index, { text: event.target.value })} /></label>
        <details><summary>Preview exact source {index + 1}</summary><pre className="source-text">{document.text || "No text entered."}</pre></details>
      </section>)}
      <button type="button" className="button secondary" disabled={busy || dossier.documents.length >= 4} onClick={() => edit((current) => ({ ...current, documents: [...current.documents, { document_id: `source-${current.documents.length + 1}`, kind: "addendum", issued_at: "", effective_at: null, text: "" }] }))}>Add original source</button>
      {dossier.documents.length >= 4 ? <p className="control-reason">The source packet is limited to four documents.</p> : null}
      <p className="intake-counter" role="status">{totalChars.toLocaleString()} / {maxChars.toLocaleString()} source characters · {nonemptyLines} / {maxLines} nonempty lines · {maxDocumentChars.toLocaleString()} characters per document</p>
      <label>Caregiver concerns<textarea aria-label="Caregiver concerns" value={dossier.caregiver_concerns} disabled={busy} onChange={(event) => edit((current) => ({ ...current, caregiver_concerns: event.target.value }))} /></label>
      <p className="control-reason">{Array.from(dossier.caregiver_concerns).length} / 1,000 characters. These concerns guide source-cited questions, not medication changes.</p>
      <label>Synthetic operations desk<select aria-label="Synthetic operations desk" value={facility} disabled={busy} onChange={(event) => { setFacility(event.target.value); setConfirmed(false); idempotency.current = key(); }} >{bootstrap.facilities.map((item) => <option key={item.id} value={item.id}>{item.city} · {item.name}</option>)}</select></label>
      <label className="check-field"><input type="checkbox" aria-label="Deliberate source citation mutation drill" checked={stress} disabled={busy} onChange={(event) => { setStress(event.target.checked); setConfirmed(false); idempotency.current = key(); }} /><span><strong>Deliberate source citation mutation drill</strong><span>Manually inject an altered candidate quotation or citation after generation. Original sources remain unchanged; the integrity gate must withhold the changed candidate.</span></span></label>
      {examples?.reference_material ? <ReferencePanel material={examples.reference_material} /> : null}
      <label className="check-field"><input type="checkbox" checked={confirmed} disabled={busy} aria-label="Confirm invented reconciliation sources" onChange={(event) => { setConfirmed(event.target.checked); setError(""); }} /><span><strong>I confirm all sources and concerns are invented and contain no real patient information.</strong><span>This confirmation is not a privacy guarantee.</span></span></label>
      {error ? <div className="inline-alert" role="alert" data-testid="reconciliation-error">{error}</div> : null}
      <button type="submit" className="button full" data-testid="start-reconciliation" disabled={Boolean(reason)} aria-describedby="reconciliation-submit-reason"><Icon name={busy ? "refresh" : "shield"} />{busy ? "Creating reconciliation case..." : "Reconcile source packet"}</button>
      <p id="reconciliation-submit-reason" className="control-reason" aria-live="polite">{reason || "Captures this exact packet and queues one real verification run. No medication regimen will be authorized."}</p>
    </form>
  </Dialog>;
}
