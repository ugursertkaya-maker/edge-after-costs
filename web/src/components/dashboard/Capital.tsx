import { Panel } from "@/components/ui/Panel";
import { DataState, Skeleton } from "@/components/ui/DataState";
import { Stat } from "@/components/dashboard/bits";
import {
  DASH,
  DEPLOY_BAND,
  DEPLOY_CEILING,
  formatDuration,
  formatInt,
  formatMoney,
  formatPct,
  formatPoints,
  formatRatio,
  formatSignedMicroMoney,
  toNum,
  tone,
} from "@/lib/format";
import type { CapitalState } from "@/lib/types";

/**
 * CAPITAL UTILISATION — the headline panel.
 *
 * Phase 3 measured a peak of two concurrent positions with 99.5% of capital
 * idle, and that reading was buried inside a portfolio table nobody scrolls
 * to. So this panel exists to make one comparison unmissable: how much of the
 * account is at work, against how much was supposed to be.
 *
 * The comparison is drawn as a single proportional bar rather than as two
 * percentages side by side, because "we are at 4% of an 80% target" is a
 * subtraction the reader should not have to perform. The target band is on the
 * same track as the fill; the distance between them IS the finding.
 *
 * Every figure is optional. An absent measurement is an em dash and an absent
 * panel says it has not been measured — a screen that drew an empty bar would
 * be claiming the account is 100% idle, which is a different statement.
 */

const [BAND_LO, BAND_HI] = DEPLOY_BAND;
const TICKS = [0, 25, 50, 75, 100];

function clampPct(v: number): number {
  return Math.max(0, Math.min(100, v));
}

type DeployKind = "empty" | "under" | "in" | "over" | "full";

function deployKind(pct: number | null): DeployKind {
  if (pct === null) return "empty";
  if (pct >= DEPLOY_CEILING) return "full";
  if (pct > BAND_HI) return "over";
  if (pct < BAND_LO) return "under";
  return "in";
}

const KIND_CLASS: Record<DeployKind, string> = {
  empty: "is-empty",
  under: "is-under",
  in: "is-in",
  over: "is-over",
  full: "is-full",
};

const KIND_TEXT: Record<DeployKind, string> = {
  empty: "Utilisation was not measured.",
  under: `Utilisation is below the target band (${BAND_LO}-${BAND_HI}%): most of the capital is not working.`,
  in: `Utilisation is inside the target band (${BAND_LO}-${BAND_HI}%).`,
  over: `Utilisation is above the target band: the cash buffer has thinned and there may be no room left for a new candidate.`,
  full: `Almost all the capital is in the market. At this point the subject is exposure risk, not rotation.`,
};

/**
 * The deployment meter.
 *
 * The band is drawn on the track itself and not in a legend: a target that
 * lives in a caption is a target the eye never compares against.
 */
function Meter({
  deployedPct,
  idlePct,
  targetPct,
  kind,
}: {
  deployedPct: number | null;
  idlePct: number | null;
  targetPct: number | null;
  kind: DeployKind;
}) {
  const fill = deployedPct === null ? 0 : clampPct(deployedPct);
  const label =
    deployedPct === null
      ? "Utilisation was not measured"
      : `${formatPct(deployedPct)} of capital is deployed, ${
          idlePct === null ? "the rest" : formatPct(idlePct)
        } is idle`;

  return (
    <div className={`cap-meter ${KIND_CLASS[kind]}`}>
      <div className="cap-legend">
        <span className="cap-leg is-dep">
          <i aria-hidden />
          Deployed <b>{deployedPct === null ? DASH : formatPct(deployedPct)}</b>
        </span>
        <span className="cap-leg is-idle">
          <i aria-hidden />
          Idle <b>{idlePct === null ? DASH : formatPct(idlePct)}</b>
        </span>
        <span className="cap-leg is-band">
          <i aria-hidden />
          Target band <b>{BAND_LO}-{BAND_HI}%</b>
        </span>
        {targetPct !== null && (
          <span className="cap-leg is-target">
            <i aria-hidden />
            Hedef <b>{formatPct(targetPct)}</b>
          </span>
        )}
      </div>

      <div className="cap-track" role="img" aria-label={label}>
        <span
          className="cap-band"
          style={{ left: `${BAND_LO}%`, width: `${BAND_HI - BAND_LO}%` }}
          title={`Phase 5 target band: ${BAND_LO}-${BAND_HI}% of capital in the market`}
        />
        <span
          className={`cap-fill ${deployedPct === null ? "is-unknown" : ""}`}
          style={{ width: `${fill}%` }}
        />
        {targetPct !== null && (
          <span
            className="cap-target"
            style={{ left: `${clampPct(targetPct)}%` }}
            title={`Bildirilen hedef: ${formatPct(targetPct)}`}
          />
        )}
      </div>

      <div className="cap-scale" aria-hidden>
        {TICKS.map((t) => (
          <span
            key={t}
            className={`cap-tick${t === 0 ? " is-first" : t === 100 ? " is-last" : ""}`}
            style={{ left: `${t}%` }}
          >
            %{t}
          </span>
        ))}
      </div>
    </div>
  );
}

