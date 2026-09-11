import type { ReactNode } from "react";

/**
 * The house surface. Everything that groups information on this terminal sits
 * in one of these, so the spacing rhythm and the head/hairline/body structure
 * are defined once here and never re-invented per section.
 */
export function Panel({
  idx,
  title,
  note,
  meta,
  flush,
  footer,
  children,
}: {
  /** The panel's fixed index, e.g. "03" — sections are referred to by number. */
  idx?: string;
  title: string;
  /** One short line saying what the panel measures and from what. */
  note?: string;
  meta?: ReactNode;
  /** Drop the body padding — for tables that run edge to edge. */
  flush?: boolean;
  footer?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="panel">
      <header className="panel-head">
        {idx && <span className="panel-idx">§{idx}</span>}
        <h2 className="panel-title">{title}</h2>
        {note && <span className="panel-note">{note}</span>}
        {meta && <div className="panel-meta">{meta}</div>}
      </header>
      <div className={`panel-body${flush ? " flush" : ""}`}>{children}</div>
      {footer && <div className="panel-foot">{footer}</div>}
    </section>
  );
}
