import { Panel } from "@/components/ui/Panel";
import { DataState, Skeleton } from "@/components/ui/DataState";
import { Stat } from "@/components/dashboard/bits";
import {
  DASH,
  formatDateTimeMs,
  formatDuration,
  formatFactor,
  formatInt,
  formatMoney,
  formatRelativeMs,
  formatSignedMicroMoney,
  strategyLabel,
  toNum,
  tone,
} from "@/lib/format";
import type { ChampionState } from "@/lib/types";

/**
 * How far apart two profit factors have to be before the gap is worth a word.
 * Below this the two measurements are saying the same thing with different
 * samples; above it they are disagreeing, and the disagreement is information.
 */
const PF_GAP = 0.1;

/**
 * The out-of-sample and live-paper profit factors, side by side.
 *
 * They are not two versions of one number and the panel never averages, picks
 * or reconciles them. The out-of-sample figure is what frozen parameters did
 * on recorded history; the live figure is what the same strategy does against
 * a real book, real latency and real queue position. They will disagree, and
 * the size of the disagreement is the only honest read on how much of the
 * backtest was an artefact of the backtest.
 */
function ProfitFactorGap({ oos, live }: { oos: number | null; live: number | null }) {
  const gap = oos !== null && live !== null ? live - oos : null;
  const gapCls =
    gap === null ? "is-empty" : gap < -PF_GAP ? "neg" : gap > PF_GAP ? "pos" : "flat";

  const verdict =
    gap === null
      ? "A difference cannot be read until both measurements exist. A single profit factor means nothing without saying under what conditions it was measured."
      : gap < -PF_GAP
        ? "Live paper is behind the research. The gap is the price of a cost the backtest did not model — queue position, latency, the real spread — and it should be deducted from the research result."
        : gap > PF_GAP
          ? "Live paper is ahead of the research. That is not good news but a warning: on a small sample this gap is usually luck, and the two measurements are expected to converge."
          : "The two measurements are close. This is the only direct evidence that the strategy behaves in real flow the way it did in research.";

  return (
    <div className="vs">
      <div className="vs-cell">
        <span className="vs-k">Out-of-sample PF</span>
        <span className={`vs-v ${oos === null ? "is-empty" : ""}`}>
          {formatFactor(oos)}
        </span>
        <span className="vs-n">frozen parameters · recorded history</span>
      </div>

      <div className="vs-gap">
        <span className="vs-op" aria-hidden>
          ↔
        </span>
        <span className={`vs-d ${gapCls}`}>
          {gap === null
            ? DASH
            : `${gap > 0 ? "+" : ""}${gap.toLocaleString("en-US", {
                minimumFractionDigits: 2,
                maximumFractionDigits: 2,
              })}`}
        </span>
        <span className="vs-dk">fark</span>
      </div>

      <div className="vs-cell is-live">
        <span className="vs-k">Live paper PF</span>
        <span className={`vs-v ${live === null ? "is-empty" : ""}`}>
          {formatFactor(live)}
        </span>
        <span className="vs-n">real flow · real latency</span>
      </div>

      <p className="vs-note">{verdict}</p>
    </div>
  );
}

/**
 * What is actually running in live paper.
 *
 * `null` is a legitimate and, given the gates, a likely answer — so it is said
 * in a sentence rather than left as an empty panel. Nothing here enables live
 * trading: this is the paper ledger's own champion.
 */
export function Champion({
  champion,
  loading,
  now,
}: {
  champion: ChampionState | null | undefined;
  loading: boolean;
  now: number;
}) {
  const c = champion ?? null;

  const oos = toNum(c?.oos_profit_factor);
  const live = toNum(c?.live_profit_factor);

  const meta = c ? (
    <>
      {c.version && (
        <span className="pill" title="The champion's version — a promoted strategy is versioned">
          {c.version}
        </span>
      )}
      {toNum(c.started_ms) !== null && (
        <span className="panel-note" title={formatDateTimeMs(c.started_ms)}>
          started {formatRelativeMs(c.started_ms, now)}
        </span>
      )}
    </>
  ) : null;

  return (
    <Panel
      idx="12"
      title="Current paper champion"
      note="the strategy running in the live paper engine"
      meta={meta}
      footer="The champion runs on paper; there is no real order anywhere in this panel. Out-of-sample PF is the research measurement and live paper PF is the measurement on real flow — the two diverging is expected, and that is exactly why they sit side by side."
    >
      {loading && !c ? (
        <Skeleton rows={3} />
      ) : !c ? (
        <DataState
          title="No paper champion"
          text="No strategy promoted out of research is running in the live paper engine. There is no promotion without a candidate that clears every gate; when one is promoted, its version, start time and both profit factors will appear here."
        />
      ) : (
        <div className="champ">
          <div className="champ-id">
            <span className="champ-k">Strategy</span>
            <span className="champ-v">
              {c.strategy ? strategyLabel(c.strategy) : DASH}
            </span>
            <span className="champ-n">
              {toNum(c.started_ms) !== null
                ? `since ${formatDateTimeMs(c.started_ms)} · running for ${formatDuration(
                    Math.max(0, (now - (toNum(c.started_ms) as number)) / 1000),
                  )}`
                : "start time not reported"}
            </span>
          </div>

          <ProfitFactorGap oos={oos} live={live} />

          <div className="pfolio-grid">
            <Stat
              label="Paper equity"
              value={formatMoney(c.paper_equity)}
              note="the champion's ledger"
            />
            <Stat
              label="Expectancy"
              value={formatSignedMicroMoney(c.expectancy)}
              cls={tone(c.expectancy) ?? ""}
              note="NET per trade"
            />
            <Stat
              label="Max drawdown"
              value={
                toNum(c.max_drawdown) === null
                  ? DASH
                  : formatMoney(-Math.abs(toNum(c.max_drawdown) as number))
              }
              cls={toNum(c.max_drawdown) ? "neg" : ""}
              note="from the peak"
            />
            <Stat label="Trades" value={formatInt(c.trades)} note="closed round trips" />
            <Stat
              label="Version"
              value={c.version ?? DASH}
              note="the promoted strategy version"
            />
          </div>
        </div>
      )}
    </Panel>
  );
}
