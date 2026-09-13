import { Panel } from "@/components/ui/Panel";
import { DataState, Skeleton } from "@/components/ui/DataState";
import {
  DASH,
  formatDuration,
  formatInt,
  formatRate,
  formatSignedMicroMoney,
  strategyLabel,
  toNum,
  tone,
} from "@/lib/format";
import {
  HOLDING_BUCKETS,
  deriveHoldingBuckets,
  medianHoldByStrategy,
  normalizeBucketKey,
} from "@/lib/phase6";
import type { BucketCell, BucketGrid } from "@/lib/phase6";
import type { HoldingBucketState, TradeRow } from "@/lib/types";

/**
 * HOLDING-TIME BUCKETS — §20.
 *
 * Net performance by holding bucket, one row per STRATEGY, never pooled.
 *
 * Pooling is the failure mode this panel exists to prevent. `flow_impulse` has
 * a ~32 s median hold and `momentum_scalp` a ~71 s one; a single "30–60 sn"
 * row would average an intentionally fast strategy against the tail of a slow
 * one and call the average a result. Worse, it would make a strategy-specific
 * holding policy look wrong: a 30-second `flow_impulse` trade is CORRECT when
 * the impulse is spent, and only a per-strategy row can say so.
 *
 * The second rule is the empty cell. A strategy that has never held for 2–5
 * minutes renders BLANK, not "$0.00". Zero net P&L over four trades and no
 * trades at all are different statements, and a grid that draws them the same
 * way invents a flat performance reading out of an absence of data.
 */

/** Below this a cell is shown but not read as a performance figure. */
const THIN_SAMPLE = 5;

function cellOf(grid: BucketGrid, strategy: string, bucket: string): BucketCell | undefined {
  return grid.cells[strategy]?.[bucket];
}

/**
 * Build the grid from the worker's rows when it serves them.
 *
 * Backend rows win over derivation, but they are folded into the same shape so
 * the table has exactly one rendering path — two code paths for the same grid
 * is how the served and derived versions drift into disagreeing.
 */
function gridFromRows(state: HoldingBucketState): BucketGrid {
  const grid: BucketGrid = { strategies: [], cells: {}, totals: {}, sample: 0, undated: 0 };
  for (const row of state.rows ?? []) {
    const strategy = row.strategy ?? "—";
    const bucket = normalizeBucketKey(row.bucket);
    const trades = toNum(row.trades);
    if (!bucket || trades === null) continue;
    const net = toNum(row.net_pnl_usd);
    const winRate = toNum(row.win_rate);
    const cell: BucketCell = {
      trades,
      net: net ?? 0,
      gross: toNum(row.gross_pnl_usd),
      cost: toNum(row.total_cost_usd),
      wins: winRate === null ? 0 : Math.round(winRate * trades),
      unmeasured: net === null ? trades : 0,
    };
    (grid.cells[strategy] ??= {})[bucket] = cell;

    const total = (grid.totals[strategy] ??= {
      trades: 0, net: 0, gross: null, cost: null, wins: 0, unmeasured: 0,
    });
    total.trades += cell.trades;
    total.net += cell.net;
    total.wins += cell.wins;
    total.unmeasured += cell.unmeasured;
    if (cell.gross !== null) total.gross = (total.gross ?? 0) + cell.gross;
    if (cell.cost !== null) total.cost = (total.cost ?? 0) + cell.cost;
    grid.sample += cell.trades;
  }
  grid.strategies = Object.keys(grid.cells).sort(
    (a, b) => (grid.totals[b]?.trades ?? 0) - (grid.totals[a]?.trades ?? 0),
  );
  return grid;
}

/** One cell: net P&L over its own trade count. The count is the point — a
    net figure without its denominator is the lie this project names by name. */
function Cell({ cell }: { cell: BucketCell | undefined }) {
  if (!cell || cell.trades === 0) {
    // Deliberately empty. See the panel docstring: this is "never held here",
    // which is a different statement from "held here and made nothing".
    return (
      <td className="hb-cell is-none" title="This strategy closed no trade in this bucket">
        <span className="hb-empty" aria-label="no trades">
          ·
        </span>
      </td>
    );
  }
  const measured = cell.trades - cell.unmeasured;
  const net = measured > 0 ? cell.net : null;
  const thin = cell.trades < THIN_SAMPLE;
  return (
    <td
      className={`hb-cell${thin ? " is-thin" : ""}`}
      title={
        `${cell.trades} trades` +
        (cell.unmeasured > 0 ? ` (${cell.unmeasured} tanesinin net P&L'i bildirilmedi)` : "") +
        (measured > 0 ? ` · win rate ${formatRate(cell.wins / measured, 0)}` : "") +
        (thin ? " · sample too thin for a performance reading" : "")
      }
    >
      <span className={`hb-net ${tone(net) ?? "muted"}`}>
        {net === null ? DASH : formatSignedMicroMoney(net)}
      </span>
      <span className="hb-n">{formatInt(cell.trades)} trades</span>
    </td>
  );
}

