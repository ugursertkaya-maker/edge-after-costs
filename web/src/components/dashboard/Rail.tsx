"use client";

import { formatMoney, formatRelativeMs } from "@/lib/format";
import type { DashboardPayload, LedgerRef } from "@/lib/types";

/**
 * The header rail. Its whole job is to make four facts impossible to miss
 * before the operator reads a single number below it: this is paper, no real
 * order can leave the process, the exchange feed is (or is not) live, and
 * which bankroll the figures belong to.
 */
export function Rail({
  data,
  ledger,
  onLedger,
  lastOkMs,
  stale,
  apiOk,
  now,
}: {
  data: DashboardPayload | null;
  ledger: string;
  onLedger: (name: string) => void;
  lastOkMs: number | null;
  /** Several consecutive failed polls — the figures below are not current. */
  stale: boolean;
  /** `GET /api/health`: null until the first probe answers. */
  apiOk: boolean | null;
  now: number;
}) {
  const connected = data?.header.data_connected ?? false;
  const liveOrders = data?.live_orders_enabled ?? false;
  const startingEquity = data?.header.starting_equity ?? null;

  // Until the first payload lands we still offer the three ledgers the brief
  // fixes, so the switcher does not pop into existence a beat after the rail.
  const ledgers: LedgerRef[] =
    data?.ledgers && data.ledgers.length > 0
      ? data.ledgers
      : [
          { name: "base", label: "$500", starting_equity: 500, equity: null },
          { name: "mid", label: "$1,000", starting_equity: 1000, equity: null },
          { name: "large", label: "$5,000", starting_equity: 5000, equity: null },
        ];

  return (
    <header className="rail">
      <div className="wordmark">
        <span className="wordmark-name">NETEDGE</span>
        <span className="wordmark-ver">
          {data?.simulator_version ?? "sim-?"} · {data?.exchange ?? "—"}
        </span>
      </div>

      <span className="rail-sep" aria-hidden />

      <span className="pill is-pos" title="Every order is simulated.">
        <span className="dot is-pulse" aria-hidden />
        PAPER
      </span>

      {/* This one is deliberately red-on-affirmative: a live-order flag that
          ever flips to ON is the single worst thing that can happen to a
          paper-only system, so it must not look like an ordinary metric. */}
      <div className="rstat">
        <span className="rstat-k">Live orders</span>
        <span className={`rstat-v ${liveOrders ? "is-neg" : "is-off"}`}>
          {liveOrders ? "ON" : "OFF"}
        </span>
      </div>

      <div className="rstat">
        <span className="rstat-k">Exchange data</span>
        <span className={`rstat-v ${connected ? "is-pos" : "is-neg"}`}>
          {connected ? "LIVE" : "DISCONNECTED"}
        </span>
      </div>

      {/* Separate from the exchange reading on purpose: an API that answers
          while the exchange feed is dead is a different fault from an API that
          does not answer at all. */}
      <div className="rstat">
        <span className="rstat-k">API</span>
        <span
          className={`rstat-v ${apiOk === null ? "is-off" : apiOk ? "is-pos" : "is-neg"}`}
        >
          {apiOk === null ? "—" : apiOk ? "UP" : "NO RESPONSE"}
        </span>
      </div>

      <div className="rstat">
        <span className="rstat-k">Starting equity</span>
        <span className="rstat-v">{formatMoney(startingEquity, 0)}</span>
      </div>

      <span className="rail-spacer" />

      {stale && (
        <span className="pill is-warn" title="The most recent polls failed.">
          STALE DATA
        </span>
      )}

      <div className="rstat">
        <span className="rstat-k">Last poll</span>
        <span className="rstat-v is-off">{formatRelativeMs(lastOkMs, now)}</span>
      </div>

      <div className="switch" role="group" aria-label="Ledger selection">
        {ledgers.map((l) => (
          <button
            key={l.name}
            type="button"
            className={`switch-btn${l.name === ledger ? " is-on" : ""}`}
            onClick={() => onLedger(l.name)}
            aria-pressed={l.name === ledger}
          >
            {l.label}
          </button>
        ))}
      </div>
    </header>
  );
}
