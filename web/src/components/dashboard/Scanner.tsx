import { Panel } from "@/components/ui/Panel";
import { DataState, Skeleton } from "@/components/ui/DataState";
import {
  DASH,
  baseAsset,
  decisionLabel,
  formatBps,
  formatBpsAbs,
  formatNumber,
  formatPrice,
  formatRatio,
  regimeLabel,
  rejectReasonLabel,
  strategyLabel,
} from "@/lib/format";
import type { Num, ScannerRow } from "@/lib/types";

function decisionClass(code: string | null): string {
  switch (code) {
    case "BUY":
      return "buy";
    case "SELL":
      return "sell";
    case "WATCH":
      return "watch";
    default:
      return "none";
  }
}

/** The order-book imbalance drawn as a centre-anchored bar in [-1, 1]. */
function Imbalance({ value }: { value: Num }) {
  if (value === null || value === undefined) {
    return <span className="muted">{DASH}</span>;
  }
  const v = Math.max(-1, Math.min(1, value));
  const pct = Math.abs(v) * 50;
  return (
    <>
      <span
        className="imb"
        role="img"
        aria-label={`Order book imbalance ${v.toFixed(2)}`}
      >
        <span
          className={`imb-fill ${v >= 0 ? "bid" : "ask"}`}
          style={{ width: `${pct}%` }}
        />
      </span>
      <span className={`imb-val ${v > 0 ? "pos" : v < 0 ? "neg" : ""}`}>
        {v > 0 ? "+" : ""}
        {v.toFixed(2)}
      </span>
    </>
  );
}

/**
 * The live scan. One row per symbol in the universe, refreshed every 3 s.
 *
 * The column that matters is Beklenen Net Edge: a setup with a beautiful
 * momentum reading and a negative net edge is a losing setup, so the decision
 * cell is driven by the net figure and the reject reason is always attached to
 * it rather than hidden in a log.
 */
export function Scanner({
  rows,
  loading,
  connected,
}: {
  rows: ScannerRow[] | null;
  loading: boolean;
  connected: boolean;
}) {
  const meta = rows ? (
    <span className="panel-note">
      {rows.length} symbols · {rows.filter((r) => r.decision === "BUY").length} buys
      sinyali
    </span>
  ) : null;

  return (
    <Panel
      idx="03"
      title="Scanner"
      note="ordered by net edge — fees, spread and slippage deducted"
      meta={meta}
      flush
      footer="The decision cell is decided on net edge; a setup that is gross-positive but net-negative is a losing setup. Hover a NO TRADE cell to see the rejection reason."
    >
      {loading && !rows ? (
        <Skeleton rows={8} />
      ) : !rows || rows.length === 0 ? (
        <DataState
          scanning={connected}
          title="The scanner has not produced data yet"
          text={
            connected
              ? "The universe is loading and the order books are warming up. Symbols will be listed here once the first evaluation cycle completes."
              : "The exchange connection is down. No decision is made on a stale book, so this table is deliberately empty."
          }
        />
      ) : (
        <div className="tbl-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th className="l">Coin</th>
                <th>Price</th>
                <th>Long skor</th>
                <th>Short skor</th>
                <th className="l">Regime</th>
                <th>Momentum</th>
                <th>Volume</th>
                <th>Spread</th>
                <th className="l">Order book</th>
                <th>Beklenen net edge</th>
                <th className="l">Strategy</th>
                <th className="c">Karar</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const noTrade = r.decision === "NO_TRADE";
                const edgeCls =
                  r.net_edge_bps === null || r.net_edge_bps === undefined
                    ? "muted"
                    : r.net_edge_bps > 0
                      ? "pos"
                      : r.net_edge_bps < 0
                        ? "neg"
                        : "dim";
                return (
                  <tr key={r.symbol}>
                    <td className="l sym" title={r.symbol}>
                      {baseAsset(r.symbol)}
                    </td>
                    <td>{formatPrice(r.price)}</td>
                    <td className={r.long_score !== null ? "" : "muted"}>
                      {formatNumber(r.long_score, 2)}
                    </td>
                    <td className={r.short_score !== null ? "dim" : "muted"}>
                      {formatNumber(r.short_score, 2)}
                    </td>
                    <td className="l">
                      <span
                        className={`tag${r.regime === "ABNORMAL" ? " is-abnormal" : ""}`}
                        title={r.regime ?? undefined}
                      >
                        {regimeLabel(r.regime)}
                      </span>
                    </td>
                    <td
                      className={
                        r.momentum === null || r.momentum === undefined
                          ? "muted"
                          : r.momentum > 0
                            ? "pos"
                            : r.momentum < 0
                              ? "neg"
                              : "dim"
                      }
                      title="15-minute compound return, bps"
                    >
                      {formatBps(r.momentum)}
                    </td>
                    <td title="Last minute's volume / its moving average">
                      {formatRatio(r.volume_ratio)}
                    </td>
                    <td
                      className={
                        r.spread_bps !== null && r.spread_bps !== undefined && r.spread_bps > 10
                          ? "warn"
                          : "dim"
                      }
                      title="bps"
                    >
                      {formatBpsAbs(r.spread_bps)}
                    </td>
                    <td className="l">
                      <Imbalance value={r.imbalance} />
                    </td>
                    <td className={edgeCls} title="bps, net">
                      {formatBps(r.net_edge_bps)}
                    </td>
                    <td className="l dim" title={r.strategy ?? undefined}>
                      {r.strategy ? strategyLabel(r.strategy) : DASH}
                    </td>
                    <td className="c">
                      <span
                        className={`decision ${decisionClass(r.decision ?? null)}`}
                        title={
                          noTrade && r.reject_reason
                            ? `${rejectReasonLabel(r.reject_reason)} (${r.reject_reason})`
                            : (r.decision ?? undefined)
                        }
                      >
                        {decisionLabel(r.decision)}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
