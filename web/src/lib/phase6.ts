/**
 * PHASE 6 — the arithmetic behind the multi-asset panels.
 *
 * Everything in this module is a pure function over the payload, kept apart
 * from the components for two reasons. The first is that these are the
 * statements the screen is making — "BTC is 41.7% of 139 trades", "$12.40 of
 * idle cash is unattributed" — and a statement that decides whether a warning
 * fires deserves a test. The second is that the worker has not been extended
 * yet: every panel here must read correctly against a payload carrying none of
 * the Phase 6 keys, and the *derivation from what already arrives* is the
 * difference between an honest fallback and an empty screen.
 *
 * Two rules are enforced here rather than per panel:
 *
 *   * A derived figure carries its own denominator. `deriveConcentration`
 *     returns the sample it was computed over, because 41.7% of 139 trades and
 *     41.7% of 3 trades are not the same reading and a panel that prints only
 *     the percentage cannot tell them apart.
 *
 *   * An absent cell is not a zero cell. The bucket grid returns `undefined`
 *     for a strategy × bucket pair that has no trades, never a zeroed record:
 *     "this strategy never held for 2–5 minutes" and "it held for 2–5 minutes
 *     and made exactly nothing" are different findings.
 *
 * This module deliberately imports no other runtime module — only types, which
 * erase. That keeps it runnable under `node --test` without a bundler, which
 * is why `num` below duplicates `toNum` from `format.ts` instead of importing
 * it. The duplication is four lines and it buys a test suite.
 */

import type {
  ConcentrationRow,
  ConcentrationState,
  IdleByReason,
  OpportunityRow,
  ScannerRow,
  TradeRow,
} from "./types";

// --------------------------------------------------------------------------
// numeric plumbing
// --------------------------------------------------------------------------

