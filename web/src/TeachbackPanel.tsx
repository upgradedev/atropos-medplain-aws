import { useEffect, useState, type FormEvent } from "react";
import { api, message } from "./api";
import { clock, label, type CaseDetail, type Role } from "./types";
import { Hash, Icon, State } from "./ui";
import { careflowBinding, type CareflowView, type PatientTeachbackView, type TeachbackAttempt } from "./careflowTypes";
import "./careflow.css";

function AttemptHistory({ attempts, privateView = false }: { attempts: TeachbackAttempt[]; privateView?: boolean }) {
  return <ol className="careflow-history" aria-label="Teach-back attempt history">{attempts.map((attempt, index) => <li key={attempt.id}>
    <div className="careflow-title"><strong>Response {index + 1}</strong><span className={`careflow-status ${attempt.status}`}>{label(attempt.status)}</span></div>
    <time dateTime={attempt.created_at}>{clock(attempt.created_at)}</time>
    <blockquote>{attempt.response_text}</blockquote>
    {attempt.status === "queued" || attempt.status === "processing" ? <p role="status" className="control-reason">Awaiting a provisional assessment. No understanding has been confirmed.</p> : null}
    {attempt.error || attempt.status === "failed" || attempt.assessment?.status === "refused" ? <div className="careflow-notice warning"><strong>Assessment unavailable</strong><p>This response has not received a usable assessment. Ask the synthetic care-team reviewer to inspect it. No clinical conclusion is supplied.</p></div> : null}
    {attempt.assessment?.status === "provisional" ? <div className="careflow-feedback">
      <strong>Provisional assessment, reviewer required</strong>
      <p className="control-reason">This checks a response against the released source. It does not certify comprehension or authorize treatment.</p>
      {privateView && attempt.assessment.assessment ? <dl className="careflow-facts"><div><dt>Source points covered</dt><dd>{attempt.assessment.assessment.covered_statement_ids.length}</dd></div><div><dt>Source points not demonstrated</dt><dd>{attempt.assessment.assessment.missing_statement_ids.length}</dd></div><div><dt>Possible misunderstanding</dt><dd>{attempt.assessment.assessment.misunderstood_statement_ids.length}</dd></div></dl> : null}
      {attempt.assessment.follow_up ? <><p>{attempt.assessment.follow_up.text}</p>{attempt.assessment.follow_up.source_quotes.map((quote) => <div key={quote.statement_id}><span className="careflow-label">Original source · {quote.statement_id}</span><blockquote>{quote.quote}</blockquote></div>)}</> : null}
    </div> : null}
    {attempt.review ? <p className="control-reason">Reviewer recorded {attempt.review.decision === "follow_up" ? "follow-up and a recheck" : "acknowledgement only"} at {clock(attempt.review.at)}. {attempt.review.decision === "follow_up" ? "Explain the approved source in your own words again." : "Acknowledgement is not a comprehension certificate."}</p> : null}
    {privateView ? <details><summary>Response integrity</summary><Hash title="Recorded response hash" value={attempt.response_sha256} />{attempt.review?.comment ? <p>{attempt.review.comment}</p> : null}</details> : null}
  </li>)}</ol>;
}

