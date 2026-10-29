import { useMemo, useState } from "react";
import {
  useReactTable,
  getCoreRowModel,
  flexRender,
  type ColumnDef,
  type SortingState,
} from "@tanstack/react-table";
import {
  clock,
  duration,
  label,
  STATUSES,
  type CaseList,
  type CaseSummary,
  type Facility,
} from "./types";
import { Badge, Icon, OriginBadge, State } from "./ui";
export interface Filters {
  status: string;
  sort: string;
  direction: string;
  limit: number;
  cursor: string;
}
export default function CasesTable({
  data,
  loading,
  error,
  retry,
  filters,
  change,
  open,
  facilities,
}: {
  data: CaseList | null;
  loading: boolean;
  error: string;
  retry: () => void;
  filters: Filters;
  change: (value: Partial<Filters>) => void;
  open: (id: string) => void;
  facilities: Facility[];
}) {
  const [history, setHistory] = useState<string[]>([]);
  const columns = useMemo<ColumnDef<CaseSummary>[]>(
    () => [
      {
        accessorKey: "packet_title",
        header: "Case / packet",
        cell: (info) => (
          <div className="case-title">
            <strong>{info.row.original.packet_title}</strong>
            <span>
              {info.row.original.id.slice(0, 12)} · generation{" "}
              {info.row.original.generation}
            </span>
            <OriginBadge origin={info.row.original.origin} />
          </div>
        ),
      },
      {
        accessorKey: "status",
        header: "Status",
        cell: (info) => <Badge status={info.row.original.status} />,
      },
      {
        accessorKey: "facility_id",
        header: "Synthetic desk",
        cell: (info) => (
          <span>
            {facilities.find((f) => f.id === info.row.original.facility_id)
              ?.city || info.row.original.facility_id}
          </span>
        ),
      },
      {
        accessorKey: "created_at",
        header: "Created",
        cell: (info) => (
          <span className="nowrap">{clock(info.row.original.created_at)}</span>
        ),
      },
      {
        accessorKey: "duration_ms",
        header: "Duration",
        cell: (info) => duration(info.row.original.duration_ms),
      },
      {
        accessorKey: "correction_count",
        header: "Corrections",
        cell: (info) => (
          <span
            className={
              info.row.original.correction_count ? "warning-text" : "muted"
            }
          >
            {info.row.original.correction_count}
          </span>
        ),
      },
    ],
    [facilities],
  );
  const sorting: SortingState = [
    { id: filters.sort, desc: filters.direction === "desc" },
  ];
  const table = useReactTable({
    data: data?.items || [],
    columns,
    getCoreRowModel: getCoreRowModel(),
    manualSorting: true,
    manualPagination: true,
    state: { sorting },
    onSortingChange: (update) => {
      const next = typeof update === "function" ? update(sorting) : update;
      if (next[0]) {
        setHistory([]);
        change({
          sort: next[0].id,
          direction: next[0].desc ? "desc" : "asc",
          cursor: "",
        });
      }
    },
    enableSortingRemoval: false,
  });
  const reset = (value: Partial<Filters>) => {
    setHistory([]);
    change({ ...value, cursor: "" });
  };
  return (
    <section className="card table-card">
      <div className="table-toolbar">
        <div>
          <h2>
            Case register{" "}
            <span className="subtle-tag">{data?.total ?? "..."}</span>
          </h2>
          <p>
            Search covers this session. Prepared examples and new runs carry
            separate origin labels. Created means added to the session.
          </p>
        </div>
        <label className="compact-field">
          Status
          <select
            value={filters.status}
            onChange={(event) => reset({ status: event.target.value })}
          >
            <option value="">All statuses</option>
            {STATUSES.map((status) => (
              <option key={status} value={status}>
                {label(status)}
              </option>
            ))}
          </select>
        </label>
      </div>
      {error ? (
        <State kind="error" title="Case register unavailable" retry={retry}>
          {error}
        </State>
      ) : loading && !data ? (
        <State kind="loading" title="Loading cases" />
      ) : !data?.items.length ? (
        <State kind="empty" title="No matching cases">
          Create a case, or change the workspace, search and status filters.
        </State>
      ) : (
        <>
          <div className="mobile-cases">
            {data.items.map((row) => (
              <button
                className="mobile-case"
                key={row.id}
                onClick={() => open(row.id)}
              >
                <div>
                  <strong>{row.packet_title}</strong>
                  <Icon name="arrow" />
                </div>
                <Badge status={row.status} />
                <OriginBadge origin={row.origin} />
                <span className="muted">
                  {clock(row.created_at)} · {duration(row.duration_ms)}
                </span>
              </button>
            ))}
          </div>
          <div className="table-scroll">
            <table>
              <thead>
                {table.getHeaderGroups().map((group) => (
                  <tr key={group.id}>
                    {group.headers.map((header) => (
                      <th
                        key={header.id}
                        aria-sort={
                          header.column.getIsSorted()
                            ? header.column.getIsSorted() === "desc"
                              ? "descending"
                              : "ascending"
                            : "none"
                        }
                      >
                        <button
                          onClick={header.column.getToggleSortingHandler()}
                        >
                          {flexRender(
                            header.column.columnDef.header,
                            header.getContext(),
                          )}
                          <span aria-hidden="true">
                            {header.column.getIsSorted() === "asc"
                              ? " ↑"
                              : header.column.getIsSorted() === "desc"
                                ? " ↓"
                                : " ↕"}
                          </span>
                        </button>
                      </th>
                    ))}
                  </tr>
                ))}
              </thead>
              <tbody>
                {table.getRowModel().rows.map((row) => (
                  <tr
                    key={row.id}
                    tabIndex={0}
                    onClick={() => open(row.original.id)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        open(row.original.id);
                      }
                    }}
                    aria-label={`Open ${row.original.packet_title}`}
                  >
                    {row.getVisibleCells().map((cell) => (
                      <td key={cell.id}>
                        {flexRender(
                          cell.column.columnDef.cell,
                          cell.getContext(),
                        )}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      <footer className="table-footer">
        <div>
          {loading ? (
            <span role="status">Refreshing...</span>
          ) : (
            <span>{data?.total ?? 0} matching cases</span>
          )}
          <span className="muted">
            {data ? `As of ${clock(data.as_of)}` : "Snapshot unavailable"}
          </span>
        </div>
        <label className="compact-field">
          Rows
          <select
            value={filters.limit}
            onChange={(event) => reset({ limit: Number(event.target.value) })}
          >
            {[5, 10, 20].map((size) => (
              <option key={size}>{size}</option>
            ))}
          </select>
        </label>
        <div className="pagination">
          <button
            className="icon-button"
            aria-label="Previous page"
            disabled={!history.length || loading}
            onClick={() => {
              const previous = history[history.length - 1];
              setHistory(history.slice(0, -1));
              change({ cursor: previous });
            }}
          >
            ←
          </button>
          <span>Page {history.length + 1}</span>
          <button
            className="icon-button"
            aria-label="Next page"
            disabled={!data?.next_cursor || loading}
            onClick={() => {
              setHistory([...history, filters.cursor]);
              change({ cursor: data?.next_cursor || "" });
            }}
          >
            →
          </button>
        </div>
        <span className="pagination-reason">
          {loading
            ? "Page controls pause while reading."
            : !history.length && !data?.next_cursor
              ? "All matching records fit on this page."
              : !data?.next_cursor
                ? "This is the last page."
                : !history.length
                  ? "You are on the first page."
                  : ""}
        </span>
      </footer>
    </section>
  );
}
