import {
  DASH,
  formatDuration,
  formatFactor,
  formatInt,
  formatMicroMoney,
  formatMoney,
  formatRate,
  formatRatio,
  formatSignedMicroMoney,
  tone,
} from "@/lib/format";
import type { DashboardMetrics, Num } from "@/lib/types";

function Metric({
  label,
  value,
  note,
  sign,
}: {
  label: string;
  value: string;
  note?: string;
  /** The signed figure this tile is coloured by, when it has one. */
  sign?: Num;
}) {
  const t = sign === undefined ? undefined : tone(sign);
  const cls = value === DASH ? "is-empty" : t ?? "";
  return (
    <div className="metric">
      <span className="metric-k">{label}</span>
      <span className={`metric-v ${cls}`}>{value}</span>
      <span className="metric-note">{note ?? ""}</span>
    </div>
  );
}

/**
 * The eleven figures that decide whether this bot is worth running. They are
 * one strip, not a dashboard of cards, because they are read together: a win
 * rate without its profit factor and expectancy is a number that lies.
 */
export function MetricStrip({
  metrics,
  startingEquity,
}: {
  metrics: DashboardMetrics | null;
  startingEquity: Num;
}) {
  const m = metrics;
  const equityNote =
    m?.cash === null || m?.cash === undefined
      ? undefined
      : `cash ${formatMoney(m.cash)}`;

  return (
    <div className="metrics">
      <Metric
        label="Current equity"
        value={formatMoney(m?.equity)}
        note={equityNote}
        sign={
          m?.equity === null || m?.equity === undefined || startingEquity === null
            ? undefined
            : m.equity - startingEquity
        }
      />
      <Metric
        label="Net P&L today"
        value={formatSignedMicroMoney(m?.pnl_today)}
        note="fees and slippage included"
        sign={m?.pnl_today}
      />
      <Metric
        label="Total net P&L"
        value={formatSignedMicroMoney(m?.pnl_total)}
        note="whole life of the ledger"
        sign={m?.pnl_total}
      />
      <Metric label="Open positions" value={formatInt(m?.open_positions)} />
      <Metric label="Trades" value={formatInt(m?.trades)} note="closed" />
      <Metric
        label="Win rate"
        value={formatRate(m?.win_rate)}
        note="meaningless on its own"
      />
      <Metric
        label="Profit factor"
        value={formatFactor(m?.profit_factor)}
        note="gross profit / gross loss"
      />
      <Metric
        label="Expectancy / trade"
        value={formatMicroMoney(m?.expectancy)}
        note="net, per trade"
        sign={m?.expectancy}
      />
      <Metric
        label="Max drawdown"
        value={m?.max_drawdown === null || m?.max_drawdown === undefined
          ? DASH
          : formatMoney(-Math.abs(m.max_drawdown))}
        note="deepest fall from peak"
        sign={m?.max_drawdown === null || m?.max_drawdown === undefined
          ? undefined
          : -Math.abs(m.max_drawdown)}
      />
      <Metric
        label="Capital turnover"
        value={formatRatio(m?.capital_turnover)}
        note="traded notional / equity"
      />
      <Metric
        label="Avg. holding time"
        value={formatDuration(m?.avg_holding_seconds)}
      />
    </div>
  );
}