export function TeachbackReview({ data, flow, role, actorId, stale, refresh, toast, onBusyChange }: {
  data: CaseDetail; flow: CareflowView; role: Role; actorId: string | null; stale: boolean;
  refresh: () => void; toast: (text: string, error?: boolean) => void;
  onBusyChange: (busy: boolean) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [responseUrl, setResponseUrl] = useState("");
  const [attemptId, setAttemptId] = useState("");
  const [decision, setDecision] = useState<"acknowledged" | "follow_up">("follow_up");
  const [comment, setComment] = useState("");
  const setWorking = (value: boolean) => { setBusy(value); onBusyChange(value); };
  const canReview = role === "reviewer" || role === "admin";
  const actorReason = !canReview ? "Switch to the reviewer or admin synthetic demo role." : !actorId ? "Select a synthetic staff actor first." : "";
  const actionReason = busy || stale ? "Read fresh workflow facts after the current request completes." : error ? "Reload the workflow before another action." : "";
  const enableReason = actionReason || (!canReview ? "Only a reviewer or admin can create private response access." : data.status !== "released" ? "Release this exact verified clarification card before enabling patient responses." : !flow.teachback.prompt ? "This card has no supported teach-back prompt." : "");
  const eligible = flow.teachback.attempts.filter((attempt) => (attempt.status === "assessed" || attempt.status === "failed") && !attempt.review);
  const selected = eligible.find((attempt) => attempt.id === attemptId);
  const failedAssessment = selected?.status === "failed" || selected?.assessment?.status === "refused";
  const effectiveDecision = failedAssessment ? "follow_up" : decision;
  useEffect(() => { setError(""); }, [flow]);
  const enable = async () => {
    if (enableReason) return;
    setWorking(true); setError("");
    try {
      const value = await api<{ careflow: CareflowView; response_url: string }>(`/cases/${encodeURIComponent(data.id)}/careflow/enable-teachback`, careflowBinding(flow));
      const target = new URL(value.response_url, location.origin);
      if (target.origin !== location.origin || !target.pathname.startsWith("/console/patient/") || !new URLSearchParams(target.hash.slice(1)).has("teachback")) throw new Error("The response service did not return a same-site private response link.");
      setResponseUrl(value.response_url); toast("Private synthetic response access created"); refresh();
    } catch (reason) { setError(message(reason)); toast(message(reason), true); }
    finally { setWorking(false); }
  };
  const copyLink = async () => {
    try {
      if (!navigator.clipboard) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(new URL(responseUrl, location.origin).href);
      toast("Private response link copied");
    } catch { toast("Copy unavailable. Open the response view directly.", true); }
  };
  const review = async (event: FormEvent) => {
    event.preventDefault();
    if (!selected || actionReason || actorReason || !comment.trim()) return;
    setWorking(true); setError("");
    try {
      await api(`/cases/${encodeURIComponent(data.id)}/careflow/teachback/${encodeURIComponent(selected.id)}/review`, {
        ...careflowBinding(flow), actor_id: actorId, decision: effectiveDecision, comment: comment.trim(),
      });
      setComment(""); setAttemptId(""); toast("Teach-back review recorded; no treatment or comprehension certification"); refresh();
    } catch (reason) { setError(message(reason)); toast(message(reason), true); }
    finally { setWorking(false); }
  };
  const reviewReason = actionReason || actorReason || (!selected ? "Choose an assessed response that needs review." : !comment.trim() ? "Record a short review comment." : "");
  return <section className="careflow-card" data-testid="teachback-review" aria-labelledby="teachback-review-title">
    <div><div className="careflow-label">Patient understanding</div><h3 id="teachback-review-title">Teach-back review and recheck</h3></div>
    <p>A response is assessed provisionally against the released source. A reviewer chooses whether further explanation and another response are needed. No assessment completes a clinical issue automatically.</p>
    {flow.teachback.prompt ? <blockquote>{flow.teachback.prompt.text}</blockquote> : <p className="control-reason">No teach-back prompt is available on this card.</p>}
    {error ? <div className="inline-alert" role="alert">{error} Reload the workflow to reconcile the result before another action.</div> : null}
    <button className="button secondary" disabled={Boolean(enableReason)} onClick={() => void enable()}>{busy ? "Saving..." : flow.teachback.enabled ? "Create private response link" : "Enable synthetic patient responses"}</button>
    <p className="control-reason">{enableReason || "The ordinary patient share stays read-only. This separate private link permits only synthetic response submission and its history."}</p>
    {responseUrl ? <div className="careflow-feedback"><strong>Private response access</strong><p className="control-reason">Treat this URL as a private capability. Share only with the synthetic patient tester. Ordinary read-only links do not include it.</p><a className="careflow-response-url" href={responseUrl} target="_blank" rel="noopener noreferrer">Open private synthetic response view</a><button className="button secondary" onClick={() => void copyLink()}>Copy private response link</button></div> : null}
    {flow.teachback.attempts.length ? <AttemptHistory attempts={flow.teachback.attempts} privateView /> : <div className="careflow-empty">No patient responses yet. Enabling a link does not submit a response or demonstrate understanding.</div>}
    {eligible.length ? <form className="careflow-form" onSubmit={(event) => void review(event)}>
      <label>Response awaiting reviewer<select value={attemptId} onChange={(event) => setAttemptId(event.target.value)}><option value="">Choose response</option>{eligible.map((attempt) => <option value={attempt.id} key={attempt.id}>{clock(attempt.created_at)} · {attempt.id}</option>)}</select></label>
      <label>Review decision<select value={effectiveDecision} onChange={(event) => setDecision(event.target.value as "acknowledged" | "follow_up")}><option value="follow_up">Clarify approved source and request recheck</option><option value="acknowledged" disabled={failedAssessment}>Acknowledge assessment only</option></select></label>
      {failedAssessment ? <p className="control-reason">This attempt has no usable assessment. Only source-based follow-up and a new recheck may be requested; it cannot be acknowledged as assessed.</p> : null}
      <label>Private reviewer comment<textarea value={comment} maxLength={500} onChange={(event) => setComment(event.target.value)} placeholder="Record review of the response and source. This private note is not shown as patient advice." /></label>
      <button className="button" type="submit" disabled={Boolean(reviewReason)}>Record teach-back review</button><p className="control-reason">{reviewReason || "The patient receives only bounded source-linked follow-up, never this free-text comment."}</p>
    </form> : <p className="control-reason">Queued assessments need completion before review. Use Reload workflow to read the latest state.</p>}
  </section>;
}

function responseStorageKey(share: string) {
  return `medplain.teachback.v1:${location.origin}/console/patient/${encodeURIComponent(share)}`;
}
function readCapability(share: string) {
  const key = responseStorageKey(share);
  const params = new URLSearchParams(location.hash.slice(1));
  const supplied = params.get("teachback");
  if (supplied && supplied.length <= 2048) {
    return supplied;
  }
  try { return sessionStorage.getItem(key); } catch { return null; }
}

async function patientRequest<T>(share: string, capability: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal?.addEventListener("abort", abort, { once: true });
  const timer = window.setTimeout(abort, 25000);
  try {
    if (signal?.aborted) throw new DOMException("Cancelled", "AbortError");
    const response = await fetch(`/api/ops/careflow/patient/${encodeURIComponent(share)}${body === undefined ? "" : "/responses"}`, {
      method: body === undefined ? "GET" : "POST", credentials: "omit",
      headers: { Accept: "application/json", "X-Medplain-Teachback": capability, ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: controller.signal,
    });
    const result = await response.json().catch(() => null);
    if (!response.ok) throw new Error(typeof result?.detail === "string" ? result.detail : `Response service unavailable (HTTP ${response.status}).`);
    return result as T;
  } catch (reason) {
    if (signal?.aborted) throw new DOMException("Cancelled", "AbortError");
    if (reason instanceof DOMException && reason.name === "AbortError") throw new Error("The response service timed out. Refresh the history before trying another submission.");
    throw reason instanceof Error ? reason : new Error("The response service cannot be reached.");
  } finally { window.clearTimeout(timer); signal?.removeEventListener("abort", abort); }
}

export default function TeachbackPanel({ share }: { share: string }) {
  const [capability, setCapability] = useState(() => readCapability(share));
  const [view, setView] = useState<PatientTeachbackView | null>(null);
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [response, setResponse] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [notice, setNotice] = useState("");
  const [pollPaused, setPollPaused] = useState(false);
  useEffect(() => {
    const params = new URLSearchParams(location.hash.slice(1));
    if (!capability || params.get("teachback") !== capability) return;
    try { sessionStorage.setItem(responseStorageKey(share), capability); } catch { /* In-memory access still works for this page. */ }
    params.delete("teachback");
    const remaining = params.toString();
    history.replaceState(history.state, "", location.pathname + location.search + (remaining ? `#${remaining}` : ""));
  }, [share, capability]);
  useEffect(() => {
    if (!capability) return;
    const controller = new AbortController();
    let timer = 0; let reads = 0;
    setLoading(true); setError(""); setPollPaused(false);
    const read = async () => {
      try {
        const current = await patientRequest<PatientTeachbackView>(share, capability, undefined, controller.signal);
        if (controller.signal.aborted) return;
        setView(current); setError("");
        if (current.attempts.some((attempt) => attempt.status === "queued" || attempt.status === "processing")) {
          if (reads++ < 60) timer = window.setTimeout(read, 2000); else setPollPaused(true);
        }
      } catch (reason) { if (!controller.signal.aborted) setError(message(reason)); }
      finally { if (!controller.signal.aborted) setLoading(false); }
    };
    void read();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [share, capability, revision]);
  const clear = () => {
    try { sessionStorage.removeItem(responseStorageKey(share)); } catch { /* Also clear this page's memory. */ }
    setCapability(null); setView(null); setResponse(""); setConfirmed(false); setNotice(""); setError("");
  };
  const last = view?.attempts[view.attempts.length - 1];
  const disabled = busy ? "Saving your synthetic response." : loading || error ? "Refresh your response history before submitting." : !view ? "Read the current prompt first." : view.attempts.length >= 3 ? "This synthetic session has reached the three-response limit." : last && last.review?.decision !== "follow_up" ? "Your response is awaiting assessment or reviewer guidance. It has not confirmed understanding." : !response.trim() ? "Explain the source in your own words to enable submission." : !confirmed ? "Confirm that the response is wholly synthetic." : "";
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!capability || !view || disabled) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await patientRequest<{ careflow: PatientTeachbackView; attempt_id: string }>(share, capability, {
        expected_version: view.case_version, prompt_id: view.prompt.prompt_id, response_text: response.trim(), synthetic_confirmed: true, idempotency_key: crypto.randomUUID(),
      });
      setView(result.careflow); setResponse(""); setConfirmed(false); setNotice("Response recorded. Assessment and human review are still required. No understanding has been certified."); setRevision((value) => value + 1);
    } catch (reason) { setError(`${message(reason)} Refresh the history to check whether your response was recorded. Do not blindly resubmit.`); }
    finally { setBusy(false); }
  };
  return <section className="careflow" id="teachback" data-testid="patient-teachback" aria-labelledby="patient-teachback-title">
    <div className="careflow-heading"><div><div className="careflow-label">Explain in your own words</div><h2 id="patient-teachback-title">Check the explanation together</h2></div>{capability ? <span className="careflow-status">Synthetic response access</span> : null}</div>
    {!capability ? <div className="careflow-notice"><strong>This share is read-only</strong><p>A separate private response link from the synthetic care-team reviewer is required to submit teach-back. Viewing this card does not confirm understanding.</p></div> : <>
      <div className="careflow-notice warning"><strong>Synthetic demonstration only</strong><p>Use invented patient responses only. This does not provide medical advice or confirm comprehension. Response access is saved only in this browser tab for this share.</p></div>
      <div className="action-row"><button className="button secondary" disabled={loading || busy} onClick={() => setRevision((value) => value + 1)}><Icon name="refresh" />Refresh response history</button><button className="button secondary" disabled={busy} onClick={clear}>Clear response access</button></div>
      {loading || busy ? <p className="control-reason" role="status">{busy ? "Wait until submission completes before clearing or refreshing access." : "Reading current response history before enabling actions."}</p> : null}
      {error ? <div className="inline-alert" role="alert">{error}</div> : null}
      {notice ? <div className="careflow-notice" role="status"><p>{notice}</p></div> : null}
      {loading && !view ? <State kind="loading" title="Reading your source-bound prompt" /> : !view ? <State kind="empty" title="No response prompt available">The private link may be expired or unavailable. Ask the synthetic reviewer for access.</State> : <>
        <blockquote>{view.prompt.text}</blockquote>
        <p className="control-reason">The prompt belongs to this exact released source and card. Keep unresolved medication decisions with the care team.</p>
        {view.attempts.length ? <AttemptHistory attempts={view.attempts} /> : <div className="careflow-empty">No response has been submitted. A short answer in your own words helps the reviewer identify what needs explaining.</div>}
        {pollPaused ? <p className="control-reason" role="status">Automatic reading paused after two minutes. Refresh response history to check the assessment.</p> : null}
        <form className="careflow-form" onSubmit={(event) => void submit(event)}><label>Your synthetic response<textarea value={response} maxLength={2000} onChange={(event) => setResponse(event.target.value)} placeholder="Explain what you understand and what you would still ask the care team. Do not include real personal or medical information." /></label><label className="careflow-check"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />This is wholly invented and contains no real patient information.</label><button type="submit" className="button" disabled={Boolean(disabled)} aria-describedby="teachback-submit-reason">{busy ? "Recording response..." : last ? "Submit synthetic recheck" : "Submit synthetic response"}</button><p className="control-reason" id="teachback-submit-reason">{disabled || "Submission records your response only. It cannot close a clarification or certify understanding."}</p></form>
      </>}
    </>}
  </section>;
}
