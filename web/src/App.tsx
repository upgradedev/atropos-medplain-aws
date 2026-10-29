import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import {
  api,
  boot,
  changeRole,
  key,
  loadExamples,
  message,
  newSession,
} from "./api";
import {
  clock,
  isActive,
  label,
  STATUSES,
  type Bootstrap,
  type CaseList,
  type CaseSummary,
  type Overview as OverviewData,
  type Role,
} from "./types";
import { Badge, Icon, OriginBadge, PageHeader, State } from "./ui";
import Overview from "./Overview";
import CasesTable, { type Filters } from "./CasesTable";
import CaseDrawer from "./CaseDrawer";
import Patient from "./Patient";
import NewCase from "./NewCase";
import type { ReconciliationSeed } from "./ReconciliationForm";
import EvaluationPanel from "./EvaluationPanel";
import ReconciliationEvaluationPanel from "./ReconciliationEvaluationPanel";
const Maps = lazy(() => import("./Maps"));
const PAGES = ["overview", "cases", "queues", "maps", "settings"] as const;
type Page = (typeof PAGES)[number];
interface Route {
  page: Page;
  workspace: string;
  q: string;
  status: string;
  caseId: string;
}
function readRoute(): Route {
  const url = new URL(location.href);
  const segment = url.pathname.replace(/^\/console\/?/, "").split("/")[0];
  return {
    page: PAGES.includes(segment as Page) ? (segment as Page) : "overview",
    workspace: url.searchParams.get("workspace") || "all",
    q: url.searchParams.get("q") || "",
    status: url.searchParams.get("status") || "",
    caseId: url.searchParams.get("case") || "",
  };
}
function routeUrl(route: Route) {
  const params = new URLSearchParams();
  if (route.workspace !== "all") params.set("workspace", route.workspace);
  if (route.q) params.set("q", route.q);
  if (route.status) params.set("status", route.status);
  if (route.caseId) params.set("case", route.caseId);
  return `/console/${route.page}${params.size ? `?${params}` : ""}`;
}
const ROLES: Role[] = ["operator", "reviewer", "auditor", "admin"];
export default function App() {
  const patient = location.pathname.match(/^\/console\/patient\/([^/]+)\/?$/);
  return patient ? (
    <Patient share={decodeURIComponent(patient[1])} />
  ) : (
    <Console />
  );
}
function Console() {
  const [route, setRoute] = useState<Route>(readRoute);
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [bootError, setBootError] = useState("");
  const [bootRevision, setBootRevision] = useState(0);
  const [collapsed, setCollapsed] = useState(false);
  const [mobile, setMobile] = useState(false);
  const [userMenu, setUserMenu] = useState(false);
  const [search, setSearch] = useState(route.q);
  const [overview, setOverview] = useState<OverviewData | null>(null);
  const [sessionCaseTotal, setSessionCaseTotal] = useState<number | null>(null);
  const [list, setList] = useState<CaseList | null>(null);
  const [cohort, setCohort] = useState<CaseSummary[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const [filters, setFilters] = useState<Filters>({
    status: route.status,
    sort: "created_at",
    direction: "desc",
    limit: 10,
    cursor: "",
  });
  const [createOpen, setCreateOpen] = useState(false);
  const [reconciliationSeed, setReconciliationSeed] =
    useState<ReconciliationSeed | null>(null);
  const [stressDefault, setStressDefault] = useState(false);
  const [sessionBusy, setSessionBusy] = useState(false);
  const [tour, setTour] = useState(false);
  const [theme, setTheme] = useState(() => {
    try {
      return localStorage.getItem("medplain.theme.v2") || "dark";
    } catch {
      return "dark";
    }
  });
  const [toasts, setToasts] = useState<
    { id: string; text: string; error: boolean }[]
  >([]);
  const searchRef = useRef<HTMLInputElement>(null);
  const pollCount = useRef(0);
  const searchTyping = useRef(false);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);
  const navigate = useCallback((change: Partial<Route>, replace = false) => {
    setRoute((current) => {
      const next = { ...current, ...change };
      history[replace ? "replaceState" : "pushState"]({}, "", routeUrl(next));
      return next;
    });
    setMobile(false);
  }, []);
  const toast = useCallback((text: string, error = false) => {
    const id = key();
    setToasts((current) => [...current.slice(-3), { id, text, error }]);
    window.setTimeout(
      () => setToasts((current) => current.filter((item) => item.id !== id)),
      8500,
    );
  }, []);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("medplain.theme.v2", theme);
    } catch {
      /* The selected theme still applies to this tab. */
    }
  }, [theme]);
  useEffect(() => {
    const pop = () => {
      const next = readRoute();
      setRoute(next);
      setSearch(next.q);
      setFilters((current) => ({
        ...current,
        status: next.status,
        cursor: "",
      }));
    };
    window.addEventListener("popstate", pop);
    return () => window.removeEventListener("popstate", pop);
  }, []);
  useEffect(() => {
    const keyboard = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      if (
        event.key === "/" &&
        !event.metaKey &&
        !event.ctrlKey &&
        !["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) &&
        !target.isContentEditable &&
        !document.querySelector("dialog[open]")
      ) {
        event.preventDefault();
        searchRef.current?.focus();
      }
    };
    window.addEventListener("keydown", keyboard);
    return () => window.removeEventListener("keydown", keyboard);
  }, []);
  useEffect(() => {
    if (!searchTyping.current) return;
    const timer = window.setTimeout(() => {
      searchTyping.current = false;
      setFilters((current) => ({ ...current, cursor: "" }));
      navigate({ page: "cases", q: search, caseId: "" }, true);
    }, 300);
    return () => window.clearTimeout(timer);
  }, [search, navigate]);
  useEffect(() => {
    let active = true;
    setBootError("");
    void boot()
      .then((data) => {
        if (active) setBootstrap(data);
      })
      .catch((reason) => {
        if (active) setBootError(message(reason));
      });
    return () => {
      active = false;
    };
  }, [bootRevision]);
  useEffect(() => {
    setOverview(null);
    setCohort([]);
    setList(null);
  }, [route.workspace]);
  useEffect(() => {
    setList(null);
  }, [route.q, route.status]);
  useEffect(() => {
    setSessionCaseTotal(null);
  }, [bootstrap?.session.session_id]);
  useEffect(() => {
    if (!bootstrap) return;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    const params = new URLSearchParams({
      workspace: route.workspace,
      q: route.q,
      status: route.status,
      sort: filters.sort,
      direction: filters.direction,
      limit: String(filters.limit),
      ...(filters.cursor ? { cursor: filters.cursor } : {}),
    });
    const overviewRequest = api<OverviewData>(
      `/overview?workspace=${encodeURIComponent(route.workspace)}`,
      undefined,
      controller.signal,
    );
    const sessionOverviewRequest =
      route.workspace === "all"
        ? overviewRequest
        : api<OverviewData>(
            "/overview?workspace=all",
            undefined,
            controller.signal,
          );
    void Promise.all([
      overviewRequest,
      api<CaseList>(`/cases?${params}`, undefined, controller.signal),
      api<CaseList>(
        `/cases?workspace=${encodeURIComponent(route.workspace)}&limit=20&sort=created_at&direction=desc`,
        undefined,
        controller.signal,
      ),
      sessionOverviewRequest,
    ])
      .then(([stats, page, all, sessionStats]) => {
        if (!controller.signal.aborted) {
          setOverview(stats);
          setList(page);
          setCohort(all.items);
          setSessionCaseTotal(sessionStats.counts.total);
        }
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(message(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [
    bootstrap,
    route.workspace,
    route.q,
    route.status,
    filters.sort,
    filters.direction,
    filters.limit,
    filters.cursor,
    revision,
  ]);
  const active = cohort.some((item) => isActive(item.status));
  useEffect(() => {
    if (!active) {
      pollCount.current = 0;
      return;
    }
    if (pollCount.current >= 120) return;
    const timer = window.setTimeout(() => {
      pollCount.current += 1;
      refresh();
    }, 2500);
    return () => window.clearTimeout(timer);
  }, [active, revision, refresh]);
  const closeCase = useCallback(() => navigate({ caseId: "" }), [navigate]);
  const openCase = useCallback(
    (id: string) => navigate({ caseId: id }),
    [navigate],
  );
  const selectDesk = useCallback(
    (id: string) => {
      setFilters((current) => ({ ...current, cursor: "" }));
      navigate({ workspace: id, page: "cases", caseId: "" });
    },
    [navigate],
  );
  const setRole = async (role: Role) => {
    setSessionBusy(true);
    try {
      const data = await changeRole(role);
      setBootstrap(data);
      toast(`Synthetic demo role: ${role}`);
    } catch (reason) {
      toast(message(reason), true);
    } finally {
      setSessionBusy(false);
    }
  };
  const resetSession = async (preparedDemo: boolean) => {
    setSessionBusy(true);
    try {
      const data = await newSession(preparedDemo);
      setBootstrap(data);
      setOverview(null);
      setList(null);
      setCohort([]);
      setSearch("");
      setFilters((current) => ({ ...current, cursor: "", status: "" }));
      navigate({
        page: "overview",
        workspace: "all",
        q: "",
        status: "",
        caseId: "",
      });
      toast(
        preparedDemo
          ? "Opened a populated synthetic demo. Prepared examples are labelled; no Bedrock calls were made to populate it."
          : "Started an empty session. It will stay empty until you create a case or load examples.",
      );
    } catch (reason) {
      toast(message(reason), true);
    } finally {
      setSessionBusy(false);
    }
  };
  const loadPreparedExamples = async () => {
    setSessionBusy(true);
    try {
      const data = await loadExamples();
      setBootstrap(data);
      refresh();
      toast(
        "Prepared examples loaded without running Bedrock. Loading again does not duplicate them.",
      );
    } catch (reason) {
      toast(message(reason), true);
    } finally {
      setSessionBusy(false);
    }
  };
  const startCreate = (stress = false) => {
    setReconciliationSeed(null);
    setStressDefault(stress);
    setCreateOpen(true);
  };
  const closeCreate = useCallback(() => {
    setCreateOpen(false);
    setReconciliationSeed(null);
  }, []);
  const drill = (status: string) => {
    const valid = STATUSES.includes(status as (typeof STATUSES)[number])
      ? status
      : "";
    setFilters((current) => ({ ...current, status: valid, cursor: "" }));
    navigate({ page: "cases", status: valid, caseId: "" });
  };
  if (bootError)
    return (
      <main className="boot-page">
        <div className="brand">
          <Icon name="shield" />
          Atropos MedPlain
        </div>
        <State
          kind="error"
          title="Operations API unavailable"
          retry={() => setBootRevision((value) => value + 1)}
        >
          {bootError} No synthetic records or metrics have been substituted.
        </State>
      </main>
    );
  if (!bootstrap)
    return (
      <main className="boot-page">
        <div className="brand">
          <Icon name="shield" />
          Atropos MedPlain
        </div>
        <State kind="loading" title="Opening an anonymous workspace">
          No account required. Preparing a synthetic session.
        </State>
      </main>
    );
  const role = bootstrap.session.role;
  const canCreate = role === "operator" || role === "admin";
  const loadExamplesReason = sessionBusy
    ? "A session request is in progress."
    : !canCreate
      ? "Switch to operator or admin to add prepared examples."
      : error
        ? "Restore the API connection before loading examples."
        : sessionCaseTotal === null
          ? "Reading the full session before loading examples."
          : !bootstrap.collection?.seed_version && sessionCaseTotal > 0
            ? "Examples can only be added to an empty session. This session already has cases in another or the current desk. Use Open populated demo to create a separate prepared session."
            : "";
  const queueCount =
    (overview?.counts.review_required || 0) + (overview?.counts.failed || 0);
  const nav = (
    <>
      <div className="nav-group-label">WORKSPACE</div>
      {PAGES.filter(
        (page) =>
          page !== "settings" || role === "admin" || role === "operator",
      ).map((page) => (
        <a
          key={page}
          href={routeUrl({ ...route, page, caseId: "" })}
          className={`nav-item ${route.page === page ? "active" : ""}`}
          aria-current={route.page === page ? "page" : undefined}
          title={label(page)}
          data-testid={`nav-${page}`}
          onClick={(event) => {
            event.preventDefault();
            navigate({ page, caseId: "" });
          }}
        >
          <Icon name={page} />
          <span className="nav-label">{label(page)}</span>
          {page === "cases" ? (
            <span className="nav-badge">{overview?.counts.total || 0}</span>
          ) : page === "queues" && queueCount ? (
            <span className="nav-badge warning">{queueCount}</span>
          ) : null}
        </a>
      ))}
      <div className="nav-divider" />
      <button
        className="nav-item"
        onClick={() => {
          setTour((value) => !value);
          setMobile(false);
        }}
        title="Safety walkthrough"
      >
        <Icon name="shield" />
        <span className="nav-label">Safety walkthrough</span>
      </button>
      {role === "reviewer" || role === "auditor" ? (
        <p className="rail-hide nav-permission">
          Settings is available to operator and admin demo roles. Use the role
          menu to explore.
        </p>
      ) : null}
    </>
  );
  return (
    <div className={`app-shell ${collapsed ? "rail" : ""}`}>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <aside className="sidebar">
        <a
          className="brand"
          href="/console/"
          onClick={(event) => {
            event.preventDefault();
            navigate({ page: "overview", caseId: "" });
          }}
        >
          <span className="brand-mark">
            <Icon name="shield" />
          </span>
          <span className="brand-copy">
            ATROPOS<strong>MedPlain</strong>
          </span>
        </a>
        <div className="sidebar-caption rail-hide">DISCHARGE OPERATIONS</div>
        <nav aria-label="Main navigation">{nav}</nav>
        <div className="sidebar-bottom">
          <div className="environment rail-hide">
            <span className="live-dot" />
            <strong>Anonymous sandbox</strong>
            <p>Synthetic records only</p>
          </div>
          <button
            className="nav-item collapse-button"
            onClick={() => setCollapsed((value) => !value)}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          >
            <Icon name="menu" />
            <span className="nav-label">Collapse sidebar</span>
          </button>
        </div>
      </aside>
      {mobile ? (
        <div className="mobile-nav">
          <button
            className="mobile-backdrop"
            onClick={() => setMobile(false)}
            aria-label="Close navigation"
          />
          <aside>
            <div className="dialog-header">
              <strong>MedPlain navigation</strong>
              <button
                className="icon-button"
                onClick={() => setMobile(false)}
                aria-label="Close navigation"
              >
                <Icon name="close" />
              </button>
            </div>
            <nav aria-label="Mobile navigation">{nav}</nav>
          </aside>
        </div>
      ) : null}
      <div className="main-shell">
        <header className="topbar">
          <button
            className="icon-button mobile-menu-button"
            aria-label="Open navigation"
            onClick={() => setMobile(true)}
          >
            <Icon name="menu" />
          </button>
          <label className="workspace-select">
            <span className="sr-only">Workspace</span>
            <Icon name="maps" />
            <select
              aria-label="Workspace"
              value={route.workspace}
              onChange={(event) => {
                setFilters((current) => ({ ...current, cursor: "" }));
                navigate({ workspace: event.target.value, caseId: "" });
              }}
            >
              <option value="all">All synthetic desks</option>
              {bootstrap.workspaces
                .filter((item) => item.id !== "all")
                .map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
            </select>
          </label>
          <div className="global-search">
            <Icon name="search" />
            <input
              ref={searchRef}
              value={search}
              maxLength={128}
              onChange={(event) => {
                searchTyping.current = true;
                setSearch(event.target.value);
              }}
              aria-label="Search all session cases"
              placeholder="Search all cases..."
            />
            <kbd>/</kbd>
          </div>
          <div className="user-area">
            <span className="topbar-runtime">
              {bootstrap.runtime.model_mode === "bedrock"
                ? "Amazon Bedrock"
                : "Offline fixture model"}
            </span>
            <button
              className="avatar"
              aria-label="Open user menu"
              aria-expanded={userMenu}
              onClick={() => setUserMenu((value) => !value)}
            >
              {role.slice(0, 2).toUpperCase()}
            </button>
            {userMenu ? (
              <div className="user-popover">
                <strong>Anonymous session</strong>
                <p>Synthetic demo roles, not production identity</p>
                <label>
                  Synthetic demo role
                  <select
                    aria-label="Synthetic demo role"
                    value={role}
                    disabled={sessionBusy}
                    onChange={(event) =>
                      void setRole(event.target.value as Role)
                    }
                  >
                    {bootstrap.session.roles.map((item) => (
                      <option key={item} value={item}>
                        {label(item)}
                      </option>
                    ))}
                  </select>
                </label>
                <p>
                  {sessionBusy
                    ? "Applying role change..."
                    : `Current permissions: ${role}`}
                </p>
                <button
                  className="text-button"
                  onClick={() => {
                    setUserMenu(false);
                    navigate({ page: "settings" });
                  }}
                >
                  Session settings
                </button>
                <button
                  className="text-button"
                  onClick={() => setUserMenu(false)}
                >
                  Close menu
                </button>
              </div>
            ) : null}
          </div>
        </header>
        <div className="demo-strip">
          <span>
            <span className="live-dot" />
            SYNTHETIC DEMO
          </span>
          <span>Source fidelity, not clinical validation</span>
          <span className="demo-role">{label(role)} lens</span>
        </div>
        <main id="main" className="page-content">
          {bootstrap.collection?.mode === "prepared_demo" ||
          (overview?.origin_counts?.prepared_demo || 0) > 0 ? (
            <section
              className="prepared-notice collection-notice"
              data-testid="prepared-demo-notice"
            >
              <Icon name="cases" />
              <div>
                <strong>Prepared examples and new verification runs</strong>
                <p>
                  Prepared examples use offline fixture records, not fresh
                  Bedrock runs. Each case shows its origin.{" "}
                  {bootstrap.runtime.model_mode === "bedrock"
                    ? "New case runs a new Strands verification using Amazon Bedrock."
                    : "New case runs a new Strands verification using the configured offline fixture model."}
                </p>
                {overview?.origin_counts ? (
                  <p className="collection-counts">
                    Current view: {overview.origin_counts.prepared_demo}{" "}
                    prepared examples · {overview.origin_counts.live_run} new
                    runs
                    {overview.origin_counts.unknown
                      ? ` · ${overview.origin_counts.unknown} with unrecorded origin`
                      : ""}
                  </p>
                ) : (
                  <p className="collection-counts">
                    Reading the current collection counts.
                  </p>
                )}
              </div>
            </section>
          ) : bootstrap.collection?.initialized ? (
            <section
              className="empty-collection-note"
              data-testid="empty-session-notice"
            >
              <strong>Fresh session without prepared examples</strong>
              <p>
                Prepared examples will not load automatically. Create a new
                case, or choose Load examples in Settings.
              </p>
            </section>
          ) : null}
          {active && pollCount.current >= 120 ? (
            <div className="notice" role="status">
              Automatic activity polling has paused after five minutes. Jobs
              continue on the server.
              <button
                className="text-button"
                onClick={() => {
                  pollCount.current = 0;
                  refresh();
                }}
              >
                Read latest activity
              </button>
            </div>
          ) : null}
          {error ? (
            <div className="api-banner" role="alert">
              <Icon name="shield" />
              <div>
                <strong>Operations API unavailable</strong>
                <p>
                  {error} Existing snapshots may be stale. No sample fallback
                  has been substituted.
                </p>
              </div>
              <button className="button secondary" onClick={refresh}>
                Retry read
              </button>
            </div>
          ) : null}
          {tour ? (
            <section className="tour card" aria-label="Safety walkthrough">
              <div className="section-heading">
                <div>
                  <div className="eyebrow">INTERACTIVE PRODUCT WALKTHROUGH</div>
                  <h2>Follow a changed dose from refusal to release.</h2>
                </div>
                <button
                  className="icon-button"
                  onClick={() => setTour(false)}
                  aria-label="Close walkthrough"
                >
                  <Icon name="close" />
                </button>
              </div>
              <div className="tour-steps">
                <div>
                  <span>01</span>
                  <strong>Run the stress packet</strong>
                  <p>
                    Start an actual verification with a deliberate dosage
                    mutation.
                  </p>
                  <button
                    className="button secondary"
                    disabled={!canCreate}
                    onClick={() => startCreate(true)}
                  >
                    Run stress sample
                  </button>
                  {!canCreate ? (
                    <p className="control-reason">
                      Switch to operator or admin first.
                    </p>
                  ) : null}
                </div>
                <div>
                  <span>02</span>
                  <strong>Inspect the safety evidence</strong>
                  <p>
                    Open the new case, then Safety and Agents to see refusal and
                    correction.
                  </p>
                  <button
                    className="button secondary"
                    onClick={() => navigate({ page: "cases" })}
                  >
                    Open case register
                  </button>
                </div>
                <div>
                  <span>03</span>
                  <strong>Review and release</strong>
                  <p>
                    Use the reviewer demo role. Release binds the exact source
                    and artifact hashes.
                  </p>
                  <button
                    className="button secondary"
                    disabled={sessionBusy}
                    onClick={() => void setRole("reviewer")}
                  >
                    Switch to reviewer
                  </button>
                  {sessionBusy ? (
                    <p className="control-reason">
                      A role request is in progress.
                    </p>
                  ) : null}
                </div>
              </div>
              <p className="muted">
                This product walkthrough is separate from the official AWS
                workshop qualification.
              </p>
            </section>
          ) : null}
          {route.page === "overview" ? (
            <Overview
              data={overview}
              loading={loading}
              error={error}
              retry={refresh}
              drill={drill}
              create={() => startCreate()}
              canCreate={canCreate}
              drillDesk={selectDesk}
            />
          ) : null}
          {route.page === "cases" ? (
            <>
              <PageHeader
                title="Case register"
                description="Trace synthetic discharge packets from intake to a released patient explanation."
              >
                <button
                  className="button"
                  onClick={() => startCreate()}
                  disabled={!canCreate || loading || Boolean(error)}
                >
                  <Icon name="plus" />
                  New case
                </button>
              </PageHeader>
              {!canCreate || loading || error ? (
                <p className="control-reason">
                  {!canCreate
                    ? "The operator or admin demo role can create cases."
                    : loading
                      ? "Creation becomes available when the current read finishes."
                      : "Restore the API connection before creating a case."}
                </p>
              ) : null}
              <CasesTable
                key={`${route.workspace}:${route.q}:${route.status}`}
                data={list}
                loading={loading}
                error={error}
                retry={refresh}
                facilities={bootstrap.facilities}
                filters={{ ...filters, status: route.status }}
                change={(change) => {
                  setFilters((current) => ({ ...current, ...change }));
                  if (change.status !== undefined)
                    navigate({ status: change.status });
                }}
                open={openCase}
              />
            </>
          ) : null}
          {route.page === "queues" ? (
            <>
              <PageHeader
                title="Review queues"
                description="Work requiring attention remains separate from released patient artifacts."
              >
                <button
                  className="button secondary"
                  onClick={refresh}
                  disabled={loading}
                >
                  Refresh queues
                </button>
              </PageHeader>
              {error ? (
                <State kind="error" title="Queues unavailable" retry={refresh}>
                  {error}
                </State>
              ) : loading && !cohort.length ? (
                <State kind="loading" title="Reading action queues" />
              ) : (
                <div className="queue-grid">
                  {[
                    {
                      title: "Needs review",
                      statuses: ["review_required"],
                      note: "Source-matched artifacts with corrections or interaction flags.",
                      icon: "shield",
                    },
                    {
                      title: "Processing",
                      statuses: ["queued", "processing"],
                      note: "Durable jobs waiting for or running the Strands Graph.",
                      icon: "refresh",
                    },
                    {
                      title: "Needs recovery",
                      statuses: ["failed", "rejected"],
                      note: "Retry is bounded to three generations and never overrides safety.",
                      icon: "queues",
                    },
                  ].map((queue) => {
                    const items = cohort.filter((item) =>
                      queue.statuses.includes(item.status),
                    );
                    return (
                      <section className="card queue-card" key={queue.title}>
                        <div className="section-heading">
                          <h2>
                            <Icon name={queue.icon} />
                            {queue.title}
                          </h2>
                          <span className="subtle-tag">{items.length}</span>
                        </div>
                        <p className="muted">{queue.note}</p>
                        {items.length ? (
                          items.map((item) => (
                            <button
                              className="queue-item"
                              onClick={() => openCase(item.id)}
                              key={item.id}
                            >
                              <strong>{item.packet_title}</strong>
                              <Badge status={item.status} />
                              <OriginBadge origin={item.origin} />
                              <span>
                                {clock(item.updated_at)}
                                <Icon name="arrow" />
                              </span>
                            </button>
                          ))
                        ) : (
                          <State kind="empty" title="Queue clear">
                            No matching cases in this workspace and session.
                          </State>
                        )}
                      </section>
                    );
                  })}
                </div>
              )}
              <p className="scope-line">
                Reviewer/admin can decide. Operator/admin can retry. The API
                enforces these demo permissions.
              </p>
            </>
          ) : null}
          {route.page === "maps" ? (
            <Suspense fallback={<State kind="loading" title="Loading maps" />}>
              <Maps
                facilities={bootstrap.facilities}
                cases={cohort}
                loading={loading}
                error={error}
                retry={refresh}
                selectDesk={selectDesk}
                openCase={openCase}
              />
            </Suspense>
          ) : null}
          {route.page === "settings" ? (
            <>
              <PageHeader
                title="Workspace settings"
                description="Understand this session, its real runtime and the limits of the demonstration."
              />
              <div className="settings-grid">
                <section className="card settings-card">
                  <div className="eyebrow">YOUR WORKSPACE</div>
                  <h2>Anonymous synthetic session</h2>
                  <label>
                    Synthetic demo role
                    <select
                      aria-label="Settings demo role"
                      value={role}
                      disabled={sessionBusy}
                      onChange={(event) =>
                        void setRole(event.target.value as Role)
                      }
                    >
                      {ROLES.map((item) => (
                        <option key={item} value={item}>
                          {label(item)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <p className="control-reason">
                    {sessionBusy
                      ? "A role change is in progress."
                      : "These roles exercise API permissions in your own synthetic session. They are not production authentication."}
                  </p>
                  <label>
                    Theme
                    <select
                      aria-label="Theme"
                      value={theme}
                      onChange={(event) => setTheme(event.target.value)}
                    >
                      <option value="dark">Dark</option>
                      <option value="light">Light</option>
                    </select>
                  </label>
                  <dl>
                    <div>
                      <dt>Case limit</dt>
                      <dd>{bootstrap.limits.max_cases} per session</dd>
                    </div>
                    <div>
                      <dt>Daily job limit</dt>
                      <dd>
                        {bootstrap.limits.daily_jobs ?? "Not configured"} jobs
                        {bootstrap.limits.daily_reset
                          ? ` · resets ${bootstrap.limits.daily_reset}`
                          : ""}
                      </dd>
                    </div>
                    <div>
                      <dt>Retention</dt>
                      <dd>Expires after 24 hours</dd>
                    </div>
                    <div>
                      <dt>Session</dt>
                      <dd className="mono break">
                        {bootstrap.session.session_id}
                      </dd>
                    </div>
                  </dl>
                  <div className="action-row session-actions">
                    <button
                      className="button secondary"
                      data-testid="open-populated-demo"
                      onClick={() => void resetSession(true)}
                      disabled={sessionBusy}
                    >
                      Open populated demo
                    </button>
                    <button
                      className="button secondary"
                      data-testid="start-empty-session"
                      onClick={() => void resetSession(false)}
                      disabled={sessionBusy}
                    >
                      Start empty session
                    </button>
                  </div>
                  <p className="control-reason">
                    {sessionBusy
                      ? "A session request is in progress."
                      : "These actions create a new session with prepared examples or an explicitly empty collection. Previous records expire after 24 hours. The daily job limit stays shared across sessions; it is not an AWS spend cap."}
                  </p>
                  <button
                    className="button secondary"
                    data-testid="load-demo-examples"
                    onClick={() => void loadPreparedExamples()}
                    disabled={Boolean(loadExamplesReason)}
                  >
                    Load examples
                  </button>
                  <p
                    className="control-reason"
                    data-testid="load-demo-examples-reason"
                  >
                    {loadExamplesReason ||
                      "Loads prepared examples into an empty session without running Bedrock. Once loaded, repeating this action does not duplicate them."}
                  </p>
                </section>
                <section className="card settings-card">
                  <div className="eyebrow">LIVE RUNTIME CONFIGURATION</div>
                  <h2>Deployment evidence</h2>
                  <dl>
                    <div>
                      <dt>Inference</dt>
                      <dd>
                        {bootstrap.runtime.model_mode === "bedrock"
                          ? "Amazon Bedrock"
                          : "Offline fixture model"}
                      </dd>
                    </div>
                    <div>
                      <dt>Job transport</dt>
                      <dd>{bootstrap.runtime.transport_mode}</dd>
                    </div>
                    <div>
                      <dt>Storage</dt>
                      <dd>{bootstrap.runtime.storage}</dd>
                    </div>
                    <div>
                      <dt>Commit</dt>
                      <dd className="mono break">{bootstrap.runtime.commit}</dd>
                    </div>
                  </dl>
                  <h3>Bounded demonstration</h3>
                  <p className="muted">
                    Choose a supplied packet or import a new synthetic note in
                    the supported text format. PDF upload and arbitrary FHIR
                    ingestion are not available.
                  </p>
                  <button className="button secondary" disabled>
                    Connect hospital system
                  </button>
                  <p className="control-reason">
                    Hospital integration is not deployed. This demo does not
                    accept external patient records.
                  </p>
                  <a className="text-button" href="/demo">
                    Open the original packet demo
                  </a>
                </section>
                <EvaluationPanel />
                <ReconciliationEvaluationPanel expectedCommit={bootstrap.runtime.commit} />
              </div>
            </>
          ) : null}
          <footer className="page-footer">
            <span>Atropos MedPlain · Synthetic examples only</span>
            <span>
              {bootstrap.runtime.commit?.slice(0, 12)} ·{" "}
              {bootstrap.runtime.model_mode === "bedrock"
                ? "Strands + Bedrock"
                : "Strands + offline fixtures"}
            </span>
          </footer>
        </main>
      </div>
      {createPortal(
        <div className="toast-stack" aria-live="polite" aria-atomic="false">
          {toasts.map((item) => (
            <div
              key={item.id}
              className={`toast ${item.error ? "toast-error" : ""}`}
              role={item.error ? "alert" : "status"}
            >
              <Icon name={item.error ? "shield" : "check"} />
              <span>{item.text}</span>
              <button
                className="icon-button"
                aria-label="Dismiss notification"
                onClick={() =>
                  setToasts((current) =>
                    current.filter((value) => value.id !== item.id),
                  )
                }
              >
                <Icon name="close" />
              </button>
            </div>
          ))}
        </div>,
        document.querySelector("dialog[open]") || document.body,
      )}
      {createOpen ? (
        <NewCase
          bootstrap={bootstrap}
          stressDefault={stressDefault}
          workspace={route.workspace}
          close={closeCreate}
          done={(id) => {
            closeCreate();
            refresh();
            navigate({ page: "cases", caseId: id });
            toast(
              "Case queued. Follow the worker and safety gate in the case drawer.",
            );
          }}
          failed={(text) => toast(text, true)}
          canCreate={canCreate}
          count={sessionCaseTotal || 0}
          reconciliationSeed={reconciliationSeed}
        />
      ) : null}
      {route.caseId ? (
        <CaseDrawer
          key={route.caseId}
          id={route.caseId}
          role={role}
          close={closeCase}
          changed={refresh}
          toast={toast}
          onRevise={(dossier, parentCaseId) => {
            setReconciliationSeed({ dossier, parent_case_id: parentCaseId });
            setStressDefault(false);
            navigate({ caseId: "" });
            setCreateOpen(true);
          }}
        />
      ) : null}
    </div>
  );
}
