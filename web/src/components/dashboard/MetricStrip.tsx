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
      : `nakit ${formatMoney(m.cash)}`;

  return (
    <div className="metrics">
      <Metric
        label="Mevcut sermaye"
        value={formatMoney(m?.equity)}
        note={equityNote}
        sign={
          m?.equity === null || m?.equity === undefined || startingEquity === null
            ? undefined
            : m.equity - startingEquity
        }
      />
      <Metric
        label="Bugünkü net P&L"
        value={formatSignedMicroMoney(m?.pnl_today)}
        note="komisyon ve kayma dahil"
        sign={m?.pnl_today}
      />
      <Metric
        label="Toplam net P&L"
        value={formatSignedMicroMoney(m?.pnl_total)}
        note="tüm defter ömrü"
        sign={m?.pnl_total}
      />
      <Metric label="Açık pozisyon" value={formatInt(m?.open_positions)} />
      <Metric label="İşlem sayısı" value={formatInt(m?.trades)} note="kapanmış" />
      <Metric
        label="Win rate"
        value={formatRate(m?.win_rate)}
        note="tek başına anlamsız"
      />
      <Metric
        label="Profit factor"
        value={formatFactor(m?.profit_factor)}
        note="brüt kâr / brüt zarar"
      />
      <Metric
        label="Expectancy / işlem"
        value={formatMicroMoney(m?.expectancy)}
        note="net, işlem başına"
        sign={m?.expectancy}
      />
      <Metric
        label="Max drawdown"
        value={m?.max_drawdown === null || m?.max_drawdown === undefined
          ? DASH
          : formatMoney(-Math.abs(m.max_drawdown))}
        note="zirveden en derin düşüş"
        sign={m?.max_drawdown === null || m?.max_drawdown === undefined
          ? undefined
          : -Math.abs(m.max_drawdown)}
      />
      <Metric
        label="Sermaye devri"
        value={formatRatio(m?.capital_turnover)}
        note="işlem hacmi / sermaye"
      />
      <Metric
        label="Ort. tutma süresi"
        value={formatDuration(m?.avg_holding_seconds)}
      />
    </div>
  );
}
