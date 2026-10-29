import type { Overview as OverviewData } from "./types";
import { clock, duration, label } from "./types";
import { Icon, PageHeader, State } from "./ui";
export default function Overview({
  data,
  loading,
  error,
  retry,
  drill,
  create,
  canCreate,
  drillDesk,
}: {
  data: OverviewData | null;
  loading: boolean;
  error: string;
  retry: () => void;
  drill: (status: string) => void;
  create: () => void;
  canCreate: boolean;
  drillDesk: (facilityId: string) => void;
}) {
  if (error)
    return (
      <State kind="error" title="Overview unavailable" retry={retry}>
        {error} No figures are shown.
      </State>
    );
  if (loading && !data)
    return (
      <State kind="loading" title="Reading session activity">
        Reading persisted prepared examples and new runs in this synthetic session.
      </State>
    );
  if (!data)
    return <State kind="empty" title="No session snapshot" retry={retry} />;
  const cards: {
    label: string;
    value: number;
    status: string;
    note: string;
    tone: string;
  }[] = [
    {
      label: "Cases in session",
      value: data.counts.total,
      status: "",
      note: data.origin_counts ? `${data.origin_counts.prepared_demo} prepared · ${data.origin_counts.live_run} new runs` : "Synthetic packets only",
      tone: "neutral",
    },
    {
      label: "Released",
      value: data.counts.released,
      status: "released",
      note: data.origin_counts?.prepared_demo ? "Includes prepared examples" : "Verified patient artifacts",
      tone: "success",
    },
    {
      label: "Awaiting review",
      value: data.counts.review_required,
      status: "review_required",
      note: "Correction or interaction flag",
      tone: "warning",
    },
    {
      label: "In progress",
      value: data.counts.queued + data.counts.processing,
      status: "processing",
      note: `${data.counts.queued} queued · ${data.counts.processing} processing`,
      tone: "accent",
    },
  ];
  const totalOutcomes = data.outcomes.reduce((sum, row) => sum + row.value, 0);
  const deskLoad = data.by_facility || [];
  const maxDeskLoad = Math.max(1, ...deskLoad.map((row) => row.count));
  const maxDuration = Math.max(
    1,
    ...data.durations.map((row) => row.duration_ms),
  );
  return (
    <>
      <PageHeader
        title="Operations overview"
        description="Every explanation has a source. Every release has a verification trail."
      >
        <button className="button secondary" onClick={retry} disabled={loading}>
          <Icon name="refresh" />
          {loading ? "Refreshing" : "Refresh"}
        </button>
        <button className="button" onClick={create} disabled={!canCreate}>
          <Icon name="plus" />
          New case
        </button>
      </PageHeader>
      {!canCreate ? (
        <p className="control-reason">
          The operator or admin synthetic demo role can create cases. Change
          role in the user menu.
        </p>
      ) : null}
      <div className="scope-line">
        <span className="live-dot" />
        {data.scope || "Current synthetic session"}
        <span>As of {clock(data.as_of)}</span>
        {loading ? <span role="status">Refreshing snapshot</span> : null}
      </div>
      <div className="kpi-grid">
        {cards.map((card) => (
          <button
            key={card.label}
            className={`kpi card ${card.tone}`}
            onClick={() => drill(card.status)}
          >
            <span className="kpi-label">
              {card.label}
              <Icon name={card.status === "released" ? "shield" : "arrow"} />
            </span>
            <strong>{card.value}</strong>
            <span className="muted">{card.note}</span>
          </button>
        ))}
      </div>
      {data.counts.total === 0 ? (
        <section className="welcome card">
          <div className="eyebrow">START WITH A SYNTHETIC PACKET</div>
          <h2>See the safety gate stop a changed dose.</h2>
          <p>
            Create a case with the dosage stress test. Follow the actual Strands
            Graph, inspect the refusal receipt, then review the corrected
            artifact.
          </p>
          <button className="button" disabled={!canCreate} onClick={create}>
            Run your first case
            <Icon name="arrow" />
          </button>
          {!canCreate ? (
            <p className="control-reason">
              Switch to operator or admin to start a case.
            </p>
          ) : null}
        </section>
      ) : null}
      <div className="chart-grid">
        <section className="card chart-card" data-testid="outcomes-chart">
          <div className="section-heading">
            <div>
              <div className="eyebrow">CASE DISTRIBUTION</div>
              <h2>Case states</h2>
            </div>
            <span className="subtle-tag">n = {data.counts.total}</span>
          </div>
          <p className="chart-scope">Persisted case states, including clearly labelled prepared examples. These counts are not measured clinical outcomes.</p>
          {totalOutcomes === 0 ? (
            <State kind="empty" title="No outcomes yet">
              Create a case to populate this chart.
            </State>
          ) : (
            <div
              className="outcome-bars"
              role="group"
              aria-label={data.outcomes
                .map((row) => `${label(row.label)}: ${row.value}`)
                .join(", ")}
            >
              {data.outcomes.map((row) => (
                <button
                  className="bar-row"
                  key={row.label}
                  onClick={() => drill(row.label)}
                >
                  <span>{label(row.label)}</span>
                  <svg
                    viewBox="0 0 200 20"
                    preserveAspectRatio="none"
                    aria-hidden="true"
                  >
                    <rect
                      width="200"
                      height="12"
                      y="4"
                      rx="4"
                      className="bar-track"
                    />
                    <rect
                      width={Math.max(0, (row.value / totalOutcomes) * 200)}
                      height="12"
                      y="4"
                      rx="4"
                      className={`bar-fill status-${row.label}`}
                    />
                  </svg>
                  <strong>{row.value}</strong>
                </button>
              ))}
            </div>
          )}
        </section>
        <section className="card chart-card" data-testid="desk-load-chart">
          <div className="section-heading">
            <div><div className="eyebrow">CURRENT COLLECTION</div><h2>Cases by synthetic desk</h2></div>
            <span className="subtle-tag">{deskLoad.length} desks</span>
          </div>
          <p className="chart-scope">Counts of persisted cases in this view, including prepared examples. Select a desk to open its register.</p>
          {deskLoad.length ? <div className="outcome-bars" role="group" aria-label="Case counts by synthetic desk">{deskLoad.map((row) => <button className="bar-row" key={row.facility_id} onClick={() => drillDesk(row.facility_id)}><span>{row.city}</span><svg viewBox="0 0 200 20" preserveAspectRatio="none" aria-hidden="true"><rect width="200" height="12" y="4" rx="4" className="bar-track"/><rect width={row.count / maxDeskLoad * 200} height="12" y="4" rx="4" className="bar-fill"/></svg><strong>{row.count}</strong></button>)}</div> : <State kind="empty" title="No desk counts available">The summary has not returned facility counts.</State>}
        </section>
      </div>
      {data.durations.length ? (
        <section className="card chart-card live-duration-panel" data-testid="live-duration-chart">
          <div className="section-heading">
            <div>
              <div className="eyebrow">MEASURED NEW RUNS ONLY</div>
              <h2>New-run processing duration</h2>
            </div>
            <span className="subtle-tag">Seconds</span>
          </div>
          <p className="chart-scope">Actual completed worker timings from new verification runs. Prepared example durations are excluded; the run model is recorded in each case.</p>
          {data.durations.length === 0 ? (
            <State kind="empty" title="No completed durations">
              A completed worker run records its measured duration.
            </State>
          ) : (
            <div className="duration-chart">
              <svg
                viewBox="0 0 500 180"
                role="img"
                aria-label={data.durations
                  .map((row) => `${row.label}: ${duration(row.duration_ms)}`)
                  .join(", ")}
              >
                <line
                  x1="25"
                  y1="150"
                  x2="490"
                  y2="150"
                  className="chart-axis"
                />
                {data.durations.map((row, index) => {
                  const width = 430 / data.durations.length;
                  const height = (row.duration_ms / maxDuration) * 110;
                  return (
                    <g key={row.id}>
                      <rect
                        x={35 + width * index}
                        y={150 - height}
                        width={Math.max(5, width - 12)}
                        height={height}
                        rx="4"
                        className="duration-bar"
                      />
                      <text x={35 + width * index} y={140 - height}>
                        {duration(row.duration_ms)}
                      </text>
                      <text x={35 + width * index} y="169">
                        {index + 1}
                      </text>
                    </g>
                  );
                })}
              </svg>
              <ol className="chart-legend">
                {data.durations.map((row, i) => (
                  <li key={row.id}>
                    <span>{i + 1}</span>
                    {row.label}
                  </li>
                ))}
              </ol>
            </div>
          )}
        </section>
      ) : <section className="card new-run-callout"><div><strong>Run a new verification to measure its duration.</strong><p>Prepared examples populate the workflow without running Bedrock. They contribute no invented processing times.</p></div><button className="button secondary" onClick={create} disabled={!canCreate}>Run a new case</button>{!canCreate ? <p className="control-reason">Switch to operator or admin to start a new run.</p> : null}</section>}
      <section className="card guard-note">
        <Icon name="shield" />
        <div>
          <strong>Fidelity to the source, with visible limits.</strong>
          <p>
            The gate checks supported facts and constrained text. Selected
            drug-interaction rules are demonstrations. This console does not
            establish clinical appropriateness.
          </p>
        </div>
      </section>
    </>
  );
}
