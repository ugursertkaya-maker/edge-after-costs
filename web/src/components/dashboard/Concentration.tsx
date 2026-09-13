import { Panel } from "@/components/ui/Panel";
import { DataState, Skeleton } from "@/components/ui/DataState";
import {
  CONCENTRATION_VERDICT_LABELS,
  CONCENTRATION_VERDICT_NOTES,
  DASH,
  baseAsset,
  concentrationTone,
  formatInt,
  formatMoney,
  formatPct,
  formatSignedMicroMoney,
  funnelStageLabel,
  toNum,
  tone,
} from "@/lib/format";
import {
  CONCENTRATION_MIN_SAMPLE,
  CONCENTRATION_THRESHOLD_PCT,
  applyFunnel,
  concentrationReading,
  concentrationRows,
  deriveConcentration,
} from "@/lib/phase6";
import type {
  ConcentrationRow,
  ConcentrationState,
  FunnelReport,
  TradeRow,
} from "@/lib/types";

/**
 * BTC CONCENTRATION MONITOR — §21.
 *
 * The brief opens with an accusation ("the trader is BTC-only") and this panel
 * exists to answer it with counts instead of argument. Two headline figures do
 * that: what share of the TRADES went to BTC, and what share of the CAPITAL
 * did. They routinely disagree — a coin can be 40% of the trade count and 60%
 * of the money — and the disagreement is itself the reading, so they are never
 * collapsed into one "BTC dominance" number.
 *
 * The flag is a DIAGNOSTIC and the panel says so in its own words. Nothing in
 * this codebase suppresses, penalises or re-ranks BTC because it fired; the
 * spec is explicit that if BTC wins on net EV, BTC trades. What the flag asks
 * for is the funnel: concentration in the trade count and concentration in
 * what is *allowed to be a candidate* are different faults with the same
 * symptom, and only the per-symbol funnel separates them.
 *
 * The resting state is designed as carefully as the firing one. Today BTC is
 * 41.7% of 139 trades, so the flag renders inactive and it has to look like a
 * finished measurement — a threshold marked on a real track with a real
 * sample beside it — rather than like a panel whose data never arrived.
 */

interface Headline {
  symbol: string | null;
  sharePct: number | null;
  sample: number | null;
}

/**
 * The threshold meter.
 *
 * The 70% line is drawn ON the track, not in a caption: a threshold that lives
 * in a legend is a threshold the eye never compares against. The distance
 * between the fill and the line is the whole reading, in both directions.
 */
function ShareMeter({
  label,
  symbol,
  pct,
  sample,
  sampleUnit,
  kind,
  note,
}: {
  label: string;
  symbol: string | null;
  pct: number | null;
  sample: number | null;
  sampleUnit: string;
  kind: string;
  note: string;
}) {
  const fill = pct === null ? 0 : Math.max(0, Math.min(100, pct));
  return (
    <div className={`cshare ${concentrationTone(kind)}`}>
      <div className="cshare-head">
        <span className="cshare-k">{label}</span>
        <span className={`cshare-v${pct === null ? " is-empty" : ""}`}>
          {pct === null ? DASH : formatPct(pct)}
        </span>
        <span className="cshare-sym">{symbol ? baseAsset(symbol) : DASH}</span>
      </div>
      <div
        className="cshare-track"
        role="img"
        aria-label={
          pct === null
            ? `${label} not measured`
            : `${label} ${formatPct(pct)}, threshold ${CONCENTRATION_THRESHOLD_PCT}%`
        }
      >
        <span className={`cshare-fill${pct === null ? " is-unknown" : ""}`} style={{ width: `${fill}%` }} />
        <span
          className="cshare-limit"
          style={{ left: `${CONCENTRATION_THRESHOLD_PCT}%` }}
          title={`BTC_CONCENTRATION threshold: ${CONCENTRATION_THRESHOLD_PCT}%`}
        />
      </div>
      <div className="cshare-foot">
        <span className="cshare-n">
          {sample === null ? "sample not reported" : `${formatInt(sample)} ${sampleUnit}`}
        </span>
        <span className="cshare-lim">threshold {CONCENTRATION_THRESHOLD_PCT}%</span>
      </div>
      <p className="cshare-x">{note}</p>
    </div>
  );
}