export function HoldingBuckets({
  buckets,
  trades,
  loading,
}: {
  buckets: HoldingBucketState | null | undefined;
  trades: TradeRow[] | null;
  loading: boolean;
}) {
  const served = buckets?.rows && buckets.rows.length > 0 ? buckets : null;
  const grid = served ? gridFromRows(served) : deriveHoldingBuckets(trades);
  const derived = !served;

  const medians: Record<string, number> = served
    ? Object.fromEntries(
        Object.entries(buckets?.median_hold_seconds ?? {})
          .map(([k, v]) => [k, toNum(v)] as const)
          .filter((e): e is readonly [string, number] => e[1] !== null),
      )
    : medianHoldByStrategy(trades);

  const sample = toNum(served?.sample) ?? grid.sample;

  const meta = (
    <>
      <span className="pill is-off" title="Closed trades the buckets were computed from">
        {formatInt(sample)} trades
      </span>
      <span className="pill is-off" title="Row count — no row is ever summed with another">
        {formatInt(grid.strategies.length)} strategies
      </span>
    </>
  );

  return (
    <Panel
      idx="20"
      title="Holding-time buckets"
      note="net performance after costs — per strategy, never pooled"
      meta={meta}
      flush
      footer={
        "Rows are NEVER summed. flow_impulse holds for a median of ~32 seconds and momentum_scalp for ~71; a single «30-60s» row would deliberately average a fast strategy against the tail of a slow one and present the mean as a result. A 30-second flow_impulse round trip is the RIGHT round trip if the impulse is spent. " +
        "An empty cell (·) means that strategy closed no round trip in that bucket; it does not mean zero dollars — the two are not drawn the same way here. Every cell carries its own trade count: a net figure without a denominator is not a performance reading, and neither is a five-trade cell. " +
        (derived
          ? "SOURCE: the closed-trade list (the API publishes the last 200 round trips). Once the worker starts publishing its own bucket block (`holding_buckets.rows`), this table will be fed from that."
          : "SOURCE: the worker's own bucket block.")
      }
    >
      {loading && grid.strategies.length === 0 ? (
        <Skeleton rows={5} />
      ) : grid.strategies.length === 0 ? (
        <DataState
          title="No bucket distribution"
          text="No strategy has produced a closed round trip yet, so no bucket has a measurement. An empty table is not zero performance: it is the reading that there is no round trip to measure."
        />
      ) : (
        <>
          <div className="tbl-wrap">
            <table className="tbl hb">
              <thead>
                <tr>
                  <th className="l">Strategy</th>
                  <th className="l" title="This strategy's median holding time — the evidence for why rows are kept separate">
                    Medyan tutma
                  </th>
                  {HOLDING_BUCKETS.map((b) => (
                    <th key={b.key} className="c">
                      {b.label}
                    </th>
                  ))}
                  <th title="Row total — only this strategy's own buckets">Total net</th>
                  <th title="Closed round trips for this strategy">Trades</th>
                </tr>
              </thead>
              <tbody>
                {grid.strategies.map((s) => {
                  const total = grid.totals[s];
                  const measured = total ? total.trades - total.unmeasured : 0;
                  return (
                    <tr key={s}>
                      <td className="l sym" title={s}>
                        {strategyLabel(s)}
                      </td>
                      <td className="l dim" title="medyan, saniye">
                        {medians[s] === undefined ? DASH : formatDuration(medians[s])}
                      </td>
                      {HOLDING_BUCKETS.map((b) => (
                        <Cell key={b.key} cell={cellOf(grid, s, b.key)} />
                      ))}
                      <td className={`net ${measured > 0 ? (tone(total?.net ?? null) ?? "muted") : "muted"}`}>
                        {measured > 0 ? formatSignedMicroMoney(total?.net ?? null) : DASH}
                      </td>
                      <td className="dim">{formatInt(total?.trades ?? null)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          <div className="hb-foot">
            <span className="hb-legend">
              <span className="hb-empty" aria-hidden>
                ·
              </span>{" "}
              no trades in this bucket — not zero, not measured
            </span>
            <span className="hb-legend is-thin">
              dimmed cell: under {THIN_SAMPLE} trades, does not count as a performance reading
            </span>
            {grid.undated > 0 && (
              <span className="hb-legend is-warn">
                {formatInt(grid.undated)} round trips did not report a holding time and were
                written to no bucket — they were not silently added to any row
              </span>
            )}
          </div>
        </>
      )}
    </Panel>
  );
}
