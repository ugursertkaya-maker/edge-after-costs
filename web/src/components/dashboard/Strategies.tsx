"use client";

import { useMemo, useState } from "react";
import { Panel } from "@/components/ui/Panel";
import { DataState, Skeleton } from "@/components/ui/DataState";
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
  strategyLabel,
  tone,
} from "@/lib/format";
import type { Num, StrategyRow } from "@/lib/types";

type SortKey = keyof Omit<StrategyRow, "strategy">;

const COLUMNS: { key: SortKey; label: string; title?: string }[] = [
  { key: "trades", label: "Trades" },
  { key: "win_rate", label: "Win rate" },
  { key: "avg_win", label: "Avg. win" },
  { key: "avg_loss", label: "Avg. loss" },
  { key: "profit_factor", label: "Profit factor" },
  { key: "expectancy", label: "Expectancy" },
  { key: "fees", label: "Fees" },
  { key: "net_pnl", label: "Net P&L" },
  { key: "max_drawdown", label: "Max DD" },
  { key: "avg_holding_seconds", label: "Ort. tutma" },
  { key: "capital_turnover", label: "Capital turnover" },
];

function cell(key: SortKey, row: StrategyRow): { text: string; cls: string } {
  const v = row[key] as Num;
  switch (key) {
    case "trades":
      return { text: formatInt(v), cls: "" };
    case "win_rate":
      return { text: formatRate(v), cls: "dim" };
    case "avg_win":
      return { text: formatMicroMoney(v), cls: v ? "pos" : "muted" };
    case "avg_loss":
      // Losses are stored as a magnitude; rendering them unsigned next to a
      // signed win column would read as a gain.
      return {
        text: v === null || v === undefined ? DASH : formatMicroMoney(-Math.abs(v)),
        cls: v ? "neg" : "muted",
      };
    case "profit_factor":
      return { text: formatFactor(v), cls: "" };
    case "expectancy":
      return { text: formatSignedMicroMoney(v), cls: tone(v) ?? "muted" };
    case "fees":
      return { text: formatMicroMoney(v), cls: "dim" };
    case "net_pnl":
      return { text: formatSignedMicroMoney(v), cls: `net ${tone(v) ?? "muted"}` };
    case "max_drawdown":
      return {
        text: v === null || v === undefined ? DASH : formatMoney(-Math.abs(v)),
        cls: v ? "neg" : "muted",
      };
    case "avg_holding_seconds":
      return { text: formatDuration(v), cls: "dim" };
    case "capital_turnover":
      return { text: formatRatio(v), cls: "dim" };
    default:
      return { text: DASH, cls: "muted" };
  }
}

/**
 * Per-strategy attribution, sorted by net P&L by default.
 *
 * The default sort is the argument: a strategy with a 78% win rate and a
 * negative net P&L is losing money, and ranking this table by win rate would
 * put it on top. Net P&L after costs is the only ordering that answers "which
 * of these should keep running".
 */
export function Strategies({
  rows,
  loading,
}: {
  rows: StrategyRow[] | null;
  loading: boolean;
}) {
  const [sortKey, setSortKey] = useState<SortKey>("net_pnl");

  const sorted = useMemo(() => {
    if (!rows) return null;
    return [...rows].sort((a, b) => {
      const av = a[sortKey] as Num;
      const bv = b[sortKey] as Num;
      // Unmeasured rows sink to the bottom regardless of direction — they are
      // not "the worst", they are simply not yet comparable.
      if (av === null || av === undefined) return 1;
      if (bv === null || bv === undefined) return -1;
      return bv - av;
    });
  }, [rows, sortKey]);

  return (
    <Panel
      idx="06"
      title="Strategy ranking"
      note="ordered by net P&L after costs"
      flush
      footer="Win rate does not decide the ranking. A strategy that takes small profits on 70% of its trades and occasionally writes a large loss is a losing strategy on net P&L; that is why this table sorts by NET P&L by default and is read alongside expectancy and profit factor."
    >
      {loading && !sorted ? (
        <Skeleton rows={4} />
      ) : !sorted || sorted.length === 0 ? (
        <DataState
          title="No strategy statistics yet"
          text="No strategy has produced a closed round trip yet. As the first trades close, every module will be listed here with its own after-cost scorecard."
        />
      ) : (
        <div className="tbl-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th className="l">Strategy</th>
                {COLUMNS.map((c) => (
                  <th
                    key={c.key}
                    className="sortable"
                    onClick={() => setSortKey(c.key)}
                    title={`Sort by the ${c.label} column`}
                  >
                    {c.label}
                    {sortKey === c.key && <span className="caret">▾</span>}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sorted.map((r) => (
                <tr key={r.strategy}>
                  <td className="l sym" title={r.strategy}>
                    {strategyLabel(r.strategy)}
                  </td>
                  {COLUMNS.map((c) => {
                    const { text, cls } = cell(c.key, r);
                    return (
                      <td key={c.key} className={cls}>
                        {text}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