/**
 * The flag, in whichever of its four states the data licenses.
 *
 * `clear` is not the absence of a reading, it is a reading: measured, over a
 * stated sample, below the line. It gets the same frame, the same eyebrow and
 * the same explanatory line as `flagged` — only the tone changes. A resting
 * diagnostic that looks like an empty box teaches the operator to distrust it
 * the day it fires.
 */
function Flag({
  verdict,
  sharePct,
  sample,
  symbol,
  cause,
  causeKnown,
}: {
  verdict: string;
  sharePct: number | null;
  sample: number | null;
  symbol: string | null;
  cause: string | null;
  causeKnown: boolean;
}) {
  const label = CONCENTRATION_VERDICT_LABELS[verdict] ?? verdict;
  const note = CONCENTRATION_VERDICT_NOTES[verdict] ?? "";

  return (
    <div className={`cflag ${concentrationTone(verdict)}`}>
      <div className="cflag-head">
        <span className="cflag-badge">{label}</span>
        <span className="cflag-read">
          {symbol ? baseAsset(symbol) : DASH} {sharePct === null ? DASH : formatPct(sharePct)}
          <span className="cflag-vs">
            {" "}
            / threshold {CONCENTRATION_THRESHOLD_PCT}% ·{" "}
            {sample === null ? "no sample" : `${formatInt(sample)} trades`}
          </span>
        </span>
      </div>
      <p className="cflag-x">{note}</p>

      <div className="cflag-cause">
        <span className="cflag-cause-k">Cause</span>
        {causeKnown ? (
          <span className="cflag-cause-v">{cause}</span>
        ) : (
          <span className="cflag-cause-v is-empty">
            {DASH} — cause data is not reaching this screen. Only the per-symbol funnel can
            say whether the concentration is in the ranking or in what was allowed to
            become a candidate; until it arrives, no cause is invented here.
          </span>
        )}
      </div>

      <p className="cflag-guard">
        This flag is a diagnosis only. BTC is nowhere suppressed, penalised or re-ranked;
        if BTC wins on net EV, BTC trades. The flag is not a constraint, it is a call to
        go and look at the funnel.
      </p>
    </div>
  );
}