/** Parse an API numeric. `null` for anything absent or unparseable. */
export function num(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

/** BTCUSD → BTC. Duplicated from `format.baseAsset` for the same reason as
    `num`; both are one line and neither is worth a bundler dependency. */
export function base(symbol: string | null | undefined): string {
  if (!symbol) return "";
  return symbol.replace(/(USDT|USDC|USD)$/i, "") || symbol;
}

// ==========================================================================
// §20 — holding buckets
// ==========================================================================

export interface HoldingBucketDef {
  key: string;
  label: string;
  /** Inclusive lower bound, seconds. */
  lo: number;
  /** Exclusive upper bound, seconds. `Infinity` on the last bucket. */
  hi: number;
}

/**
 * The eight buckets, frozen by the brief.
 *
 * The boundaries are not decoration: `flow_impulse` has a ~32 s median hold
 * and `momentum_scalp` a ~71 s one, so the 30–60 s and 1–2 m edges are exactly
 * where those two strategies separate. Pooling them into one row would average
 * a strategy that is working with one that is not.
 */
export const HOLDING_BUCKETS: readonly HoldingBucketDef[] = [
  { key: "lt_30s", label: "<30 sn", lo: 0, hi: 30 },
  { key: "30_60s", label: "30–60 sn", lo: 30, hi: 60 },
  { key: "1_2m", label: "1–2 dk", lo: 60, hi: 120 },
  { key: "2_5m", label: "2–5 dk", lo: 120, hi: 300 },
  { key: "5_10m", label: "5–10 dk", lo: 300, hi: 600 },
  { key: "10_25m", label: "10–25 dk", lo: 600, hi: 1500 },
  { key: "25_60m", label: "25–60 dk", lo: 1500, hi: 3600 },
  { key: "gt_60m", label: ">60 dk", lo: 3600, hi: Infinity },
];

/**
 * Spellings the worker might plausibly send for each bucket.
 *
 * The backend block does not exist yet, so the key it will use is a guess.
 * Rather than guess once and render nothing when the guess is wrong, every
 * obvious spelling maps onto the frozen key — and an unknown key is kept and
 * shown as its own column rather than silently dropped.
 */
const BUCKET_ALIASES: Record<string, string> = {
  lt_30s: "lt_30s", "<30s": "lt_30s", u30s: "lt_30s", "0_30s": "lt_30s", s0_30: "lt_30s",
  "30_60s": "30_60s", "30-60s": "30_60s", s30_60: "30_60s",
  "1_2m": "1_2m", "1-2m": "1_2m", m1_2: "1_2m", "60_120s": "1_2m",
  "2_5m": "2_5m", "2-5m": "2_5m", m2_5: "2_5m",
  "5_10m": "5_10m", "5-10m": "5_10m", m5_10: "5_10m",
  "10_25m": "10_25m", "10-25m": "10_25m", m10_25: "10_25m",
  "25_60m": "25_60m", "25-60m": "25_60m", m25_60: "25_60m",
  gt_60m: "gt_60m", ">60m": "gt_60m", o60m: "gt_60m", "60m_plus": "gt_60m",
};

/** Map a worker-supplied bucket name onto a frozen key, or keep it as-is. */
export function normalizeBucketKey(raw: string | null | undefined): string | null {
  if (!raw) return null;
  const k = String(raw).trim().toLowerCase().replace(/\s+/g, "");
  return BUCKET_ALIASES[k] ?? k;
}

/** Which bucket a hold of `seconds` falls in. Negative durations are not a
    bucket — they are a broken timestamp, and they are reported as null. */
export function bucketFor(seconds: number | null): HoldingBucketDef | null {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return null;
  for (const b of HOLDING_BUCKETS) {
    if (seconds >= b.lo && seconds < b.hi) return b;
  }
  return null;
}

/** A trade's hold, preferring the reported figure over the timestamp span. */
export function holdingSecondsOf(t: TradeRow): number | null {
  const reported = num((t as { holding_seconds?: unknown }).holding_seconds);
  if (reported !== null) return reported;
  const a = num(t.entry_ms);
  const b = num(t.exit_ms);
  if (a === null || b === null || b < a) return null;
  return (b - a) / 1000;
}

export interface BucketCell {
  trades: number;
  net: number;
  gross: number | null;
  cost: number | null;
  wins: number;
  /** Trades whose net P&L was not reported — counted, never guessed at. */
  unmeasured: number;
}

export interface BucketGrid {
  /** One row per strategy. Never pooled: that is the whole point of §20. */
  strategies: string[];
  /** `strategy -> bucketKey -> cell`. A missing key means NO TRADES. */
  cells: Record<string, Record<string, BucketCell>>;
  /** Per-strategy totals, for the row-level denominator. */
  totals: Record<string, BucketCell>;
  /** Closed trades the grid was built from — the denominator for everything. */
  sample: number;
  /** Trades dropped because their hold could not be established. */
  undated: number;
}

function emptyCell(): BucketCell {
  return { trades: 0, net: 0, gross: null, cost: null, wins: 0, unmeasured: 0 };
}

function addTrade(cell: BucketCell, net: number | null, gross: number | null, cost: number | null) {
  cell.trades += 1;
  if (net === null) {
    cell.unmeasured += 1;
  } else {
    cell.net += net;
    if (net > 0) cell.wins += 1;
  }
  if (gross !== null) cell.gross = (cell.gross ?? 0) + gross;
  if (cost !== null) cell.cost = (cell.cost ?? 0) + cost;
}

/**
 * Build the strategy × bucket grid from the closed-trade list.
 *
 * The trades array the API serves is the last 200 round trips, so this is a
 * bounded sample and the caller is expected to print `sample` next to it. That
 * is not a caveat to bury: a bucket holding four trades is not a performance
 * reading, and the panel says how many it has.
 */
export function deriveHoldingBuckets(trades: TradeRow[] | null | undefined): BucketGrid {
  const grid: BucketGrid = {
    strategies: [],
    cells: {},
    totals: {},
    sample: 0,
    undated: 0,
  };
  if (!trades || trades.length === 0) return grid;

  for (const t of trades) {
    const secs = holdingSecondsOf(t);
    const bucket = bucketFor(secs);
    const strategy = t.strategy ?? "—";
    if (bucket === null) {
      grid.undated += 1;
      continue;
    }
    grid.sample += 1;
    const net = num(t.net_pnl_usd);
    const gross = num(t.gross_pnl_usd);
    const cost = num(t.total_cost_usd ?? t.fees_usd);

    const row = (grid.cells[strategy] ??= {});
    const cell = (row[bucket.key] ??= emptyCell());
    addTrade(cell, net, gross, cost);

    const total = (grid.totals[strategy] ??= emptyCell());
    addTrade(total, net, gross, cost);
  }

  grid.strategies = Object.keys(grid.cells).sort((a, b) => {
    const at = grid.totals[a]?.trades ?? 0;
    const bt = grid.totals[b]?.trades ?? 0;
    return bt - at || a.localeCompare(b);
  });
  return grid;
}

/** The median hold per strategy — the figure that proves the rows are not
    interchangeable (≈32 s against ≈71 s is two different instruments). */
export function medianHoldByStrategy(
  trades: TradeRow[] | null | undefined,
): Record<string, number> {
  const by: Record<string, number[]> = {};
  for (const t of trades ?? []) {
    const s = holdingSecondsOf(t);
    if (s === null) continue;
    (by[t.strategy ?? "—"] ??= []).push(s);
  }
  const out: Record<string, number> = {};
  for (const [k, xs] of Object.entries(by)) {
    xs.sort((a, b) => a - b);
    const mid = Math.floor(xs.length / 2);
    out[k] = xs.length % 2 ? xs[mid] : (xs[mid - 1] + xs[mid]) / 2;
  }
  return out;
}

// ==========================================================================
// §21 — concentration
// ==========================================================================

/**
 * The flag's threshold and the sample it needs before it may speak.
 *
 * 70% is the brief's number. The 30-trade floor is this screen's own
 * condition and it is the more important half: a 100% BTC share over two
 * trades is not concentration, it is two trades, and a flag that fires on it
 * teaches the operator to ignore the flag.
 */
export const CONCENTRATION_THRESHOLD_PCT = 70;
export const CONCENTRATION_MIN_SAMPLE = 30;

export type ConcentrationVerdict = "flagged" | "clear" | "thin" | "unmeasured";

export interface ConcentrationReading {
  verdict: ConcentrationVerdict;
  /** Trade share of the leading symbol, in points (41.7 === 41.7%). */
  sharePct: number | null;
  /** The symbol the share belongs to. */
  symbol: string | null;
  /** Trades the share was computed over. Null means no denominator at all. */
  sample: number | null;
}

/**
 * Read the concentration flag.
 *
 * Four outcomes, and three of them are not "fine". `thin` says the sample is
 * too small to judge, which is NOT the same as clear; `unmeasured` says
 * nothing was counted, which is not the same as thin. The flag is diagnostic
 * only — nothing in this codebase reads it, and nothing about a symbol's
 * ranking changes because it fired.
 */
export function concentrationReading(
  sharePct: number | null,
  sample: number | null,
): ConcentrationReading & { symbol: string | null } {
  const shape = { sharePct, symbol: null as string | null, sample };
  if (sharePct === null || sample === null) {
    return { ...shape, verdict: "unmeasured" };
  }
  if (sample < CONCENTRATION_MIN_SAMPLE) return { ...shape, verdict: "thin" };
  if (sharePct > CONCENTRATION_THRESHOLD_PCT) return { ...shape, verdict: "flagged" };
  return { ...shape, verdict: "clear" };
}

export interface DerivedConcentration {
  rows: ConcentrationRow[];
  /** Closed trades counted. The denominator behind every share below. */
  tradeSample: number;
  /** Summed traded notional, USD. The denominator behind the capital shares. */
  notionalTotal: number | null;
  /** Trades whose notional was not reported — the capital share's blind spot. */
  notionalMissing: number;
}

/**
 * Per-coin concentration from the closed-trade list.
 *
 * "Capital allocated" here means CUMULATIVE TRADED NOTIONAL — the sum of every
 * round trip's size — not capital currently deployed. A $50 position recycled
 * ten times contributes $500. That is the correct denominator for "which coin
 * does this account's capital actually flow through", and it is a different
 * number from `portfolio.capital_deployed`; the panel labels which one it is.
 */
export function deriveConcentration(
  trades: TradeRow[] | null | undefined,
): DerivedConcentration {
  const acc: Record<string, { trades: number; net: number; netKnown: number; notional: number; notionalKnown: number }> = {};
  let notionalMissing = 0;

  for (const t of trades ?? []) {
    const sym = t.symbol;
    if (!sym) continue;
    const a = (acc[sym] ??= { trades: 0, net: 0, netKnown: 0, notional: 0, notionalKnown: 0 });
    a.trades += 1;
    const net = num(t.net_pnl_usd);
    if (net !== null) {
      a.net += net;
      a.netKnown += 1;
    }
    const notional = num(t.notional_usd);
    if (notional !== null) {
      a.notional += notional;
      a.notionalKnown += 1;
    } else {
      notionalMissing += 1;
    }
  }

  const tradeSample = Object.values(acc).reduce((s, a) => s + a.trades, 0);
  const notionalKnownTotal = Object.values(acc).reduce((s, a) => s + a.notional, 0);
  const anyNotional = Object.values(acc).some((a) => a.notionalKnown > 0);

  const rows: ConcentrationRow[] = Object.entries(acc)
    .map(([symbol, a]) => ({
      symbol,
      trades: a.trades,
      net_pnl_usd: a.netKnown > 0 ? a.net : null,
      capital_allocated_usd: a.notionalKnown > 0 ? a.notional : null,
      trade_share_pct: tradeSample > 0 ? (a.trades / tradeSample) * 100 : null,
      capital_share_pct:
        anyNotional && notionalKnownTotal > 0 ? (a.notional / notionalKnownTotal) * 100 : null,
      // Nothing in the closed-trade list says how many signals a symbol
      // produced or how many were refused. Those come from the funnel, and
      // until it is served they stay absent rather than being inferred.
      signals: null,
      qualified_signals: null,
      rejected_signals: null,
    }))
    .sort((a, b) => (b.trades ?? 0) - (a.trades ?? 0));

  return {
    rows,
    tradeSample,
    notionalTotal: anyNotional ? notionalKnownTotal : null,
    notionalMissing,
  };
}

/**
 * Fold the funnel report into the per-coin rows.
 *
 * The funnel is the only source for "how many signals did this coin produce
 * and how many were refused", and it is the only source for WHY a coin never
 * trades — which is the cause line the flag has to print. Stage names are
 * frozen in `app/research/funnel.py`.
 */
export function applyFunnel(
  rows: ConcentrationRow[],
  funnel: Record<string, { stages?: Record<string, number | null | undefined> | null; bottleneck_stage?: string | null; bottleneck_reason?: string | null }> | null | undefined,
): ConcentrationRow[] {
  if (!funnel) return rows;
  const seen = new Set(rows.map((r) => r.symbol));
  const merged = rows.map((r) => {
    const f = funnel[r.symbol];
    if (!f) return r;
    return { ...r, ...funnelCounts(f) };
  });
  // A symbol that never traded is exactly the symbol this panel exists to
  // show. It has no trade row, so it is appended from the funnel alone.
  for (const [symbol, f] of Object.entries(funnel)) {
    if (seen.has(symbol)) continue;
    merged.push({
      symbol,
      trades: num(f.stages?.traded) ?? 0,
      net_pnl_usd: null,
      capital_allocated_usd: null,
      trade_share_pct: null,
      capital_share_pct: null,
      ...funnelCounts(f),
    });
  }
  return merged;
}

function funnelCounts(f: {
  stages?: Record<string, number | null | undefined> | null;
  bottleneck_stage?: string | null;
  bottleneck_reason?: string | null;
}): Partial<ConcentrationRow> {
  const st = f.stages ?? {};
  const evaluated = num(st.strategy_evaluated);
  const ranked = num(st.ranked);
  const candidate = num(st.candidate);
  return {
    signals: evaluated ?? candidate,
    qualified_signals: ranked,
    rejected_signals:
      evaluated !== null && ranked !== null ? Math.max(0, evaluated - ranked) : null,
    bottleneck_stage: f.bottleneck_stage ?? null,
    bottleneck_reason: f.bottleneck_reason ?? null,
  };
}

/** The per-coin rows, from the worker's own block when it serves one and from
    the closed trades when it does not. The caller is told which it got. */
export function concentrationRows(
  served: ConcentrationState | null | undefined,
  trades: TradeRow[] | null | undefined,
): { rows: ConcentrationRow[]; derived: boolean; sample: number | null } {
  const servedRows = served?.rows ?? null;
  if (servedRows && servedRows.length > 0) {
    return { rows: servedRows, derived: false, sample: num(served?.sample_trades) };
  }
  const d = deriveConcentration(trades);
  return { rows: d.rows, derived: true, sample: d.tradeSample };
}

// ==========================================================================
// §22 — idle reconciliation
// ==========================================================================

export interface IdleReconciliation {
  /** What the reasons add up to. */
  sum: number;
  /** What the portfolio says is idle. Null when it did not say. */
  reported: number | null;
  /** reported − sum. Positive means idle cash NOBODY attributed. */
  unexplained: number | null;
  /** Whether the gap is large enough to be a defect rather than rounding. */
  mismatch: boolean;
  /** Reason codes the payload did not carry at all. */
  missing: string[];
}

/** Anything under a cent is rounding, not an unexplained dollar. */
const IDLE_EPSILON = 0.005;

/**
 * Reconcile the attribution against the reported idle balance.
 *
 * The contract says these are equal. When they are not, the panel says so in
 * words — an unattributed dollar is the single thing §22 exists to catch, and
 * rounding it into the total would turn a measurement defect into a clean
 * looking pie chart.
 */
export function reconcileIdle(
  byReason: IdleByReason | null | undefined,
  reportedIdleUsd: number | null,
  expectedCodes: readonly string[],
): IdleReconciliation {
  const entries = Object.entries(byReason ?? {})
    .map(([k, v]) => [k, num(v)] as const)
    .filter((e): e is readonly [string, number] => e[1] !== null);
  const sum = entries.reduce((a, [, v]) => a + v, 0);
  const unexplained = reportedIdleUsd === null ? null : reportedIdleUsd - sum;
  return {
    sum,
    reported: reportedIdleUsd,
    unexplained,
    mismatch: unexplained !== null && Math.abs(unexplained) > IDLE_EPSILON,
    missing: expectedCodes.filter((c) => num((byReason ?? {})[c]) === null),
  };
}

// ==========================================================================
// §4 — the live opportunity board
// ==========================================================================

/**
 * The board from the scanner, when the allocator's own queue is not served.
 *
 * This is a genuine fallback and the panel labels it as one, because the two
 * are not the same object: the scanner row is what the ranker saw this cycle,
 * the queue is what the allocator was actually choosing between. They agree
 * most of the time and the difference is exactly where a selection bug would
 * live, so the screen never claims to be showing the queue when it is showing
 * the scan.
 *
 * The EV column is the sharper difference: `QueuedOpportunity.net_ev_bps` is
 * basis points of expected NET edge, while the scanner's `net_edge_bps` is the
 * ranker's own composite reading. Both are bps; neither is dollars. The panel
 * prints the unit.
 */
export function deriveOpportunities(
  scanner: ScannerRow[] | null | undefined,
): OpportunityRow[] {
  if (!scanner || scanner.length === 0) return [];
  const rows = scanner.map((r) => {
    const decision = r.decision ? String(r.decision).toUpperCase() : null;
    const rejected = decision === "NO_TRADE" || !!r.reject_reason;
    return {
      symbol: r.symbol,
      strategy: r.strategy,
      net_ev_bps: num(r.net_edge_bps),
      status: rejected ? "REJECT" : decision === "BUY" ? "SELECTED" : "QUEUED",
      reject_reason: r.reject_reason ?? null,
      rank: null as number | null,
    } satisfies OpportunityRow;
  });

  // Rank is the allocator's ordering, so only the candidates that are still
  // in the running get one. A refused candidate has no rank — printing "7th"
  // next to REJECT would suggest it was in a queue it never entered.
  const live = rows
    .filter((r) => r.status !== "REJECT" && r.net_ev_bps !== null)
    .sort((a, b) => (b.net_ev_bps ?? 0) - (a.net_ev_bps ?? 0));
  live.forEach((r, i) => {
    r.rank = i + 1;
  });

  return [
    ...live,
    ...rows
      .filter((r) => r.status === "REJECT" || r.net_ev_bps === null)
      .sort((a, b) => (b.net_ev_bps ?? -Infinity) - (a.net_ev_bps ?? -Infinity)),
  ];
}

/**
 * The board, from the worker's queue when it serves one.
 *
 * `OpportunityQueue.as_dict()` emits `entries`, already ordered worst-EV-last,
 * and every entry on it is by definition still queued — the queue holds what
 * had no capital, not what was refused. So rank comes from the queue's own
 * ordering rather than being recomputed here: re-sorting the board would mean
 * the screen disagreeing with the allocator about which candidate is first.
 */
export function opportunityRows(
  served: OpportunityRow[] | null | undefined,
  scanner: ScannerRow[] | null | undefined,
): { rows: OpportunityRow[]; derived: boolean } {
  if (served && served.length > 0) {
    return {
      rows: served.map((r, i) => ({
        ...r,
        rank: num(r.rank) ?? i + 1,
        status: r.status ?? (r.reject_reason ? "REJECT" : "QUEUED"),
      })),
      derived: false,
    };
  }
  return { rows: deriveOpportunities(scanner), derived: true };
}
