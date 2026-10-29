import { useEffect, useRef, type ReactNode } from "react";
import { label, type Status } from "./types";

const paths: Record<string, string> = {
  overview: "M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z",
  cases: "M8 3h8l4 4v14H4V3h4m6 0v6h6M8 13h8M8 17h5",
  queues: "M4 5h16M4 12h16M4 19h16M7 3v4M17 10v4M10 17v4",
  maps: "m3 6 6-3 6 3 6-3v15l-6 3-6-3-6 3V6Zm6-3v15m6-12v15",
  settings:
    "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8m0-5v3m0 12v3M3 12h3m12 0h3M5.6 5.6l2.1 2.1m8.6 8.6 2.1 2.1M5.6 18.4l2.1-2.1m8.6-8.6 2.1-2.1",
  search: "m21 21-5-5M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14",
  close: "m6 6 12 12M6 18 18 6",
  menu: "M4 6h16M4 12h16M4 18h16",
  arrow: "m9 5 7 7-7 7",
  plus: "M12 4v16M4 12h16",
  shield: "m12 2 9 4v6c0 5-9 10-9 10S3 17 3 12V6l9-4Zm-4 10 3 3 5-6",
  refresh:
    "M20 7v5h-5M4 17v-5h5m-4-4a8 8 0 0 1 13-3l2 2M4 17l2 2a8 8 0 0 0 13-3",
  sun: "M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1 1m12 12 1 1M5 19l1-1M18 6l1-1M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10",
  check: "m5 12 4 4L19 6",
  download: "M12 3v12m-5-5 5 5 5-5M4 17v4h16v-4",
};
export function Icon({
  name,
  className = "",
}: {
  name: string;
  className?: string;
}) {
  return (
    <svg
      className={`icon ${className}`}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name] || paths.shield} />
    </svg>
  );
}
export function Badge({ status }: { status: Status | string }) {
  return <span className={`badge status-${status}`}>{label(status)}</span>;
}
export function State({
  kind,
  title,
  children,
  retry,
}: {
  kind: "empty" | "loading" | "error";
  title: string;
  children?: ReactNode;
  retry?: () => void;
}) {
  return (
    <div
      className={`state state-${kind}`}
      role={kind === "error" ? "alert" : "status"}
    >
      <span className={`state-icon ${kind === "loading" ? "pulse" : ""}`}>
        <Icon
          name={
            kind === "error"
              ? "shield"
              : kind === "loading"
                ? "refresh"
                : "cases"
          }
        />
      </span>
      <h3>{title}</h3>
      {children ? <p>{children}</p> : null}
      {retry ? (
        <button className="button secondary" onClick={retry}>
          <Icon name="refresh" />
          Retry read
        </button>
      ) : null}
    </div>
  );
}
export function Dialog({
  title,
  children,
  onClose,
  wide = false,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
}) {
  const root = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = root.current;
    const previous = document.activeElement as HTMLElement | null;
    if (!element) return;
    element.showModal();
    const cancel = (event: Event) => {
      event.preventDefault();
      onClose();
    };
    element.addEventListener("cancel", cancel);
    const oldOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      element.removeEventListener("cancel", cancel);
      element.close();
      document.body.style.overflow = oldOverflow;
      previous?.focus();
    };
  }, [onClose]);
  return (
    <dialog
      ref={root}
      className={wide ? "dialog drawer" : "dialog"}
      aria-labelledby="dialog-title"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="dialog-inner">
        <header className="dialog-header">
          <h2 id="dialog-title">{title}</h2>
          <button
            autoFocus
            className="icon-button"
            aria-label="Close dialog"
            onClick={onClose}
          >
            <Icon name="close" />
          </button>
        </header>
        {children}
      </div>
    </dialog>
  );
}
export function PageHeader({
  eyebrow,
  title,
  description,
  children,
}: {
  eyebrow?: string;
  title: string;
  description: string;
  children?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        <div className="breadcrumb">
          Operations <span>/</span> {eyebrow || title}
        </div>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {children ? <div className="page-actions">{children}</div> : null}
    </header>
  );
}
export function Hash({
  title,
  value,
}: {
  title: string;
  value?: string | null;
}) {
  return (
    <div className="hash">
      <span>{title}</span>
      <code>{value || "Not recorded"}</code>
    </div>
  );
}
export function Download({
  value,
  name,
  children,
}: {
  value: unknown;
  name: string;
  children: ReactNode;
}) {
  const download = () => {
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }),
    );
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = name;
    anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return (
    <button className="button secondary" onClick={download}>
      <Icon name="download" />
      {children}
    </button>
  );
}
