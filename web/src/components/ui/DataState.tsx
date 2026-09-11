import type { ReactNode } from "react";

/**
 * Empty is a legitimate reading here, not a failure: a scalper that has
 * refused every candidate for an hour is doing its job. So the empty state is
 * a composed screen with its own voice, never a blank panel that reads as a
 * bug.
 */
export function DataState({
  title,
  text,
  scanning,
}: {
  title: string;
  text?: string;
  /** Marks the state as live work in progress rather than a dead end. */
  scanning?: boolean;
}) {
  return (
    <div className="state">
      <div className={`state-mark${scanning ? " is-scanning" : ""}`} aria-hidden />
      <p className="state-title">{title}</p>
      {text && <p className="state-text">{text}</p>}
    </div>
  );
}

/** Placeholder rows with the same rhythm as the table they stand in for. */
export function Skeleton({ rows = 6 }: { rows?: number }) {
  const widths = ["86%", "72%", "94%", "64%", "81%", "77%", "58%", "89%"];
  return (
    <div className="skel-rows" aria-hidden>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skel" style={{ width: widths[i % widths.length] }} />
      ))}
    </div>
  );
}

/**
 * The compact warning strip. It never replaces the screen — the terminal keeps
 * its full structure and this says, in one line, what is missing.
 */
export function Banner({
  tone = "warn",
  tag,
  detail,
  children,
}: {
  tone?: "warn" | "err";
  tag: string;
  detail?: string;
  children: ReactNode;
}) {
  return (
    <div className={`banner banner-${tone}`} role="status">
      <span className="banner-tag">{tag}</span>
      <span>{children}</span>
      {detail && <span className="banner-detail">{detail}</span>}
    </div>
  );
}
