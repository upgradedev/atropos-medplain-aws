import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, message } from "./api";
import { clock, label, type CaseDetail, type Role } from "./types";
import { Hash, Icon, State } from "./ui";
import { TeachbackReview } from "./TeachbackPanel";
import {
  careflowBinding,
  type ActorDirectory,
  type CareflowView,
  type EvidenceResult,
} from "./careflowTypes";
import "./careflow.css";

function safeSource(uri: string) {
  try {
    return new URL(uri).protocol === "https:" ? uri : null;
  } catch {
    return null;
  }
}

export function EvidenceSearch({ topic }: { topic?: string }) {
  const [query, setQuery] = useState("");
  const [language, setLanguage] = useState("en");
  const [jurisdiction, setJurisdiction] = useState("any");
  const [asOf, setAsOf] = useState(() => new Date().toISOString().slice(0, 10));
  const [result, setResult] = useState<EvidenceResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);
  const search = async (event: FormEvent) => {
    event.preventDefault();
    if (busy || !query.trim() || !asOf) return;
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setBusy(true);
    setError("");
    setResult(null);
    try {
      const response = await api<EvidenceResult>(
        "/careflow/evidence/search",
        {
          query: query.trim(),
          language,
          population: "adult",
          jurisdiction,
          as_of: asOf,
          limit: 3,
        },
        controller.signal,
      );
      if (!controller.signal.aborted) setResult(response);
    } catch (reason) {
      if (!controller.signal.aborted) setError(message(reason));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  };
  const disabled = busy
    ? "Searching the reviewed snapshot."
    : !query.trim()
      ? "Enter a general workflow question to search."
      : !asOf
        ? "Choose the snapshot review date."
        : "";
  return (
    <section
      className="careflow-card"
      aria-labelledby="careflow-evidence-title"
      data-testid="careflow-evidence"
    >
      <div>
        <div className="careflow-label">Reviewed workflow evidence</div>
        <h3 id="careflow-evidence-title">Inspect the source context</h3>
      </div>
      <p>
        These educational snapshots were reviewed by the engineering team, not
        clinically approved. They cannot supply this patient's missing dose,
        owner or start date. Use a general topic, without patient identifiers.
      </p>
      <form className="careflow-form" onSubmit={(event) => void search(event)}>
        {topic ? (
          <div>
            <button
              className="text-button"
              type="button"
              disabled={busy}
              onClick={() => {
                setQuery(topic);
                setResult(null);
                setError("");
              }}
            >
              Use current task's workflow topic
            </button>
            <p className="control-reason">
              {busy
                ? "Wait for the evidence search to finish."
                : "Copies a general workflow topic only. It does not send the patient source or start a search."}
            </p>
          </div>
        ) : null}
        <label>
          Evidence question
          <input
            value={query}
            maxLength={240}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Who accepts responsibility for a pending test?"
          />
        </label>
        <div className="careflow-fields">
          <label>
            Evidence language
            <select
              aria-label="Evidence language"
              value={language}
              onChange={(event) => setLanguage(event.target.value)}
            >
              <option value="en">English</option>
              <option value="el">Greek</option>
            </select>
          </label>
          <label>
            Guidance jurisdiction
            <select
              aria-label="Guidance jurisdiction"
              value={jurisdiction}
              onChange={(event) => setJurisdiction(event.target.value)}
            >
              <option value="any">Any jurisdiction</option>
              <option value="US">United States</option>
              <option value="EU">European Union</option>
              <option value="GR">Greece</option>
            </select>
          </label>
          <label>
            Snapshot reviewed as of
            <input
              type="date"
              value={asOf}
              onChange={(event) => setAsOf(event.target.value)}
            />
          </label>
        </div>
        <button
          className="button secondary"
          type="submit"
          disabled={Boolean(disabled)}
          aria-describedby="evidence-search-reason"
        >
          <Icon name="search" />
          {busy ? "Searching evidence..." : "Search reviewed evidence"}
        </button>
        <p id="evidence-search-reason" className="control-reason">
          {disabled ||
            "Adult workflow guidance only. No web browsing or prescription recommendations."}
        </p>
      </form>
      {busy ? (
        <State kind="loading" title="Reading reviewed evidence" />
      ) : error ? (
        <div role="alert" className="inline-alert">
          {error} Edit or resubmit the search when the service is available.
        </div>
      ) : !result ? (
        <div className="careflow-empty">
          Search to inspect exact source snippets, versions and recorded scope.
        </div>
      ) : (
        <>
          <div className="careflow-notice">
            <strong>
              {result.status === "supported"
                ? "Matching reviewed passages"
                : "No matching reviewed passage"}
            </strong>
            <p>
              {result.status === "supported"
                ? "A retrieval match is not a clinical judgment or proof that a patient-specific claim is supported."
                : "No matching engineering-reviewed passage was returned. No missing fact or recommendation has been invented."}
            </p>
          </div>
          <div className="careflow-meta">
            <span>Snapshot {result.corpus.version}</span>
            <span>Observed {result.corpus.snapshot_date}</span>
          </div>
          <p className="control-reason">{result.corpus.scope}</p>
          <div className="careflow-evidence">
            {result.entries.map((entry) => (
              <article key={entry.evidence_id} className="careflow-feedback">
                <div className="careflow-label">
                  {entry.publisher} · {entry.section}
                </div>
                <h4>
                  {safeSource(entry.uri) ? (
                    <a
                      href={entry.uri}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      {entry.title}
                    </a>
                  ) : (
                    entry.title
                  )}
                </h4>
                <blockquote>{entry.span.quote}</blockquote>
                <div className="careflow-meta">
                  <span>Version {entry.source_version}</span>
                  <span>
                    {entry.jurisdiction} · {entry.population} · {entry.language}
                  </span>
                  <span>
                    Snapshot review window {entry.valid_from} to{" "}
                    {entry.valid_until || "open-ended"}
                  </span>
                  <span>
                    Lexical retrieval score {entry.score.toFixed(2)}, not
                    clinical confidence
                  </span>
                </div>
                <details>
                  <summary>Inspect source integrity and offsets</summary>
                  <p className="control-reason">
                    Source {entry.source_id} · snapshot byte offsets{" "}
                    {entry.span.start} to {entry.span.end}. The hash binds the
                    captured excerpt, not the complete primary page. Matching
                    text does not establish clinical applicability.
                  </p>
                  <Hash
                    title="Excerpt snapshot hash"
                    value={entry.document_sha256}
                  />
                  <Hash title="Exact snippet hash" value={entry.span.sha256} />
                </details>
              </article>
            ))}
          </div>
          <details>
            <summary>Approved snapshot identity</summary>
            <Hash title="Evidence corpus hash" value={result.corpus.sha256} />
          </details>
        </>
      )}
    </section>
  );
}

