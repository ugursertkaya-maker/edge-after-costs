import {
  DASH,
  formatMicroMoney,
  formatPct,
  formatSignedMicroMoney,
  tone,
} from "@/lib/format";
import type { DashboardMetrics } from "@/lib/types";

/**
 * The governing statement of the whole terminal, given the largest type on the
 * screen: a gross gain is not profit.
 *
 * Three values, never one. PROFIT BEFORE COSTS is what the price moves earned,
 * TOTAL COST is what capturing them cost in fees, spread and slippage, and
 * only TRUE NET PROFIT is money. V1's measured result was that the middle term
 * is large enough to flip the sign of the third, which is exactly why it is
 * drawn here as an equation instead of being folded away into a P&L line.
 *
 * When the worker has not reported these yet the block keeps its structure and
 * shows em dashes: an unmeasured cost is not a zero cost, and the difference
 * between those two statements is the entire thesis.
 */
export function CostSplit({ metrics }: { metrics: DashboardMetrics | null }) {
  const gross = metrics?.profit_before_costs ?? null;
  const costs = metrics?.total_costs ?? null;
  const net = metrics?.real_net_profit ?? null;
  const reported = gross !== null || costs !== null || net !== null;

  // What share of the gross move the costs consumed. Only meaningful when the
  // gross is a real, positive figure — a ratio against a loss says nothing.
  const bite =
    gross !== null && costs !== null && gross > 0
      ? (Math.abs(costs) / gross) * 100
      : null;

  return (
    <section className={`split${reported ? "" : " is-empty"}`} aria-label="Profit decomposition">
      <div className="split-cell">
        <span className="split-k">Profit before costs</span>
        <span className={`split-v ${gross === null ? "is-empty" : (tone(gross) ?? "")}`}>
          {formatSignedMicroMoney(gross)}
        </span>
        <span className="split-n">what the price move earned — not profit</span>
      </div>

      <span className="split-op" aria-hidden>
        −
      </span>

      <div className="split-cell">
        <span className="split-k">Total cost</span>
        <span className={`split-v ${costs === null ? "is-empty" : "neg"}`}>
          {costs === null ? DASH : formatMicroMoney(-Math.abs(costs))}
        </span>
        <span className="split-n">
          {bite === null
            ? "fees + spread + slippage"
            : `fees + spread + slippage · ${formatPct(bite, 0)} of gross`}
        </span>
      </div>

      <span className="split-op" aria-hidden>
        =
      </span>

      <div className="split-cell is-final">
        <span className="split-k">True net profit</span>
        <span className={`split-v ${net === null ? "is-empty" : (tone(net) ?? "")}`}>
          {formatSignedMicroMoney(net)}
        </span>
        <span className="split-n">
          {reported
            ? "hesaba fiilen giren tutar"
            : "dollars, once the worker starts reporting all three figures"}
        </span>
      </div>
    </section>
  );
}
