import { lazy, Suspense, useEffect, useState } from "react";
import { api, message } from "./api";
import { Badge, OriginBadge, PageHeader, State } from "./ui";
import type { CaseDetail, CaseSummary, Facility } from "./types";
const FacilityMap = lazy(() => import("./FacilityMap"));
const LEGACY_NODES = [
  {
    id: "LabAndDischargeParserAgent",
    title: "Parse source",
    subtitle: "Lab & discharge parser",
    tool: "Deterministic reference-range evaluator",
    x: 24,
    y: 60,
  },
  {
    id: "DrugAndContraindicationVerifierAgent",
    title: "Verify medicines",
    subtitle: "Drug & contraindication verifier",
    tool: "Dose/frequency parity and curated interaction checks",
    x: 260,
    y: 60,
  },
  {
    id: "PatientAdvocateExplainerAgent",
    title: "Explain for patient",
    subtitle: "Patient advocate",
    tool: "Structured source-grounded explanation builder",
    x: 496,
    y: 60,
  },
  {
    id: "AtroposClinicalSafetyGate",
    title: "Atropos safety gate",
    subtitle: "Lifecycle publication validator",
    tool: "Exact source fidelity and SHA-256 receipt",
    x: 732,
    y: 60,
  },
];
const RECONCILIATION_NODES = [
  {
    id: "DischargeEvidenceIndexerAgent",
    title: "Index original sources",
    subtitle: "Discharge evidence indexer",
    tool: "Read original documents and bind their source index and hashes",
    x: 24,
    y: 60,
  },
  {
    id: "CrossDocumentClarificationPlannerAgent",
    title: "Plan clarification",
    subtitle: "Cross-document planner",
    tool: "Check source constraints and pinned references; select bounded questions",
    x: 260,
    y: 60,
  },
  {
    id: "ClarificationCardPublisherAgent",
    title: "Build clarification",
    subtitle: "Clarification card publisher",
    tool: "Build the source-bound clarification card; never authorize a medication regimen",
    x: 496,
    y: 60,
  },
  {
    id: "AtroposReconciliationSafetyGate",
    title: "Reconciliation gate",
    subtitle: "Lifecycle integrity validator",
    tool: "Verify source atoms, original citations and allowed questions; emit the receipt chain",
    x: 732,
    y: 60,
  },
];
export default function Maps({
  facilities,
  cases,
  loading,
  error,
  retry,
  selectDesk,
  openCase,
}: {
  facilities: Facility[];
  cases: CaseSummary[];
  loading: boolean;
  error: string;
  retry: () => void;
  selectDesk: (id: string) => void;
  openCase: (id: string) => void;
}) {
  const [view, setView] = useState("Workflow");
  const [caseId, setCaseId] = useState("");
  const [fetchedDetail, setDetail] = useState<CaseDetail | null>(null);
  const [detailError, setDetailError] = useState("");
  const [node, setNode] = useState(LEGACY_NODES[0].id);
  useEffect(() => {
    if (!caseId) {
      setDetail(null);
      return;
    }
    const controller = new AbortController();
    setDetail(null);
    setDetailError("");
    void api<CaseDetail>(
      `/cases/${encodeURIComponent(caseId)}`,
      undefined,
      controller.signal,
    )
      .then(setDetail)
      .catch((reason) => {
        if (!controller.signal.aborted) setDetailError(message(reason));
      });
    return () => controller.abort();
  }, [caseId, cases]);
  const detail = fetchedDetail?.id === caseId ? fetchedDetail : null;
  const selectedCase = cases.find((item) => item.id === caseId);
  const reconciliation =
    (detail || selectedCase)?.source_profile === "medplain-reconciliation-v1";
  const nodes = reconciliation ? RECONCILIATION_NODES : LEGACY_NODES;
  const selected = nodes.find((item) => item.id === node) || nodes[0];
  const activeNode = selected.id;
  const trace =
    detail?.result?.trace.filter(
      (item) =>
        (item.agent || item.node || "").includes(activeNode) ||
        (activeNode === "AtroposClinicalSafetyGate" &&
          (item.agent || item.node || "").includes("Atropos")),
    ) || [];
  return (
    <>
      <PageHeader
        title="Execution & facility maps"
        description="Inspect how verification runs, and where synthetic operations desks are located."
      />
      <div className="tabs" role="tablist" aria-label="Map views">
        {["Workflow", "Facilities"].map((item) => (
          <button
            key={item}
            role="tab"
            aria-selected={view === item}
            onClick={() => setView(item)}
          >
            {item}
          </button>
        ))}
      </div>
      {error ? (
        <State kind="error" title="Map activity unavailable" retry={retry}>
          {error}
        </State>
      ) : loading && !cases.length ? (
        <State kind="loading" title="Loading map activity" />
      ) : view === "Facilities" ? (
        <section className="card map-card">
          <Suspense
            fallback={<State kind="loading" title="Loading geographic map" />}
          >
            <FacilityMap
              facilities={facilities}
              cases={cases}
              select={selectDesk}
            />
          </Suspense>
          <div className="facility-grid">
            {facilities.map((facility) => (
              <button
                className="facility-tile"
                key={facility.id}
                onClick={() => selectDesk(facility.id)}
              >
                <span className="eyebrow">SYNTHETIC DESK</span>
                <strong>{facility.city}</strong>
                <span>{facility.name}</span>
                <span className="subtle-tag">
                  {
                    cases.filter((item) => item.facility_id === facility.id)
                      .length
                  }{" "}
                  cases
                </span>
              </button>
            ))}
          </div>
          {!facilities.length ? (
            <State kind="empty" title="No synthetic facilities configured" />
          ) : null}
        </section>
      ) : (
        <>
          <section className="card flow-card">
            <div className="section-heading">
              <div>
                <div className="eyebrow">LOAD-BEARING STRANDS GRAPH</div>
                <h2>Three agents. One publication gate.</h2>
              </div>
              <label className="compact-field">
                Recorded case
                <select
                  aria-label="Recorded case"
                  value={caseId}
                  onChange={(event) => setCaseId(event.target.value)}
                >
                  <option value="">Architecture only</option>
                  {cases.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.origin === "prepared_demo"
                        ? "Prepared example"
                        : item.origin === "live_run"
                          ? "New run"
                          : "Origin unrecorded"}
                      : {item.packet_title} · {item.id.slice(0, 6)}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <p className="muted">
              {reconciliation
                ? "Multisource reconciliation profile. "
                : "Simple discharge profile. "}
              Choose a node to inspect its purpose. Choose a case to overlay
              recorded evidence. The diagram describes code structure; it does
              not simulate a live run.
            </p>
            <div className="workflow-scroll">
              <svg
                viewBox="0 0 980 245"
                className="workflow"
                role="group"
                aria-label={
                  reconciliation
                    ? "Evidence indexer to cross-document clarification planner to clarification publisher to reconciliation safety gate. Refusal returns to clarification planning."
                    : "Parser to medication verifier to patient advocate to safety gate. Refusal returns to medication verification."
                }
                data-testid="workflow-map"
                data-source-profile={
                  reconciliation
                    ? "medplain-reconciliation-v1"
                    : "simple-discharge"
                }
              >
                <defs>
                  <marker
                    id="arrowhead"
                    markerWidth="8"
                    markerHeight="8"
                    refX="7"
                    refY="4"
                    orient="auto"
                  >
                    <path d="M0 0 8 4 0 8" fill="none" stroke="currentColor" />
                  </marker>
                </defs>
                <path
                  d="M224 105h35m201 0h35m201 0h35"
                  className="flow-edge"
                  markerEnd="url(#arrowhead)"
                />
                <path
                  d="M830 151v50H360v-50"
                  className="correction-edge"
                  markerEnd="url(#arrowhead)"
                />
                <text x="530" y="229" className="correction-label">
                  Refused candidate: automatic correction handoff
                </text>
                {nodes.map((item) => (
                  <g
                    key={item.id}
                    role="button"
                    tabIndex={0}
                    aria-label={`Inspect ${item.title}`}
                    aria-pressed={activeNode === item.id}
                    onClick={() => setNode(item.id)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        setNode(item.id);
                      }
                    }}
                    className={`flow-node ${activeNode === item.id ? "selected" : ""}`}
                  >
                    <rect
                      x={item.x}
                      y={item.y}
                      width="200"
                      height="92"
                      rx="12"
                    />
                    <text x={item.x + 16} y={item.y + 34}>
                      {item.title}
                    </text>
                    <text
                      x={item.x + 16}
                      y={item.y + 57}
                      className="node-subtitle"
                    >
                      {item.subtitle}
                    </text>
                    <text
                      x={item.x + 16}
                      y={item.y + 76}
                      className="node-caption"
                    >
                      {detail?.result?.trace.some((entry) =>
                        (entry.agent || entry.node || "").includes(item.id),
                      )
                        ? detail.origin === "prepared_demo"
                          ? "Prepared example evidence"
                          : "Recorded in selected case"
                        : caseId
                          ? "Inspect evidence below"
                          : "Select to inspect"}
                    </text>
                  </g>
                ))}
              </svg>
            </div>
          </section>
          <section className="card node-detail">
            <div className="eyebrow">SELECTED GRAPH NODE</div>
            <h2>{selected.title}</h2>
            <p className="mono break">{selected.id}</p>
            <p>{selected.tool}</p>
            {detail ? (
              <div className="map-case-origin">
                <OriginBadge origin={detail.origin} />
                <p>
                  {detail.origin === "prepared_demo"
                    ? "Prepared offline fixture trace. It illustrates this saved example and is not a fresh Bedrock execution."
                    : detail.origin === "live_run"
                      ? detail.result?.model_mode === "bedrock"
                        ? "Recorded evidence from a new Amazon Bedrock run."
                        : detail.result?.model_mode === "fixture"
                          ? "Recorded evidence from a new offline fixture run."
                          : "New run; model evidence is not available yet."
                      : "This legacy record has no recorded origin."}
                </p>
              </div>
            ) : null}
            {detailError ? (
              <p role="alert" className="warning-text">
                {detailError}
              </p>
            ) : caseId && !detail ? (
              <State kind="loading" title="Reading recorded execution" />
            ) : !caseId ? (
              <p className="muted">
                No case selected. These are architecture labels, not execution
                events.
              </p>
            ) : (
              <>
                {trace.length ? (
                  trace.map((item, i) => (
                    <article key={i} className="receipt-card">
                      <Badge status={item.status} />
                      <p>{item.message}</p>
                    </article>
                  ))
                ) : (
                  <p className="muted">
                    No node trace is recorded for this selected node in the
                    current snapshot.
                  </p>
                )}
                {detail ? (
                  <button
                    className="button secondary"
                    onClick={() => openCase(detail.id)}
                  >
                    Open full case evidence
                  </button>
                ) : null}
              </>
            )}
          </section>
        </>
      )}
    </>
  );
}