const STEPS = ["Assigned", "Accepted", "Answered", "Revision linked", "Closed"];
const STAGES = [
  "open",
  "assigned",
  "accepted",
  "answered",
  "revision_linked",
  "closed",
];

export default function CareflowPanel({
  data,
  role,
  toast,
  changed,
  onRoleChange,
  roleBusy = false,
  onOpenCase,
  onRevise,
  onReviewRelease,
}: {
  data: CaseDetail;
  role: Role;
  toast: (text: string, error?: boolean) => void;
  changed: () => void;
  onRoleChange?: (role: Role) => Promise<void>;
  roleBusy?: boolean;
  onOpenCase?: (caseId: string) => void;
  onRevise?: () => void;
  onReviewRelease?: () => void;
}) {
  const [flow, setFlow] = useState<CareflowView | null>(null);
  const latestFlow = useRef<CareflowView | null>(null);
  const [directory, setDirectory] = useState<ActorDirectory | null>(null);
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [selectedId, setSelectedId] = useState("");
  const [assignee, setAssignee] = useState("");
  const [dueAt, setDueAt] = useState(() =>
    new Date(Date.now() + 2 * 3600_000).toISOString().slice(0, 16),
  );
  const [answer, setAnswer] = useState("");
  const [revisionId, setRevisionId] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const refresh = () => setRevision((value) => value + 1);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    void Promise.all([
      api<CareflowView>(
        `/cases/${encodeURIComponent(data.id)}/careflow`,
        undefined,
        controller.signal,
      ),
      api<ActorDirectory>("/careflow/actors", undefined, controller.signal),
    ])
      .then(([view, actors]) => {
        if (!controller.signal.aborted) {
          if (
            !latestFlow.current ||
            latestFlow.current.case_id !== view.case_id ||
            view.case_version >= latestFlow.current.case_version
          ) {
            latestFlow.current = view;
            setFlow(view);
          }
          setDirectory((current) =>
            !current || actors.actor_version >= current.actor_version
              ? actors
              : current,
          );
        }
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(message(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [data.id, revision]);
  useEffect(() => {
    // Local actions already returned their current workflow version. Only a
    // newer external case version needs another read; neither path resets drafts.
    const current = latestFlow.current;
    if (
      current &&
      current.case_id === data.id &&
      data.version > current.case_version
    ) {
      setRevision((value) => value + 1);
    }
  }, [data.id, data.version, flow?.case_version]);
  const selectTask = (id: string) => {
    if (id === (selectedId || flow?.tasks[0]?.id)) return;
    setSelectedId(id);
    setAssignee("");
    setAnswer("");
    setRevisionId("");
    setConfirmed(false);
  };
  const task =
    flow?.tasks.find((item) => item.id === selectedId) || flow?.tasks[0];
  const actorId = directory?.selected_actor_id || null;
  const actorLabel = (id: string | null) =>
    directory?.actors.find((actor) => actor.id === id)?.label ||
    (id ? id : "Not assigned");
  const canAssign = role !== "auditor";
  const canReview = role === "reviewer" || role === "admin";
  const stale = Boolean(error) || loading || roleBusy;
  const actionReason = busy
    ? "An action is in progress."
    : stale
      ? "Read fresh workflow facts before making a change."
      : !canReview
        ? "Switch to the reviewer or admin synthetic demo role."
        : !actorId
          ? "Select a synthetic staff actor first."
          : actorId !== task?.assignee_id
            ? "Select the assigned synthetic staff actor. A different actor cannot accept or resolve this task."
            : "";
  const mutate = async (
    action: string,
    body: Record<string, unknown>,
    success: string,
  ) => {
    if (!flow || busy || stale) return;
    setBusy(true);
    try {
      const view = await api<CareflowView>(
        `/cases/${encodeURIComponent(data.id)}/careflow/${action}`,
        { ...careflowBinding(flow), ...body },
      );
      if (
        !latestFlow.current ||
        latestFlow.current.case_id !== view.case_id ||
        view.case_version >= latestFlow.current.case_version
      ) {
        latestFlow.current = view;
        setFlow(view);
      }
      if (action === "answer") {
        setAnswer((current) =>
          current.trim() === body.answer_text ? "" : current,
        );
      }
      if (action === "link-revision" || action === "close") setConfirmed(false);
      toast(success);
      changed();
    } catch (reason) {
      setError(message(reason));
      toast(
        `${message(reason)} Reload the workflow before another action.`,
        true,
      );
    } finally {
      setBusy(false);
    }
  };
  const chooseActor = async (id: string) => {
    if (!directory || busy || stale || !canAssign) return;
    if (id === directory.selected_actor_id) return;
    setAnswer("");
    setConfirmed(false);
    setBusy(true);
    try {
      setDirectory(
        await api<ActorDirectory>("/careflow/actor", {
          actor_id: id,
          expected_actor_version: directory.actor_version,
        }),
      );
      toast("Synthetic staff actor selected");
      refresh();
    } catch (reason) {
      setError(message(reason));
      toast(message(reason), true);
    } finally {
      setBusy(false);
    }
  };
  if (loading && !flow)
    return <State kind="loading" title="Reading clarification workflow" />;
  if (!flow || !directory)
    return (
      <State
        kind="error"
        title="Clarification workflow unavailable"
        retry={refresh}
      >
        {error || "The workflow service did not return a current snapshot."}
      </State>
    );
  const assignmentReason =
    busy || stale
      ? "Read fresh facts after the current request completes."
      : !canAssign
        ? "Auditors can inspect but cannot assign tasks."
        : !assignee
          ? "Choose a synthetic staff assignee."
          : !dueAt
            ? "Set a UTC due time within this synthetic session."
            : "";
  const stage = task ? STAGES.indexOf(task.status) : 0;
  const nextAction = !task
    ? "This card has no clarification tasks. Inspect release and response access below."
    : task.status === "open"
      ? "Choose an assignee and due time. Assignment is separate from acceptance."
      : task.status === "assigned"
        ? `Select ${actorLabel(task.assignee_id)} and the reviewer or admin role, then accept responsibility explicitly.`
        : task.status === "accepted"
          ? "The assigned actor can record a source-bound clarification. The note alone cannot close this issue."
          : task.status === "answered"
            ? "Prepare the clarified documents as a new child case, verify them, then return here to link that revision."
            : task.status === "revision_linked"
              ? "Inspect the linked revision before explicitly confirming resolution. Closure does not release a patient artifact."
              : "This task is closed against its linked revision. Continue in that revised case for artifact review and patient responses.";
  return (
    <div
      className="careflow"
      data-testid="careflow-panel"
      aria-busy={loading || busy}
    >
      <div className="careflow-heading">
        <div>
          <div className="careflow-label">
            From question to accountable follow-up
          </div>
          <h3>Clarification workspace</h3>
          <p>
            Track acceptance and source-backed resolution. Releasing a card does
            not complete a clinical issue.
          </p>
        </div>
        <button
          className="button secondary"
          onClick={refresh}
          disabled={loading || busy}
        >
          <Icon name="refresh" />
          Reload workflow
        </button>
      </div>
      <section
        className="careflow-next"
        aria-label="Next care action"
        aria-live="polite"
        data-testid="careflow-next-action"
      >
        <div className="careflow-label">Next step</div>
        <p>{nextAction}</p>
        <p className="control-reason">
          {flow.tasks.filter((item) => item.status === "closed").length} of{" "}
          {flow.tasks.length} tasks closed. This describes workflow records, not
          clinical success.
        </p>
      </section>
      {loading || busy ? (
        <p className="control-reason" role="status">
          {busy
            ? "Saving action. Please wait before reloading."
            : "Reading the latest workflow."}
        </p>
      ) : null}
      {error ? (
        <div className="inline-alert" role="alert">
          {error} Previous snapshot retained. Reload the workflow before making
          another change.
        </div>
      ) : null}
      <div className="careflow-notice warning">
        <strong>Anonymous synthetic staff simulation</strong>
        <p>
          {directory.demo_notice || flow.notice} These actors are not
          authenticated clinicians. Task closure never authorizes a medication
          regimen.
        </p>
      </div>
      <details data-testid="careflow-role-guide">
        <summary>Which demo role and staff actor should I use?</summary>
        <p>
          Start as Operator to assign follow-up or prepare revised documents.
          Switch to Reviewer, select the assigned staff actor and explicitly
          accept responsibility before recording that actor's answer. Reviewer
          decisions also control release and response follow-up. Admin can
          coordinate these demo steps; Auditor is read-only.
        </p>
        <p className="control-reason" data-testid="careflow-role-context">
          Current demo role: {label(role)}. Acting as:{" "}
          {actorId ? actorLabel(actorId) : "no staff actor selected"}. The role
          controls available actions; the selected actor identifies who records
          them. Changing either does not accept a task.
        </p>
      </details>
      <div className="careflow-toolbar">
        {onRoleChange ? (
          <label>
            Workflow demo role
            <select
              aria-label="Workflow demo role"
              value={role}
              disabled={busy || loading || roleBusy}
              onChange={(event) =>
                void onRoleChange(event.target.value as Role)
              }
            >
              <option value="operator">
                Operator: assign and prepare revisions
              </option>
              <option value="reviewer">
                Reviewer: accept, reply and review
              </option>
              <option value="admin">
                Admin: coordinate the synthetic journey
              </option>
              <option value="auditor">Auditor: inspect only</option>
            </select>
            <span className="control-reason">
              {roleBusy
                ? "Applying the requested demo role."
                : busy || loading
                  ? "Wait for the current request before changing role."
                  : "Demo permissions and selected staff actor are separate. These are not authenticated clinician identities."}
            </span>
          </label>
        ) : null}
        <label>
          Act as synthetic staff
          <select
            aria-label="Act as synthetic staff"
            value={actorId || ""}
            disabled={busy || stale || !canAssign}
            onChange={(event) => void chooseActor(event.target.value)}
          >
            <option value="" disabled>
              Select a demo actor
            </option>
            {directory.actors.map((actor) => (
              <option value={actor.id} key={actor.id}>
                {actor.label}
              </option>
            ))}
          </select>
        </label>
        <p className="control-reason">
          {!canAssign
            ? "Auditor lens is read-only."
            : busy || stale
              ? "Actor selection requires a fresh, idle workflow."
              : "Selecting an actor is explicit. Accepting an assignment is a separate action."}
        </p>
      </div>
      {!task ? (
        <State kind="empty" title="No clarification tasks">
          This verified card has no questions that require a follow-up task.
        </State>
      ) : (
        <div className="careflow-grid">
          <nav className="careflow-list" aria-label="Clarification tasks">
            {flow.tasks.map((item, index) => (
              <button
                data-testid={`careflow-task-${item.id}`}
                className="careflow-item"
                aria-pressed={task.id === item.id}
                key={item.id}
                disabled={busy || loading}
                onClick={() => selectTask(item.id)}
              >
                <span className={`careflow-status ${item.status}`}>
                  {label(item.status)}
                </span>
                <strong>
                  {index + 1}. {item.prompt}
                </strong>
                <small>{actorLabel(item.assignee_id)}</small>
              </button>
            ))}
          </nav>
          <section
            className="careflow-card"
            aria-labelledby="careflow-task-title"
            key={task.id}
          >
            <div className="careflow-title">
              <span className="careflow-label">{label(task.section)}</span>
              <span className={`careflow-status ${task.status}`}>
                {label(task.status)}
              </span>
            </div>
            <h3 id="careflow-task-title">{task.prompt}</h3>
            <ol className="careflow-steps" aria-label="Resolution progress">
              {STEPS.map((step, index) => (
                <li key={step} data-complete={stage >= index + 1}>
                  {index + 1}. {step}
                </li>
              ))}
            </ol>
            <dl className="careflow-facts">
              <div>
                <dt>Assigned owner</dt>
                <dd>{actorLabel(task.assignee_id)}</dd>
              </div>
              <div>
                <dt>Due</dt>
                <dd>
                  {task.due_at
                    ? `${clock(task.due_at)} · ${task.due_at}`
                    : "Not set"}
                </dd>
              </div>
              <div>
                <dt>Explicit acceptance</dt>
                <dd>
                  {task.accepted_at
                    ? clock(task.accepted_at)
                    : "Awaiting the assigned actor"}
                </dd>
              </div>
              <div>
                <dt>Captured source</dt>
                <dd className="mono">
                  {flow.source_hash.slice(0, 16)}… · generation{" "}
                  {flow.generation}
                </dd>
              </div>
            </dl>
            <details>
              <summary>
                Inspect cited original sources ({task.statement_ids.length})
              </summary>
              {task.statement_ids.map((id) => {
                const witness = data.result?.artifact?.source_statements.find(
                  (item) => item.statement_id === id,
                );
                return (
                  <div className="careflow-witness" key={id}>
                    <code>{id}</code>
                    {witness ? (
                      <>
                        <blockquote>{witness.citation.quote}</blockquote>
                        <p className="control-reason">
                          {witness.document_kind} · issued{" "}
                          {witness.issued_at || "not recorded"} · effective{" "}
                          {witness.effective_at || "not recorded"}
                        </p>
                      </>
                    ) : (
                      <p className="control-reason">
                        This witness is unavailable in the current card. Reload
                        the case before relying on it.
                      </p>
                    )}
                  </div>
                );
              })}
            </details>
            {task.status === "open" || task.status === "assigned" ? (
              <div className="careflow-form">
                <div className="careflow-fields">
                  <label>
                    Assign synthetic staff
                    <select
                      aria-label="Assign synthetic staff"
                      disabled={busy || stale}
                      value={assignee}
                      onChange={(event) => setAssignee(event.target.value)}
                    >
                      <option value="">Choose assignee</option>
                      {directory.actors.map((actor) => (
                        <option key={actor.id} value={actor.id}>
                          {actor.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Due date and time (UTC)
                    <input
                      type="datetime-local"
                      disabled={busy || stale}
                      value={dueAt}
                      onChange={(event) => setDueAt(event.target.value)}
                    />
                  </label>
                </div>
                <button
                  className="button secondary"
                  disabled={Boolean(assignmentReason)}
                  onClick={() =>
                    void mutate(
                      "assign",
                      {
                        task_id: task.id,
                        assignee_id: assignee,
                        due_at: `${dueAt}:00Z`,
                      },
                      "Assignment recorded; acceptance still required",
                    )
                  }
                >
                  Assign follow-up
                </button>
                <p className="control-reason">
                  {assignmentReason ||
                    "Assignment does not imply acceptance. Due time is entered explicitly in UTC."}
                </p>
              </div>
            ) : null}
            {task.status === "assigned" ? (
              <div className="careflow-form">
                <button
                  className="button"
                  disabled={Boolean(actionReason)}
                  onClick={() =>
                    void mutate(
                      "accept",
                      { task_id: task.id, actor_id: actorId },
                      "Assigned actor explicitly accepted responsibility",
                    )
                  }
                >
                  Accept responsibility
                </button>
                <p className="control-reason">
                  {actionReason ||
                    `Accept explicitly as ${actorLabel(actorId)}. This is a synthetic demonstration.`}
                </p>
              </div>
            ) : null}
            {task.status === "accepted" ? (
              <div className="careflow-form">
                <label>
                  Source-bound clarification note
                  <textarea
                    aria-label="Source-bound clarification note"
                    value={answer}
                    disabled={busy}
                    maxLength={2000}
                    onChange={(event) => setAnswer(event.target.value)}
                    placeholder="Record the synthetic care-team clarification. A separate verified source revision is required before closure."
                  />
                </label>
                <button
                  className="button"
                  disabled={Boolean(actionReason) || !answer.trim()}
                  onClick={() =>
                    void mutate(
                      "answer",
                      {
                        task_id: task.id,
                        actor_id: actorId,
                        answer_text: answer.trim(),
                      },
                      "Reviewer note recorded; source revision still required",
                    )
                  }
                >
                  Record clarification note
                </button>
                <p className="control-reason">
                  {actionReason ||
                    (!answer.trim()
                      ? "Enter a source-bound answer to enable this action."
                      : "This private note is not an authoritative prescription or patient instruction.")}
                </p>
              </div>
            ) : null}
            {task.answer ? (
              <div className="careflow-feedback">
                <strong>Recorded clarification note</strong>
                <p>{task.answer.text}</p>
                <p className="control-reason">
                  {actorLabel(task.answer.actor_id)} · {clock(task.answer.at)} ·
                  reviewer note, not treatment authority
                </p>
              </div>
            ) : null}
            {task.status === "answered" ? (
              <div className="careflow-form">
                {onRevise ? (
                  <>
                    <button
                      className="button secondary"
                      disabled={
                        busy ||
                        stale ||
                        (role !== "operator" && role !== "admin")
                      }
                      onClick={onRevise}
                    >
                      Prepare clarified source revision
                    </button>
                    <p className="control-reason">
                      {role !== "operator" && role !== "admin"
                        ? "Choose the operator or admin demo role to edit a new source revision."
                        : busy || stale
                          ? "Wait for fresh workflow facts before preparing a revision."
                          : "Opens the original documents as editable input for a new child case. Nothing runs until you confirm and submit."}
                    </p>
                  </>
                ) : null}
                <label>
                  Separately verified source revision
                  <select
                    aria-label="Separately verified source revision"
                    disabled={busy || stale}
                    value={revisionId}
                    onChange={(event) => setRevisionId(event.target.value)}
                  >
                    <option value="">Choose a verified child case</option>
                    {flow.candidate_revisions.map((candidate) => (
                      <option value={candidate.case_id} key={candidate.case_id}>
                        {candidate.case_id} · generation {candidate.generation}{" "}
                        · {label(candidate.status)}
                      </option>
                    ))}
                  </select>
                </label>
                <button
                  className="button secondary"
                  disabled={Boolean(actionReason) || !revisionId}
                  onClick={() =>
                    void mutate(
                      "link-revision",
                      {
                        task_id: task.id,
                        actor_id: actorId,
                        revision_case_id: revisionId,
                      },
                      "Verified source revision linked",
                    )
                  }
                >
                  Link verified revision
                </button>
                <p className="control-reason">
                  {actionReason ||
                    (!flow.candidate_revisions.length
                      ? "Create and independently verify a revised source packet using the case revision control. No eligible verified child case exists yet."
                      : !revisionId
                        ? "Select the verified child case that contains the clinical clarification."
                        : "Only a separately verified direct revision of this patient and source lineage can be linked.")}
                </p>
              </div>
            ) : null}
            {task.revision ? (
              <div className="careflow-feedback">
                <strong>Linked verified revision</strong>
                <p className="mono">
                  {task.revision.case_id} · generation{" "}
                  {task.revision.generation}
                </p>
                <Hash
                  title="Revision source hash"
                  value={task.revision.source_hash}
                />
                <Hash
                  title="Revision artifact hash"
                  value={task.revision.artifact_hash}
                />
                {onOpenCase ? (
                  <button
                    className="button secondary"
                    onClick={() => {
                      if (task.revision) onOpenCase(task.revision.case_id);
                    }}
                  >
                    Open linked verified revision
                  </button>
                ) : null}
              </div>
            ) : null}
            {task.status === "revision_linked" && task.revision ? (
              <div className="careflow-form">
                <label className="careflow-check">
                  <input
                    type="checkbox"
                    checked={confirmed}
                    disabled={busy || stale}
                    onChange={(event) => setConfirmed(event.target.checked)}
                  />
                  I reviewed this verified revision and confirm that it resolves
                  this issue in the synthetic case.
                </label>
                <button
                  className="button"
                  disabled={Boolean(actionReason) || !confirmed}
                  onClick={() =>
                    void mutate(
                      "close",
                      {
                        task_id: task.id,
                        actor_id: actorId,
                        clinician_confirmed: true,
                        revision_source_hash: task.revision?.source_hash,
                        revision_artifact_hash: task.revision?.artifact_hash,
                      },
                      "Demo closure recorded against the verified source revision",
                    )
                  }
                >
                  Confirm resolution and close
                </button>
                <p className="control-reason">
                  {actionReason ||
                    (!confirmed
                      ? "Explicit clinician confirmation is required after reviewing the linked revision."
                      : "Closing this task does not release a patient artifact or authorize a regimen.")}
                </p>
              </div>
            ) : null}
            {task.closure ? (
              <div className="careflow-notice">
                <strong>Resolution recorded</strong>
                <p>
                  {actorLabel(task.closure.actor_id)} confirmed the linked
                  source revision at {clock(task.closure.at)}. The original
                  source and card remain unchanged.
                </p>
              </div>
            ) : null}
            <details>
              <summary>Task event history ({task.events.length})</summary>
              <ol className="careflow-history">
                {task.events.map((event) => (
                  <li key={event.id}>
                    <time dateTime={event.at}>{clock(event.at)}</time>
                    <strong> {label(event.type)}</strong>
                    <p>{event.message}</p>
                  </li>
                ))}
              </ol>
            </details>
          </section>
        </div>
      )}
      <EvidenceSearch
        topic={
          task
            ? task.code.includes("pending") || task.code.includes("owner")
              ? "pending test responsibility accepted transfer"
              : task.section === "teachback"
                ? "teach-back understanding source follow-up"
                : "medication reconciliation source changes clarification"
            : undefined
        }
      />
      <TeachbackReview
        key={`${data.id}:${data.generation}:${data.artifact_hash}:${actorId || "unselected"}`}
        data={data}
        flow={flow}
        role={role}
        actorId={actorId}
        stale={stale || busy}
        refresh={refresh}
        toast={toast}
        onBusyChange={setBusy}
        onReviewRelease={onReviewRelease}
      />
    </div>
  );
}
