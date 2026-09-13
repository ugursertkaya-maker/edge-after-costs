"use client";

import { Component, type ErrorInfo, type ReactNode } from "react";

/**
 * React never commits a partially-rendered tree: without a boundary, one bad
 * field in one panel takes the entire terminal with it and leaves a blank
 * screen that names nothing. That is exactly the shape of failure this screen
 * cannot afford — the operator is reading it to decide whether the account is
 * safe, and "nothing" reads the same as "flat".
 *
 * So every panel gets its own boundary. A throw now costs one section, and
 * that section says which one it was and what the error said, which is the
 * difference between a bug report and a guess.
 */
export class PanelBoundary extends Component<
  { name: string; children: ReactNode },
  { message: string | null }
> {
  state: { message: string | null } = { message: null };

  static getDerivedStateFromError(error: unknown) {
    return { message: error instanceof Error ? error.message : String(error) };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Kept on the console verbatim: the operator is often on a browser we
    // cannot attach a debugger to, and this string is what they can send us.
    console.error(`[panel:${this.props.name}]`, error, info.componentStack);
  }

  render() {
    if (this.state.message === null) return this.props.children;
    return (
      <section className="panel">
        <header className="panel-head">
          <span className="panel-idx">!</span>
          <h2 className="panel-title">{this.props.name}</h2>
          <span className="panel-note">panel failed to render — the rest of the screen is still valid</span>
        </header>
        <div className="panel-body">
          <p className="state-title">This section raised an error.</p>
          <p className="state-text" style={{ fontFamily: "var(--font-mono)" }}>
            {this.state.message}
          </p>
        </div>
      </section>
    );
  }
}
