import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, api, key, message } from "./api";
import type { Bootstrap, CaseSummary, IntakeTemplate } from "./types";
import { Dialog, Icon, State } from "./ui";

type InputMode = "sample" | "import";

function submissionError(cause: unknown, importing: boolean): string {
  if (!importing || !(cause instanceof ApiError)) return message(cause);
  if (cause.status === 413)
    return "The synthetic note is too large. Shorten it to the supported character and line limits.";
  if (cause.status === 422)
    return `The synthetic note was not accepted. ${cause.message}`;
  if (cause.status >= 500)
    return "The intake service is temporarily unavailable. Read the case register before retrying.";
  return cause.message;
}

export default function NewCase({
  bootstrap,
  stressDefault,
  workspace,
  close,
  done,
  failed,
  canCreate,
  count,
}: {
  bootstrap: Bootstrap;
  stressDefault: boolean;
  workspace: string;
  close: () => void;
  done: (id: string) => void;
  failed: (text: string) => void;
  canCreate: boolean;
  count: number;
}) {
  const initial = stressDefault
    ? bootstrap.packets.find((item) => item.stress_available) ||
      bootstrap.packets[0]
    : bootstrap.packets[0];
  const [packet, setPacket] = useState(initial?.id || "");
  const [facility, setFacility] = useState(
    workspace === "all" ? bootstrap.facilities[0]?.id || "" : workspace,
  );
  const [stress, setStress] = useState(
    Boolean(initial?.stress_available) && stressDefault,
  );
  const [mode, setMode] = useState<InputMode>("sample");
  const [template, setTemplate] = useState<IntakeTemplate | null>(null);
  const [templateError, setTemplateError] = useState("");
  const [templateRevision, setTemplateRevision] = useState(0);
  const [source, setSource] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const idempotency = useRef(key());
  const submitting = useRef(false);
  useEffect(() => {
    if (mode !== "import" || template) return;
    const controller = new AbortController();
    setTemplateError("");
    void api<IntakeTemplate>("/intake-template", undefined, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) setTemplate(value);
      })
      .catch((cause) => {
        if (!controller.signal.aborted)
          setTemplateError(
            cause instanceof ApiError && cause.status >= 500
              ? "The supported intake format could not be loaded. Retry this read."
              : message(cause),
          );
      });
    return () => controller.abort();
  }, [mode, template, templateRevision]);
  const edited = () => {
    idempotency.current = key();
    setError("");
  };
  const selected = bootstrap.packets.find((item) => item.id === packet);
  const lineCount = source ? source.split(/\r\n|\r|\n/).length : 0;
  const sourceLength = Array.from(source).length;
  const importReason = !template
    ? templateError
      ? "Load the supported intake format before submitting. Your note stays in this dialog."
      : "Loading the supported intake format."
    : !source.trim()
      ? "Enter a synthetic discharge note or load the editable template."
      : sourceLength > template.limits.max_source_chars
        ? `The note exceeds ${template.limits.max_source_chars.toLocaleString()} characters. Shorten it; text is never silently truncated.`
        : lineCount > template.limits.max_lines
          ? `The note exceeds ${template.limits.max_lines} lines. Use the supported format and shorten it.`
          : !confirmed
            ? "Confirm that this note is invented and contains no real patient information."
            : "";
  const reason = busy
    ? "The case request is in progress. Do not resubmit."
    : !canCreate
      ? "Switch to the operator or admin demo role to create cases."
      : count >= bootstrap.limits.max_cases
        ? `This session has reached its ${bootstrap.limits.max_cases}-case limit. Start a new session in Settings.`
        : !facility
          ? "Select an available synthetic desk."
          : mode === "import"
            ? importReason
            : !packet
              ? "Select an available packet."
              : "";
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (reason || submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setError("");
    try {
      const response = await api<{
        case: CaseSummary;
        created: boolean;
        job_id: string;
      }>(
        mode === "import" ? "/cases/import" : "/cases",
        mode === "import"
          ? {
              source_text: source,
              synthetic_confirmed: confirmed,
              facility_id: facility,
              idempotency_key: idempotency.current,
            }
          : {
              packet_id: packet,
              facility_id: facility,
              stress,
              idempotency_key: idempotency.current,
            },
      );
      done(response.case.id);
    } catch (cause) {
      const text = submissionError(cause, mode === "import");
      setError(
        `${text} Check the case register before retrying. The same submission identifier is retained.`,
      );
      failed(text);
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  };
  return (
    <Dialog title="New verification case" onClose={close}>
      <form className="new-case-body" onSubmit={(event) => void submit(event)}>
        <div className="eyebrow">SYNTHETIC INPUT ONLY</div>
        <p>
          Choose a sample or enter a new synthetic note. Each new case runs the
          actual Strands Graph, tools and lifecycle safety gate.
        </p>
        <fieldset className="intake-mode" disabled={busy} aria-describedby="new-case-reason">
          <legend>Source type</legend>
          <label>
            <input
              type="radio"
              name="intake-mode"
              value="sample"
              checked={mode === "sample"}
              onChange={() => {
                setMode("sample");
                edited();
              }}
            />
            Sample packet
          </label>
          <label>
            <input
              type="radio"
              name="intake-mode"
              value="import"
              checked={mode === "import"}
              onChange={() => {
                setMode("import");
                edited();
              }}
              data-testid="import-note-mode"
            />
            Import synthetic note
          </label>
        </fieldset>
        {mode === "sample" ? (
          <>
        <label>
          Discharge packet
          <select
            aria-label="Discharge packet"
            value={packet}
            disabled={busy}
            onChange={(event) => {
              const next = bootstrap.packets.find(
                (item) => item.id === event.target.value,
              );
              setPacket(event.target.value);
              setStress(Boolean(next?.stress_available));
              edited();
            }}
          >
            {bootstrap.packets.map((item) => (
              <option key={item.id} value={item.id}>
                {item.title}
              </option>
            ))}
          </select>
        </label>
        <div className="packet-preview">
          <Icon name="cases" />
          <div>
            <strong>{selected?.title}</strong>
            <p>{selected?.summary}</p>
          </div>
        </div>
          </>
        ) : (
          <section className="intake-editor" aria-label="Synthetic note intake">
            <div className="notice">
              Invented text only. Do not paste patient records, names, dates of
              birth or contact details. This confirmation is not a privacy
              guarantee or automatic detection of patient information.
            </div>
            {!template ? (
              templateError ? (
                <State
                  kind="error"
                  title="Intake format unavailable"
                  retry={() => setTemplateRevision((value) => value + 1)}
                >
                  {templateError}
                </State>
              ) : (
                <p className="muted" role="status">Loading supported intake format...</p>
              )
            ) : (
              <>
                <div className="intake-template-actions">
                  <button
                    type="button"
                    className="button secondary"
                    data-testid="load-intake-template"
                    disabled={busy}
                    onClick={() => {
                      setSource(template.source_text);
                      setConfirmed(false);
                      edited();
                    }}
                  >
                    {source ? "Replace with synthetic template" : "Load editable template"}
                  </button>
                  <span className="muted">Edit the template to create a new source.</span>
                </div>
                <details className="intake-format">
                  <summary>Supported note format and limits</summary>
                  <ul>
                    {template.format_hints.map((hint) => <li key={hint}>{hint}</li>)}
                  </ul>
                  <p>
                    Up to {template.limits.max_medications} medicines, {template.limits.max_labs} labs,
                    {" "}{template.limits.max_lines} lines and {template.limits.max_source_chars.toLocaleString()} characters.
                    Unknown or ambiguous lines are refused before a model runs.
                  </p>
                </details>
              </>
            )}
            <label htmlFor="synthetic-source-note">Synthetic discharge note</label>
            <textarea
              id="synthetic-source-note"
              data-testid="intake-source-text"
              aria-describedby="intake-source-limits new-case-reason"
              aria-invalid={Boolean(template && (sourceLength > template.limits.max_source_chars || lineCount > template.limits.max_lines))}
              value={source}
              disabled={busy}
              spellCheck={false}
              autoComplete="off"
              placeholder="Load the editable template to see the supported format."
              onChange={(event) => {
                setSource(event.target.value);
                setConfirmed(false);
                edited();
              }}
            />
            <p id="intake-source-limits" className="intake-counter">
              {sourceLength.toLocaleString()}{template ? ` / ${template.limits.max_source_chars.toLocaleString()}` : ""} characters
              {" · "}{lineCount}{template ? ` / ${template.limits.max_lines}` : ""} lines
            </p>
            <details className="intake-preview">
              <summary>Preview exact submitted source</summary>
              <pre data-testid="intake-source-preview">{source || "No note entered yet."}</pre>
              <p className="muted">This text is captured with its own SHA-256. Deterministic format validation precedes the parser Agent's independent extraction check.</p>
            </details>
            <label className="check-field">
              <input
                type="checkbox"
                checked={confirmed}
                disabled={busy}
                data-testid="confirm-synthetic-note"
                onChange={(event) => {
                  setConfirmed(event.target.checked);
                  edited();
                }}
              />
              <span>
                <strong>I confirm this note is invented and contains no real patient information.</strong>
                <span>{template?.privacy_notice || "Synthetic-only confirmation does not detect or remove personal information."}</span>
              </span>
            </label>
            <p className="muted">Text intake only. PDF upload, arbitrary FHIR and real clinical records are not supported.</p>
          </section>
        )}
        <label>
          Synthetic operations desk
          <select
            aria-label="Synthetic operations desk"
            value={facility}
            disabled={busy}
            onChange={(event) => {
              setFacility(event.target.value);
              edited();
            }}
          >
            {bootstrap.facilities.map((item) => (
              <option key={item.id} value={item.id}>
                {item.city} · {item.name}
              </option>
            ))}
          </select>
        </label>
        {mode === "sample" ? (
          <>
        <label className="check-field">
          <input
            type="checkbox"
            checked={stress}
            disabled={busy || !selected?.stress_available}
            onChange={(event) => {
              setStress(event.target.checked);
              edited();
            }}
          />
          <span>
            <strong>Deliberate dosage mutation stress test</strong>
            <span>
              Inject a changed candidate dose. The gate must refuse it and route
              a correction. The source remains unchanged.
            </span>
          </span>
        </label>
        {!selected?.stress_available ? (
          <p className="control-reason">
            The dosage mutation stress test is available only for the designated
            stress packet. Select that packet to enable it.
          </p>
        ) : null}
          </>
        ) : null}
        {error ? (
          <div className="inline-alert" role="alert" data-testid="intake-error">
            {error}
          </div>
        ) : null}
        <button
          className="button full"
          type="submit"
          disabled={Boolean(reason)}
          aria-describedby="new-case-reason"
          data-testid="start-verification"
        >
          <Icon name={busy ? "refresh" : "shield"} />
          {busy ? "Creating case..." : "Start verification"}
        </button>
        <p className="control-reason" id="new-case-reason" data-testid="new-case-reason" aria-live="polite">
          {reason ||
            (mode === "import"
              ? "Captures this synthetic note and queues one real verification job. The parser Agent must match the independently validated source before an explanation can publish."
              : "Creates one real queued job in your anonymous session from the selected synthetic packet.")}
        </p>
      </form>
    </Dialog>
  );
}