export function Capital({
  capital,
  loading,
}: {
  capital: CapitalState | null | undefined;
  loading: boolean;
}) {
  const c = capital ?? null;

  const equity = toNum(c?.equity);
  const deployedUsd = toNum(c?.deployed_usd);
  const idleUsd = toNum(c?.idle_usd);

  // A percentage the worker did not send is derived from the dollars when both
  // of those are present — and marked as derived, because a computed reading
  // and a reported one are not the same evidence. Nothing is derived from
  // nothing: with no equity there is no denominator and the cell stays an em
  // dash.
  const reportedDeployedPct = toNum(c?.deployed_pct);
  const reportedIdlePct = toNum(c?.idle_pct);
  const canDerive = equity !== null && equity > 0;
  const deployedPct =
    reportedDeployedPct ??
    (canDerive && deployedUsd !== null ? (deployedUsd / equity) * 100 : null);
  const idlePct =
    reportedIdlePct ?? (canDerive && idleUsd !== null ? (idleUsd / equity) * 100 : null);
  const derived =
    (reportedDeployedPct === null && deployedPct !== null) ||
    (reportedIdlePct === null && idlePct !== null);

  const targetPct = toNum(c?.target_pct);
  const kind = deployKind(deployedPct);

  // The contract says deployed + idle is the equity. When it does not, the
  // panel says so rather than quietly drawing a bar that adds up.
  const identityGap =
    equity !== null && deployedUsd !== null && idleUsd !== null
      ? deployedUsd + idleUsd - equity
      : null;
  const identityBroken = identityGap !== null && Math.abs(identityGap) > 0.005;

  const gapPoints =
    targetPct !== null && deployedPct !== null ? targetPct - deployedPct : null;
  const gapUsd =
    gapPoints !== null && equity !== null ? (gapPoints / 100) * equity : null;
  const shareOfTarget =
    targetPct !== null && targetPct > 0 && deployedPct !== null
      ? (deployedPct / targetPct) * 100
      : null;

  const meta = c ? (
    <>
      <span
        className={`pill ${
          kind === "in" ? "is-pos" : kind === "full" ? "is-neg" : kind === "empty" ? "is-off" : "is-warn"
        }`}
        title="Share of capital working in the market"
      >
        {deployedPct === null ? "utilisation not measured" : `${formatPct(deployedPct)} deployed`}
      </span>
      {targetPct !== null && (
        <span className="panel-note" title="The utilisation this run targets">
          hedef {formatPct(targetPct)}
        </span>
      )}
      {toNum(c.open_positions) !== null && (
        <span className="panel-note">{formatInt(c.open_positions)} open positions</span>
      )}
    </>
  ) : null;

  return (
    <Panel
      idx="01"
      title="Capital utilisation"
      note="how much is working, how much is idle, and where the target sits"
      meta={meta}
      footer="Utilisation is not a performance measure: idle money does not lose, it merely does not earn. This panel shows the ratio alongside the target because the real question is this — is capital idle because no trade could be produced, or because the trades produced could not cover their own cost? The answer is in §02. Thresholds are not lowered to push utilisation towards the target; a trade with negative expected value is still NO TRADE."
    >
      {loading && !c ? (
        <Skeleton rows={5} />
      ) : !c ? (
        <DataState
          title="Capital utilisation has not been measured yet"
          text="The worker is not reporting utilisation fields to this screen. That does not mean all the capital is idle — it means it was never measured, and this panel does not draw an unmeasured ratio as zero. Once the portfolio layer starts producing the idle-capital attribution, deployed notional, idle notional and the target band will appear here on a single bar."
        />
      ) : (
        <div className="cap">
          <div className="cap-heads">
            <div className="cap-cell">
              <span className="cap-k">Equity</span>
              <span className={`cap-v ${equity === null ? "is-empty" : ""}`}>
                {formatMoney(equity)}
              </span>
              <span className="cap-n">the whole account</span>
            </div>

            <div className="cap-cell is-dep">
              <span className="cap-k">Deployed capital</span>
              <span className={`cap-v ${deployedUsd === null ? "is-empty" : "pos"}`}>
                {formatMoney(deployedUsd)}
              </span>
              <span className="cap-n">
                {deployedPct === null
                  ? "notional at work in the market"
                  : `${formatPct(deployedPct)} · notional at work in the market`}
              </span>
            </div>

            <div className={`cap-cell is-idle ${KIND_CLASS[kind]}`}>
              <span className="cap-k">Idle capital</span>
              <span className={`cap-v ${idleUsd === null ? "is-empty" : ""}`}>
                {formatMoney(idleUsd)}
              </span>
              <span className="cap-n">
                {idlePct === null
                  ? "in no position at all"
                  : `${formatPct(idlePct)} · in no position at all`}
              </span>
            </div>
          </div>

          <Meter
            deployedPct={deployedPct}
            idlePct={idlePct}
            targetPct={targetPct}
            kind={kind}
          />

          <p className={`cap-read ${KIND_CLASS[kind]}`}>
            {KIND_TEXT[kind]}{" "}
            {targetPct === null
              ? `No target was reported for this run; the shading on the bar is Phase 5's ${BAND_LO}-${BAND_HI}% band.`
              : shareOfTarget === null
                ? `Hedef ${formatPct(targetPct)}.`
                : `Target ${formatPct(targetPct)}, measured ${formatPct(
                    deployedPct as number,
                  )} — that is ${formatPct(shareOfTarget, 0)} of target. The ${formatPoints(
                    gapPoints,
                  )}${gapUsd === null ? "" : `, ${formatMoney(Math.abs(gapUsd))} demektir`}.`}
          </p>

          {identityBroken && (
            <p className="cap-warn">
              Inconsistency: deployed {formatMoney(deployedUsd)} plus idle{" "}
              {formatMoney(idleUsd)} does not equal the reported {formatMoney(equity)} of
              equity — a gap of {formatMoney(identityGap)}. The panel does not round this
              away; the bar is drawn from the reported ratios.
            </p>
          )}

          <div className="pfolio-grid">
            <Stat
              label="Open positions"
              value={formatInt(c.open_positions)}
              note="adet · hedef 8–15"
            />
            <Stat
              label="Qualified opportunities"
              value={formatInt(c.qualified_opportunities)}
              note="candidates that cleared the gates"
            />
            <Stat
              label="Rejected opportunities"
              value={formatInt(c.rejected_opportunities)}
              note="candidates eliminated at the gates"
            />
            <Stat
              label="Daily capital turnover"
              value={formatRatio(c.turnover_per_day)}
              note="traded notional / equity"
            />
            <Stat
              label="Avg. position"
              value={formatMoney(c.avg_position_usd)}
              note="hedef $10–50"
            />
            <Stat
              label="Avg. holding time"
              value={formatDuration(c.avg_holding_seconds)}
              note="per position"
            />
            <Stat
              label="NET P&L today"
              value={formatSignedMicroMoney(c.net_pnl_today)}
              cls={tone(c.net_pnl_today) ?? ""}
              note="costs deducted"
            />
            <Stat
              label="NET per $100 deployed"
              value={formatSignedMicroMoney(c.net_pnl_per_100_deployed)}
              cls={tone(c.net_pnl_per_100_deployed) ?? ""}
              note="yield on working capital"
              title="Idle money does not distort this ratio; it is measured only over capital actually put into the market."
            />
          </div>

          {derived && (
            <p className="cap-note">
              Utilisation ratios that were not reported have been derived from the
              notionals (deployed ÷ equity). A derived ratio is not the same evidence as
              a measured one; this line disappears once the worker reports the ratios.
            </p>
          )}
        </div>
      )}
    </Panel>
  );
}
