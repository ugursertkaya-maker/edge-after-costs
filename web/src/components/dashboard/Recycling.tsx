import { Panel } from "@/components/ui/Panel";
import { Skeleton } from "@/components/ui/DataState";
import { Stat } from "@/components/dashboard/bits";
import {
  DASH,
  RECYCLE_BAD_MS,
  RECYCLE_FAST_MS,
  RECYCLE_SLOW_MS,
  baseAsset,
  formatClockMs,
  formatDuration,
  formatExactMs,
  formatInt,
  formatMoney,
  formatSeconds,
  recycleTone,
  strategyLabel,
  toNum,
} from "@/lib/format";
import type { CapitalState, PortfolioState, RecycleEvent, RecyclingState } from "@/lib/types";

/**
 * CAPITAL RECYCLING CHAIN — §22.
 *
 * One question: when a position closes, how long before that money is working
 * again? For a strategy whose median hold is ~32 seconds, a 40-second gap
 * between the close and the next entry halves the account's turnover without
 * taking one extra unit of risk — the delay costs more than most of the
 * trades earn. So the chain is drawn as a chain, in order, with the two
 * intervals the brief names printed at the precision they are measured at:
 * close→rescan in MILLISECONDS, close→next-entry in SECONDS.
 *
 * The one reading this panel refuses to blur: `qualified_candidate_existed`.
 * A long gap with no qualified candidate is NO_QUALIFIED_SIGNAL — the market
 * had nothing, and the recycler is innocent. A long gap WITH a qualified
 * candidate waiting is a recycling defect. They produce the same idle balance
 * and they are opposite findings, so the chain states which one it is rather
 * than printing one duration and letting the reader guess.
 *
 * The idle-capital reasons are NOT repeated here. There is exactly one
 * attribution on this screen and it lives in §02; this panel links to it and
 * shows the balance, because the same idle dollar explained twice in two
 * places is how a dashboard starts disagreeing with itself.
 */

const STEPS: {
  key: keyof RecycleEvent;
  label: string;
  note: string;
}[] = [
  { key: "closed_ms", label: "Position closed", note: "The moment the round trip ended" },
  {
    key: "capital_released_ms",
    label: "Capital released",
    note: "The moment accounting counted the money as reusable",
  },
  {
    key: "rescan_triggered_ms",
    label: "Rescan",
    note: "The moment the scanner was triggered to look for a new candidate",
  },
  { key: "next_entry_ms", label: "Next entry", note: "The moment the money re-entered the market" },
];

/** One interval, with its own tone band. The bands are set against the hold,
    not against a round number — see `recycleTone` in `lib/format.ts`. */
function Interval({
  label,
  value,
  text,
  hint,
}: {
  label: string;
  value: number | null;
  text: string;
  hint: string;
}) {
  const cls = recycleTone(value);
  return (
    <div className={`rcy-int ${cls}`} title={hint}>
      <span className="rcy-int-k">{label}</span>
      <span className={`rcy-int-v${value === null ? " is-empty" : ""}`}>{text}</span>
      <span className="rcy-int-n">{hint}</span>
    </div>
  );
}

function Chain({ ev, now }: { ev: RecycleEvent; now: number }) {
  const closed = toNum(ev.closed_ms);
  const marks = STEPS.map((s) => ({ ...s, ms: toNum(ev[s.key] as number | null | undefined) }));

  return (
    <ol className="rcy-chain">
      {marks.map((m, i) => {
        const prev = i === 0 ? null : marks[i - 1].ms;
        const delta = m.ms !== null && prev !== null ? m.ms - prev : null;
        const pending = m.ms === null;
        return (
          <li key={String(m.key)} className={`rcy-step${pending ? " is-pending" : ""}`}>
            <span className="rcy-dot" aria-hidden />
            <span className="rcy-k">{m.label}</span>
            <span className={`rcy-v${pending ? " is-empty" : ""}`}>
              {pending ? DASH : formatClockMs(m.ms)}
            </span>
            <span className="rcy-d">
              {delta === null ? (
                i === 0 ? (
                  ""
                ) : (
                  <span className="muted">{DASH}</span>
                )
              ) : (
                `+${formatExactMs(delta)}`
              )}
            </span>
            <span className="rcy-n">{m.note}</span>
          </li>
        );
      })}
      {toNum(ev.next_entry_ms) === null && closed !== null && (
        <li className="rcy-open">
          This chain has not closed yet: <b>{formatDuration((now - closed) / 1000)}</b>
          {" "}has passed since the close with no new entry.{" "}
          {ev.qualified_candidate_existed === true
            ? "A qualified candidate DID exist when capital was released — in that case the delay is charged to the recycling mechanism, not to the market."
            : ev.qualified_candidate_existed === false
              ? "No qualified candidate existed when capital was released — here the delay is NO_QUALIFIED_SIGNAL, not a recycling defect."
              : "Whether a qualified candidate existed was not reported; without that, it cannot be said whether the delay came from the market or from the mechanism."}
        </li>
      )}
    </ol>
  );
}