function CoinTable({ rows, hasFunnel }: { rows: ConcentrationRow[]; hasFunnel: boolean }) {
  return (
    <div className="tbl-wrap">
      <table className="tbl">
        <thead>
          <tr>
            <th className="l">Coin</th>
            <th>Trades</th>
            <th title="Share of the trade count">Trade share</th>
            <th title="Net P&L after costs">Net P&amp;L</th>
            <th title="Sum of the notionals of closed trades — not the instantaneous deployed capital">
              Allocated capital
            </th>
            <th title="Share of allocated capital">Capital share</th>
            <th title="Funnel: strategy_evaluated">Signals</th>
            <th title="Funnel: ranked — signals that cleared every gate">Qualified</th>
            <th title="Signals - qualified">Rejected</th>
            <th className="l" title="The stage this symbol died at, and the reason most often recorded there">
              Bottleneck
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const trades = toNum(r.trades);
            return (
              <tr key={r.symbol}>
                <td className="l sym" title={r.symbol}>
                  {baseAsset(r.symbol)}
                </td>
                <td className={trades ? "" : "dim"}>{formatInt(trades)}</td>
                <td className={toNum(r.trade_share_pct) === null ? "muted" : ""}>
                  {toNum(r.trade_share_pct) === null ? DASH : formatPct(r.trade_share_pct ?? null)}
                </td>
                <td className={`net ${tone(r.net_pnl_usd ?? null) ?? "muted"}`}>
                  {toNum(r.net_pnl_usd) === null
                    ? DASH
                    : formatSignedMicroMoney(r.net_pnl_usd ?? null)}
                </td>
                <td className={toNum(r.capital_allocated_usd) === null ? "muted" : "dim"}>
                  {formatMoney(r.capital_allocated_usd ?? null)}
                </td>
                <td className={toNum(r.capital_share_pct) === null ? "muted" : ""}>
                  {toNum(r.capital_share_pct) === null
                    ? DASH
                    : formatPct(r.capital_share_pct ?? null)}
                </td>
                <td className={toNum(r.signals) === null ? "muted" : "dim"}>
                  {formatInt(r.signals ?? null)}
                </td>
                <td className={toNum(r.qualified_signals) === null ? "muted" : ""}>
                  {formatInt(r.qualified_signals ?? null)}
                </td>
                <td className={toNum(r.rejected_signals) === null ? "muted" : "dim"}>
                  {formatInt(r.rejected_signals ?? null)}
                </td>
                <td className="l dim" title={r.bottleneck_reason ?? undefined}>
                  {r.bottleneck_stage ? (
                    <>
                      {funnelStageLabel(r.bottleneck_stage)}
                      {r.bottleneck_reason ? (
                        <span className="cbn"> · {r.bottleneck_reason}</span>
                      ) : null}
                    </>
                  ) : (
                    <span className="muted">{hasFunnel ? DASH : "no funnel"}</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function Concentration({
  concentration,
  funnel,
  trades,
  loading,
}: {
  concentration: ConcentrationState | null | undefined;
  funnel: FunnelReport | null | undefined;
  trades: TradeRow[] | null;
  loading: boolean;
}) {
  const served = concentration ?? null;
  const { rows: baseRows, derived, sample } = concentrationRows(served, trades);
  const rows = applyFunnel(baseRows, funnel?.symbols ?? null).sort(
    (a, b) => (toNum(b.trades) ?? 0) - (toNum(a.trades) ?? 0),
  );

  // The capital denominator only exists when at least one trade reported its
  // notional, so it is read from the derivation rather than assumed.
  const derivation = deriveConcentration(trades);

  const top = rows[0] ?? null;
  const tradeHead: Headline = {
    symbol: served?.top_symbol ?? top?.symbol ?? null,
    sharePct: toNum(served?.top_trade_share_pct) ?? toNum(top?.trade_share_pct ?? null),
    sample: sample ?? null,
  };
  const capTop = [...rows].sort(
    (a, b) => (toNum(b.capital_share_pct) ?? -1) - (toNum(a.capital_share_pct) ?? -1),
  )[0];
  // The capital share has its own denominator and it is smaller than the trade
  // one whenever a round trip did not report its size. Reporting the trade
  // sample beside a capital percentage would overstate what was counted.
  const capSample = derived
    ? derivation.notionalTotal === null
      ? null
      : derivation.tradeSample - derivation.notionalMissing
    : toNum(served?.sample_notional_usd) === null
      ? null
      : toNum(served?.sample_trades);
  const capHead: Headline = {
    symbol: served?.top_symbol ?? capTop?.symbol ?? null,
    sharePct: toNum(served?.top_capital_share_pct) ?? toNum(capTop?.capital_share_pct ?? null),
    sample: capSample,
  };

  const reading = concentrationReading(tradeHead.sharePct, tradeHead.sample);
  const verdict = served?.flag === true ? "flagged" : reading.verdict;

  const cause = served?.cause ?? causeFromFunnel(funnel);
  const meta = (
    <>
      <span
        className="pill is-off"
        title="Closed trades the shares were computed from — the denominator of every percentage"
      >
        {sample === null ? "no sample" : `${formatInt(sample)} trades`}
      </span>
      <span
        className={`pill ${verdict === "flagged" ? "is-warn" : "is-off"}`}
        title={CONCENTRATION_VERDICT_NOTES[verdict] ?? ""}
      >
        {verdict === "flagged" ? "FLAG ACTIVE" : "FLAG NOT ACTIVE"}
      </span>
    </>
  );

  return (
    <Panel
      idx="18"
      title="BTC concentration monitor"
      note="per-asset trade, capital and funnel counts"
      meta={meta}
      footer={
        "ALLOCATED CAPITAL is the sum of the notionals of closed trades: a $50 position that rotates ten times writes $500 into this column. It is not the same quantity as the instantaneous DEPLOYED CAPITAL in §09, and the two columns are never summed. " +
        (derived
          ? "Trade and capital shares are DERIVED from the closed-trade list (the API publishes the last 200 round trips), while the signal/qualified/rejected columns come only from the per-symbol funnel. "
          : "") +
        `The flag threshold is ${CONCENTRATION_THRESHOLD_PCT}% and the smallest sample it will speak on is ${CONCENTRATION_MIN_SAMPLE} trades; a high share across few trades is few trades, not concentration. The flag suppresses no asset.`
      }
    >
      {loading && rows.length === 0 ? (
        <Skeleton rows={5} />
      ) : rows.length === 0 ? (
        <DataState
          title="No per-asset counts"
          text="Neither closed trades nor the per-symbol funnel reached this screen. Concentration was not measured — and an unmeasured concentration is not an absent one."
        />
      ) : (
        <div className="conc">
          <div className="conc-heads">
            <ShareMeter
              label="BTC trade share"
              symbol={tradeHead.symbol}
              pct={tradeHead.sharePct}
              sample={tradeHead.sample}
              sampleUnit="closed trades"
              kind={verdict}
              note="How many round trips went to this asset. It is an output of the ranking, not a quota."
            />
            <ShareMeter
              label="BTC capital share"
              symbol={capHead.symbol}
              pct={capHead.sharePct}
              sample={capHead.sample}
              sampleUnit="trades with a reported notional"
              kind={capHead.sharePct !== null && capHead.sharePct > CONCENTRATION_THRESHOLD_PCT ? "flagged" : verdict}
              note="How much of the money passed through this asset. Differing from the trade share is not a defect but a separate reading: a few large round trips are a different exposure from many small ones."
            />
          </div>

          <Flag
            verdict={verdict}
            sharePct={tradeHead.sharePct}
            sample={tradeHead.sample}
            symbol={tradeHead.symbol}
            cause={cause}
            causeKnown={cause !== null}
          />

          {derivation.notionalMissing > 0 && (
            <p className="xnote">
              {formatInt(derivation.notionalMissing)} trades did not report a notional;
              the capital share is computed only over those that did. An unreported notional
              does not count as zero dollars, it is removed from the denominator.
            </p>
          )}

          <CoinTable rows={rows} hasFunnel={!!funnel?.symbols} />
        </div>
      )}
    </Panel>
  );
}

/**
 * The cause, read off the funnel's own wall counts.
 *
 * The wording is assembled from counts, not composed: "251 sembol
 * stuck at the `discovered` stage" is the funnel speaking. When no funnel is
 * served there is no cause, and the flag prints that instead of a theory.
 */
function causeFromFunnel(funnel: FunnelReport | null | undefined): string | null {
  const wall = funnel?.wall ?? null;
  if (!wall) return null;
  const entries = Object.entries(wall)
    .map(([stage, n]) => [stage, toNum(n)] as const)
    .filter((e): e is readonly [string, number] => e[1] !== null && e[1] > 0)
    .sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) return null;
  const [stage, n] = entries[0];
  const total = entries.reduce((s, [, v]) => s + v, 0);
  const reason = topReasonAt(funnel, stage);
  return (
    `${formatInt(n)} / ${formatInt(total)} symbols are stuck at the "${funnelStageLabel(stage)}" stage` +
    (reason ? ` — the reason most often recorded there: ${reason}.` : ".")
  );
}

function topReasonAt(funnel: FunnelReport | null | undefined, stage: string): string | null {
  const tally: Record<string, number> = {};
  for (const f of Object.values(funnel?.symbols ?? {})) {
    const bucket = f.drop_reasons?.[stage];
    if (!bucket) continue;
    for (const [reason, n] of Object.entries(bucket)) {
      const v = toNum(n);
      if (v === null) continue;
      tally[reason] = (tally[reason] ?? 0) + v;
    }
  }
  const top = Object.entries(tally).sort((a, b) => b[1] - a[1])[0];
  return top ? top[0] : null;
}
