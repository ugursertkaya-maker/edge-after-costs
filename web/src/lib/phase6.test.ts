/**
 * Tests for the Phase 6 derivations.
 *
 * These are not unit tests for their own sake. Each one pins a statement the
 * screen makes out loud and would be wrong to make quietly:
 *
 *   * the concentration flag stays INACTIVE against today's real data (BTC is
 *     41.7% of 139 trades, not 70%+), and fires only over a real sample;
 *   * an empty holding bucket is absent, not zero;
 *   * strategies are never pooled into one bucket row;
 *   * unattributed idle cash is reported with its sign intact.
 *
 * Run: `npm test` in `web/` (Node's own runner; no test framework installed).
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  CONCENTRATION_MIN_SAMPLE,
  bucketFor,
  concentrationReading,
  deriveConcentration,
  deriveHoldingBuckets,
  deriveOpportunities,
  holdingSecondsOf,
  medianHoldByStrategy,
  normalizeBucketKey,
  reconcileIdle,
} from "./phase6.ts";
import type { ScannerRow, TradeRow } from "./types.ts";

function trade(over: Partial<TradeRow> & { id: string }): TradeRow {
  return {
    symbol: "BTCUSD",
    strategy: "flow_impulse",
    entry_ms: 1_000_000,
    exit_ms: 1_030_000,
    entry_price: 100,
    exit_price: 101,
    exit_reason: "TAKE_PROFIT",
    gross_pnl_usd: 1,
    fees_usd: 0.1,
    slippage_usd: 0,
    net_pnl_usd: 0.9,
    ...over,
  } as TradeRow;
}

// -------------------------------------------------------------- buckets

test("bucket boundaries are half-open: lower inclusive, upper exclusive", () => {
  assert.equal(bucketFor(0)?.key, "lt_30s");
  assert.equal(bucketFor(29.9)?.key, "lt_30s");
  // 30 s belongs to the 30–60 bucket, not to <30. flow_impulse sits on this
  // edge by design, so an off-by-one here would move a whole strategy.
  assert.equal(bucketFor(30)?.key, "30_60s");
  assert.equal(bucketFor(59.9)?.key, "30_60s");
  assert.equal(bucketFor(60)?.key, "1_2m");
  assert.equal(bucketFor(71)?.key, "1_2m");
  assert.equal(bucketFor(1499)?.key, "10_25m");
  assert.equal(bucketFor(1500)?.key, "25_60m");
  assert.equal(bucketFor(3600)?.key, "gt_60m");
  assert.equal(bucketFor(999_999)?.key, "gt_60m");
});

test("a negative or missing hold is not a bucket", () => {
  assert.equal(bucketFor(-1), null);
  assert.equal(bucketFor(null), null);
  assert.equal(bucketFor(Number.NaN), null);
});

test("holding seconds prefer the reported figure over the timestamp span", () => {
  assert.equal(
    holdingSecondsOf(trade({ id: "a", holding_seconds: 42, entry_ms: 0, exit_ms: 999_000 })),
    42,
  );
  assert.equal(holdingSecondsOf(trade({ id: "b", entry_ms: 1000, exit_ms: 31_000 })), 30);
  assert.equal(holdingSecondsOf(trade({ id: "c", entry_ms: null, exit_ms: null })), null);
});

test("bucket keys the worker may spell differently still land in one column", () => {
  assert.equal(normalizeBucketKey("25-60m"), "25_60m");
  assert.equal(normalizeBucketKey(" <30s "), "lt_30s");
  assert.equal(normalizeBucketKey("60m_plus"), "gt_60m");
  // An unknown key survives as itself rather than being dropped silently.
  assert.equal(normalizeBucketKey("weird_bucket"), "weird_bucket");
  assert.equal(normalizeBucketKey(null), null);
});

test("strategies are never pooled into one bucket row", () => {
  const grid = deriveHoldingBuckets([
    trade({ id: "1", strategy: "flow_impulse", holding_seconds: 32, net_pnl_usd: 0.4 }),
    trade({ id: "2", strategy: "momentum_scalp", holding_seconds: 71, net_pnl_usd: -0.2 }),
    trade({ id: "3", strategy: "flow_impulse", holding_seconds: 33, net_pnl_usd: 0.1 }),
  ]);
  assert.deepEqual(grid.strategies.sort(), ["flow_impulse", "momentum_scalp"]);
  // ~32 s and ~71 s land in DIFFERENT buckets and different rows; neither row
  // may carry the other's trades.
  assert.equal(grid.cells.flow_impulse["30_60s"].trades, 2);
  assert.equal(grid.cells.momentum_scalp["1_2m"].trades, 1);
  assert.equal(grid.cells.flow_impulse["1_2m"], undefined);
  assert.equal(grid.cells.momentum_scalp["30_60s"], undefined);
  assert.equal(grid.sample, 3);
});

test("a bucket with no trades is ABSENT, never a zero cell", () => {
  const grid = deriveHoldingBuckets([
    trade({ id: "1", strategy: "flow_impulse", holding_seconds: 10, net_pnl_usd: 0 }),
  ]);
  // Measured zero: present, with a trade count behind it.
  assert.equal(grid.cells.flow_impulse.lt_30s.trades, 1);
  assert.equal(grid.cells.flow_impulse.lt_30s.net, 0);
  // Never measured: the key does not exist at all, which is what lets the
  // table draw a mark instead of "$0.00".
  assert.equal(grid.cells.flow_impulse["2_5m"], undefined);
  assert.equal("2_5m" in grid.cells.flow_impulse, false);
});

test("a trade whose net P&L was not reported is counted, not summed as zero", () => {
  const grid = deriveHoldingBuckets([
    trade({ id: "1", holding_seconds: 10, net_pnl_usd: 0.5 }),
    trade({ id: "2", holding_seconds: 11, net_pnl_usd: null }),
  ]);
  const cell = grid.cells.flow_impulse.lt_30s;
  assert.equal(cell.trades, 2);
  assert.equal(cell.unmeasured, 1);
  assert.equal(cell.net, 0.5);
});

test("trades with no establishable hold are dropped and reported, not hidden", () => {
  const grid = deriveHoldingBuckets([
    trade({ id: "1", holding_seconds: 10 }),
    trade({ id: "2", holding_seconds: null, entry_ms: null, exit_ms: null }),
  ]);
  assert.equal(grid.sample, 1);
  assert.equal(grid.undated, 1);
});

test("median hold separates the fast strategy from the slow one", () => {
  const med = medianHoldByStrategy([
    trade({ id: "1", strategy: "flow_impulse", holding_seconds: 30 }),
    trade({ id: "2", strategy: "flow_impulse", holding_seconds: 34 }),
    trade({ id: "3", strategy: "momentum_scalp", holding_seconds: 71 }),
  ]);
  assert.equal(med.flow_impulse, 32);
  assert.equal(med.momentum_scalp, 71);
});

// -------------------------------------------------------- concentration

test("per-coin shares carry their own denominator", () => {
  const trades = [
    ...Array.from({ length: 58 }, (_, i) =>
      trade({ id: `b${i}`, symbol: "BTCUSD", notional_usd: 50, net_pnl_usd: 0.1 }),
    ),
    ...Array.from({ length: 51 }, (_, i) =>
      trade({ id: `e${i}`, symbol: "ETHUSD", notional_usd: 50, net_pnl_usd: -0.05 }),
    ),
    ...Array.from({ length: 30 }, (_, i) =>
      trade({ id: `s${i}`, symbol: "SOLUSD", notional_usd: 50, net_pnl_usd: 0.02 }),
    ),
  ];
  const d = deriveConcentration(trades);
  assert.equal(d.tradeSample, 139);
  const btc = d.rows.find((r) => r.symbol === "BTCUSD")!;
  // The live reading the spec opens with: 58 of 139 is 41.7%, not 70%+.
  assert.equal(btc.trades, 58);
  assert.ok(Math.abs((btc.trade_share_pct as number) - 41.7) < 0.1);
  assert.ok(Math.abs((btc.capital_share_pct as number) - 41.7) < 0.1);
  assert.equal(d.notionalTotal, 139 * 50);
});

test("an unreported notional leaves the capital denominator, it does not become zero", () => {
  const d = deriveConcentration([
    trade({ id: "1", symbol: "BTCUSD", notional_usd: 100 }),
    trade({ id: "2", symbol: "ETHUSD", notional_usd: null }),
  ]);
  assert.equal(d.notionalMissing, 1);
  assert.equal(d.notionalTotal, 100);
  const eth = d.rows.find((r) => r.symbol === "ETHUSD")!;
  assert.equal(eth.capital_allocated_usd, null);
});

test("signal counts are never inferred from the trade list", () => {
  const d = deriveConcentration([trade({ id: "1" })]);
  assert.equal(d.rows[0].signals, null);
  assert.equal(d.rows[0].qualified_signals, null);
  assert.equal(d.rows[0].rejected_signals, null);
});

test("BTC_CONCENTRATION stays INACTIVE against today's real data", () => {
  // 41.7% of 139 closed trades — the flag must not fire, and the reading must
  // be a measured "clear", not an absence.
  const r = concentrationReading(41.7, 139);
  assert.equal(r.verdict, "clear");
  assert.equal(r.sample, 139);
});

test("the flag fires only above the threshold AND over a real sample", () => {
  assert.equal(concentrationReading(85, 139).verdict, "flagged");
  assert.equal(concentrationReading(70, 139).verdict, "clear", "70% is not ABOVE 70%");
  // 100% of two trades is two trades, not concentration.
  assert.equal(concentrationReading(100, 2).verdict, "thin");
  assert.equal(concentrationReading(100, CONCENTRATION_MIN_SAMPLE - 1).verdict, "thin");
  assert.equal(concentrationReading(100, CONCENTRATION_MIN_SAMPLE).verdict, "flagged");
  assert.equal(concentrationReading(null, 139).verdict, "unmeasured");
  assert.equal(concentrationReading(41.7, null).verdict, "unmeasured");
});

// ------------------------------------------------------------ idle cash

const NINE = [
  "NO_QUALIFIED_SIGNAL",
  "EXECUTION_COST",
  "RISK_LIMIT",
  "CORRELATION_LIMIT",
  "LIQUIDITY_LIMIT",
  "COOLDOWN",
  "SCANNER_DELAY",
  "CAPITAL_STATE_BUG",
  "OTHER",
];

test("idle reasons that do not sum to the balance are reported with their sign", () => {
  const r = reconcileIdle({ NO_QUALIFIED_SIGNAL: 300, EXECUTION_COST: 50 }, 400, NINE);
  assert.equal(r.sum, 350);
  assert.equal(r.unexplained, 50); // positive: $50 idle that nobody explained
  assert.equal(r.mismatch, true);
});

test("over-attribution is a different fault from under-attribution", () => {
  const r = reconcileIdle({ NO_QUALIFIED_SIGNAL: 500 }, 400, NINE);
  assert.equal(r.unexplained, -100);
  assert.equal(r.mismatch, true);
});

test("sub-cent differences are rounding, not an unexplained dollar", () => {
  const r = reconcileIdle({ NO_QUALIFIED_SIGNAL: 350.001 }, 350, NINE);
  assert.equal(r.mismatch, false);
});

test("an unreported reason is not a zero-dollar reason", () => {
  const r = reconcileIdle({ NO_QUALIFIED_SIGNAL: 350 }, 350, NINE);
  assert.equal(r.missing.length, 8);
  assert.ok(r.missing.includes("SCANNER_DELAY"));
  assert.ok(!r.missing.includes("NO_QUALIFIED_SIGNAL"));
});

test("a missing idle balance is not a matching one", () => {
  const r = reconcileIdle({ NO_QUALIFIED_SIGNAL: 350 }, null, NINE);
  assert.equal(r.unexplained, null);
  assert.equal(r.mismatch, false);
});

// --------------------------------------------------- opportunity board

function scan(over: Partial<ScannerRow> & { symbol: string }): ScannerRow {
  return {
    price: 100,
    long_score: 1,
    short_score: 0,
    regime: "TRENDING",
    momentum: 5,
    volume_ratio: 1,
    spread_bps: 2,
    imbalance: 0,
    net_edge_bps: 5,
    strategy: "flow_impulse",
    decision: "WATCH",
    reject_reason: null,
    ...over,
  } as ScannerRow;
}

test("the board ranks live candidates by net EV and leaves refusals unranked", () => {
  const rows = deriveOpportunities([
    scan({ symbol: "BTCUSD", net_edge_bps: 3 }),
    scan({ symbol: "ETHUSD", net_edge_bps: 9, decision: "BUY" }),
    scan({ symbol: "SOLUSD", net_edge_bps: 12, decision: "NO_TRADE", reject_reason: "WIDE_SPREAD" }),
  ]);
  assert.equal(rows[0].symbol, "ETHUSD");
  assert.equal(rows[0].rank, 1);
  assert.equal(rows[0].status, "SELECTED");
  assert.equal(rows[1].symbol, "BTCUSD");
  assert.equal(rows[1].rank, 2);

  const sol = rows.find((r) => r.symbol === "SOLUSD")!;
  // Highest EV of the three and still unranked: it never entered the queue,
  // and a rank would claim it did.
  assert.equal(sol.status, "REJECT");
  assert.equal(sol.rank, null);
  assert.equal(sol.reject_reason, "WIDE_SPREAD");
});

test("a candidate with a reject reason is a refusal whatever its decision says", () => {
  const rows = deriveOpportunities([
    scan({ symbol: "ADAUSD", decision: "WATCH", reject_reason: "THIN_DEPTH" }),
  ]);
  assert.equal(rows[0].status, "REJECT");
  assert.equal(rows[0].rank, null);
});

test("an empty scan produces an empty board, not a fabricated row", () => {
  assert.deepEqual(deriveOpportunities([]), []);
  assert.deepEqual(deriveOpportunities(null), []);
});