export function Recycling({
  recycling,
  capital,
  portfolio,
  loading,
  now,
}: {
  recycling: RecyclingState | null | undefined;
  capital: CapitalState | null | undefined;
  portfolio: PortfolioState | null | undefined;
  loading: boolean;
  now: number;
}) {
  const r = recycling ?? null;
  const last = r?.last ?? r?.events?.[0] ?? null;

  const toRescan = toNum(last?.close_to_rescan_ms) ?? deltaOf(last?.closed_ms, last?.rescan_triggered_ms);
  const toEntryMs = toNum(last?.close_to_entry_ms) ?? deltaOf(last?.closed_ms, last?.next_entry_ms);

  // Cash is read from the recycling block when it serves one and from the
  // capital / portfolio layers when it does not. All three name the same
  // dollars, so preferring one and saying which is better than showing three.
  const cash =
    toNum(r?.available_cash_usd) ??
    toNum(capital?.idle_usd) ??
    toNum(portfolio?.available_cash) ??
    null;
  const cashSource = toNum(r?.available_cash_usd) !== null
    ? "recycling block"
    : toNum(capital?.idle_usd) !== null
      ? "§02 idle balance"
      : toNum(portfolio?.available_cash) !== null
        ? "§09 available cash"
        : null;

  const idleSeconds =
    toNum(r?.idle_seconds) ??
    (toNum(last?.capital_released_ms) !== null && toNum(last?.next_entry_ms) === null
      ? (now - (toNum(last?.capital_released_ms) as number)) / 1000
      : null);

  const qualified = toNum(r?.qualified_opportunities) ?? toNum(capital?.qualified_opportunities);
  const sample = toNum(r?.closes) ?? toNum(r?.sample) ?? (r?.events?.length ?? null);
  // Closes where a qualified candidate was waiting and the capital still sat.
  // This is the only count that separates "the market had nothing" from "we
  // failed to redeploy", so it is a headline, not a table column.
  const notTaken = toNum(r?.qualified_but_not_taken);
  const stateBug = toNum(last?.state_bug_usd);

  // `RecycleEvent.as_dict()` names these `symbol` and `next_entry_symbol`;
  // both spellings are accepted so the panel does not depend on which one the
  // worker ends up projecting.
  const closedSymbol = last?.closed_symbol ?? last?.symbol ?? null;
  const nextSymbol = last?.next_symbol ?? last?.next_entry_symbol ?? null;

  const meta = (
    <>
      <span className="pill is-off" title="Closes the medians were computed from">
        {sample === null ? "no sample" : `${formatInt(sample)} closes`}
      </span>
      <span className="pill is-pos" title="The entire recycling chain is a simulation.">
        PAPER
      </span>
    </>
  );

  return (
    <Panel
      idx="19"
      title="Capital recycling chain"
      note="close → release → rescan → next entry"
      meta={meta}
      footer={
        "CLOSE→SCAN is in milliseconds and CLOSE→ENTRY in seconds; the median ordering is read together with the accompanying close count, since a single chain is not a distribution. The REASON distribution for idle capital is not repeated on this panel: this screen has exactly one attribution and it is in §02 — explaining the same dollar separately in two places is how a dashboard contradicts itself. " +
        `The thresholds are relative to holding time: under ${formatExactMs(RECYCLE_FAST_MS)} is instantaneous, under ${formatExactMs(RECYCLE_SLOW_MS)} is acceptable, and over ${formatExactMs(RECYCLE_BAD_MS)} is a delay longer than the median holding time, costing more than most round trips earn.`
      }
    >
      {loading && !r ? (
        <Skeleton rows={5} />
      ) : (
        <div className="rcy">
          <div className="rcy-ints">
            <Interval
              label="Close → rescan"
              value={toRescan}
              text={formatExactMs(toRescan)}
              hint="Time until the scanner is asked, after the money is released"
            />
            <Interval
              label="Close → next entry"
              value={toEntryMs}
              text={toEntryMs === null ? DASH : formatSeconds(toEntryMs / 1000)}
              hint="Time until the money is back in the market"
            />
            <Interval
              label="Median close → scan"
              value={toNum(r?.median_close_to_rescan_ms)}
              text={formatExactMs(r?.median_close_to_rescan_ms ?? null)}
              hint={sample === null ? "sample not reported" : `over ${formatInt(sample)} closes`}
            />
            <Interval
              label="Median close → entry"
              value={toNum(r?.median_close_to_entry_ms)}
              text={
                toNum(r?.median_close_to_entry_ms) === null
                  ? DASH
                  : formatSeconds((toNum(r?.median_close_to_entry_ms) as number) / 1000)
              }
              hint={sample === null ? "sample not reported" : `over ${formatInt(sample)} closes`}
            />
          </div>

          <div className="pfolio-grid">
            <Stat
              label="Available cash"
              value={formatMoney(cash)}
              note={cashSource ? `kaynak: ${cashSource}` : "bildirilmedi"}
              cls={cash === null ? "flat" : undefined}
              title="Cash that could enter a new position right now"
            />
            <Stat
              label="Time idle"
              value={idleSeconds === null ? DASH : formatDuration(idleSeconds)}
              note={idleSeconds === null ? "not measured" : "since the last release"}
              cls={idleSeconds === null ? "flat" : idleSeconds > 60 ? "warn" : undefined}
            />
            <Stat
              label="Qualified opportunities"
              value={formatInt(qualified)}
              note={qualified === null ? "not reported" : "candidates clearing the gates this cycle"}
              cls={qualified === null ? "flat" : qualified > 0 ? "pos" : undefined}
              title="How many candidates had cleared every gate while capital was free"
            />
            <Stat
              label="Kapanan son tur"
              value={closedSymbol ? baseAsset(closedSymbol) : DASH}
              note={
                last?.closed_trade_id
                  ? `tur ${last.closed_trade_id.slice(0, 8)}`
                  : "no close records"
              }
              cls={closedSymbol ? undefined : "flat"}
            />
            <Stat
              label="Next qualified candidate"
              value={
                nextSymbol
                  ? baseAsset(nextSymbol)
                  : last?.qualified_candidate_existed === false
                    ? "YOKTU"
                    : DASH
              }
              note={
                last?.next_strategy
                  ? strategyLabel(last.next_strategy)
                  : last?.qualified_candidate_existed === false
                    ? "no candidate at the moment of release"
                    : "bildirilmedi"
              }
              cls={nextSymbol ? undefined : "flat"}
            />
            <Stat
              label="Qualified but not taken"
              value={formatInt(notTaken)}
              note={
                notTaken === null
                  ? "bildirilmedi"
                  : notTaken > 0
                    ? "a candidate existed at the close and capital still waited"
                    : "no such close occurred"
              }
              cls={notTaken === null ? "flat" : notTaken > 0 ? "neg" : "pos"}
              title="If this number is above zero, the delay is charged to the recycling mechanism rather than to the market"
            />
            <Stat
              label="Unreconciled cash"
              value={stateBug === null ? DASH : formatMoney(stateBug)}
              note={
                stateBug === null
                  ? "bildirilmedi"
                  : stateBug > 0
                    ? "CAPITAL_STATE_BUG — also visible in §02"
                    : "cash, exposure and equity reconcile"
              }
              cls={stateBug === null ? "flat" : stateBug > 0 ? "neg" : undefined}
              title="The amount by which cash, exposure and equity fail to reconcile. It is a defect, not a market reading."
            />
            <Stat
              label="Was a candidate available"
              value={
                last?.qualified_candidate_existed === true
                  ? "VARDI"
                  : last?.qualified_candidate_existed === false
                    ? "YOKTU"
                    : DASH
              }
              note="the one field that separates a defect from a market in a long delay"
              cls={
                last?.qualified_candidate_existed === true && toEntryMs !== null && toEntryMs > RECYCLE_BAD_MS
                  ? "neg"
                  : last?.qualified_candidate_existed === undefined ||
                      last?.qualified_candidate_existed === null
                    ? "flat"
                    : undefined
              }
            />
          </div>

          {last ? (
            <Chain ev={last} now={now} />
          ) : (
            <div className="fin-empty">
              <div className="fin-empty-head">
                <span className="state-mark" aria-hidden />
                <p className="fin-empty-t">The recycling chain is not being measured yet</p>
              </div>
              <p className="fin-empty-x">
                The portfolio layer is not reporting a single close chain to this screen. That
                does not mean capital recycles instantly — it means the time between a close and
                the next entry has never been measured, and those two statements are not used
                interchangeably here. The cash and idle-time figures above are read from where
                they can be measured; until the chain itself arrives, the close→scan delay is unknown.
              </p>
              <p className="xnote">
                Fields expected: <code>recycling.last</code> and <code>recycling.events[]</code>{" "}
                — her biri <code>closed_trade_id</code>, <code>closed_ms</code>,{" "}
                <code>capital_released_ms</code>, <code>rescan_triggered_ms</code>,{" "}
                <code>next_entry_ms</code>, <code>qualified_candidate_existed</code>,{" "}
                <code>close_to_rescan_ms</code>, <code>close_to_entry_ms</code>.
              </p>
            </div>
          )}

          {r?.events && r.events.length > 1 && (
            <div className="tbl-wrap">
              <table className="tbl">
                <thead>
                  <tr>
                    <th className="l">Kapanan tur</th>
                    <th>Close</th>
                    <th title="milliseconds">Close → scan</th>
                    <th title="seconds">Close → entry</th>
                    <th className="c">Candidate available</th>
                    <th className="l">Next entry</th>
                  </tr>
                </thead>
                <tbody>
                  {r.events.slice(0, 25).map((ev, i) => {
                    const a = toNum(ev.close_to_rescan_ms) ?? deltaOf(ev.closed_ms, ev.rescan_triggered_ms);
                    const b = toNum(ev.close_to_entry_ms) ?? deltaOf(ev.closed_ms, ev.next_entry_ms);
                    return (
                      <tr key={ev.closed_trade_id ?? `${ev.closed_ms}-${i}`}>
                        <td className="l sym" title={ev.closed_trade_id ?? undefined}>
                          {ev.closed_symbol ?? ev.symbol
                            ? baseAsset((ev.closed_symbol ?? ev.symbol) as string)
                            : DASH}
                        </td>
                        <td className="dim">{formatClockMs(ev.closed_ms ?? null)}</td>
                        <td className={a === null ? "muted" : ""}>{formatExactMs(a)}</td>
                        <td className={b === null ? "muted" : ""}>
                          {b === null ? DASH : formatSeconds(b / 1000)}
                        </td>
                        <td className="c">
                          {ev.qualified_candidate_existed === true ? (
                            <span className="warn">VARDI</span>
                          ) : ev.qualified_candidate_existed === false ? (
                            <span className="dim">YOKTU</span>
                          ) : (
                            <span className="muted">{DASH}</span>
                          )}
                        </td>
                        <td className="l dim">
                          {ev.next_symbol ?? ev.next_entry_symbol
                            ? baseAsset((ev.next_symbol ?? ev.next_entry_symbol) as string)
                            : DASH}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}

function deltaOf(a: number | null | undefined, b: number | null | undefined): number | null {
  const x = toNum(a);
  const y = toNum(b);
  if (x === null || y === null || y < x) return null;
  return y - x;
}
