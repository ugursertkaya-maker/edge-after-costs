import { Panel } from "@/components/ui/Panel";
import { DataState, Skeleton } from "@/components/ui/DataState";
import {
  DASH,
  IDLE_GROUP_LABELS,
  IDLE_GROUP_NOTES,
  IDLE_GROUP_ORDER,
  IDLE_GROUP_VERDICTS,
  IDLE_REASONS_ALL,
  IDLE_REASON_NOTES,
  formatMoney,
  formatPct,
  formatRate,
  idleGroup,
  idleReasonLabel,
  toNum,
} from "@/lib/format";
import type { IdleGroup } from "@/lib/format";
import { reconcileIdle } from "@/lib/phase6";
import type { CapitalState, IdleByReason } from "@/lib/types";

/**
 * WHY CAPITAL IS IDLE — the panel this phase is actually decided on.
 *
 * The requirement is one sentence: *if $350 is idle, say exactly why.* So the
 * reasons are reported in DOLLARS, never as percentages of a percentage —
 * "%14 of the idle 95%" is a number nobody can act on, and $47.20 is.
 *
 * Phase 6 EXTENDS this panel rather than adding a second one beside it. The
 * six Phase 5 reasons became nine — COOLDOWN, SCANNER_DELAY, CAPITAL_STATE_BUG
 * and OTHER — and the fourth group they brought with them is a defect report:
 * `SCANNER_DELAY` and `CAPITAL_STATE_BUG` say the money is idle because WE are
 * broken, which is neither a market reading nor a portfolio decision. There is
 * exactly one idle attribution on this screen, because the same idle dollar
 * explained twice in two panels is how a dashboard starts disagreeing with
 * itself.
 *
 * The reasons do not mean the same kind of thing, and the layout says so
 * before the reader has looked at a legend. A tall NO_QUALIFIED_SIGNAL bar
 * means there was nothing to buy: widening the universe is exactly the fix. A
 * tall EXECUTION_COST bar means there WERE candidates and they were not worth
 * their own round trip: widening the universe does not help and plausibly
 * hurts, because every non-Tier-0 pair costs ~20 bps against ~2 bps on
 * Tier-0. Those are opposite conclusions from the same idle balance, so they
 * are grouped, coloured and captioned as opposites.
 *
 * The payload contract guarantees the reasons sum to the idle balance. When a
 * payload arrives where they do not, the discrepancy is printed — hiding it
 * behind a rounded total would make a broken measurement look like a clean
 * one.
 */

interface Row {
  code: string;
  usd: number;
  group: IdleGroup | null;
}

/** A near-tie is not a dominant reason, and calling one a finding when the
    runner-up is within a tenth of it would be the panel over-reading. */
const TIE_SHARE = 0.1;

function groupClass(g: IdleGroup | null): string {
  return `is-${g ?? "other"}`;
}

function Dominant({ rows, basis }: { rows: Row[]; basis: number }) {
  const top = rows[0];
  const second = rows[1];
  if (!top || top.usd <= 0 || basis <= 0) return null;

  const share = top.usd / basis;
  const tie =
    second && second.usd > 0 && (top.usd - second.usd) / basis < TIE_SHARE ? second : null;

  return (
    <div className={`idom ${groupClass(top.group)}`}>
      <span className="idom-eye">Largest reason capital is idle</span>
      <div className="idom-head">
        <span className="idom-name" title={top.code}>
          {idleReasonLabel(top.code)}
        </span>
        <span className="idom-v">{formatMoney(top.usd)}</span>
        <span className="idom-p">{formatRate(share, 0)}</span>
      </div>
      <p className="idom-x">{IDLE_REASON_NOTES[top.code] ?? "No explanation is defined for this reason; the code is shown as-is."}</p>
      {top.group && <p className="idom-verdict">{IDLE_GROUP_VERDICTS[top.group]}</p>}
      {tie && (
        <p className="idom-tie">
          Dominance is not clear: <b>{idleReasonLabel(tie.code)}</b> is level with it at
          {" "}{formatMoney(tie.usd)}. With two reasons this close, drawing a single
          conclusion would say more than the measurement does.
        </p>
      )}
    </div>
  );
}

function Attribution({
  byReason,
  idleUsd,
  idlePct,
}: {
  byReason: IdleByReason;
  idleUsd: number | null;
  idlePct: number | null;
}) {
  const rows: Row[] = Object.keys(byReason)
    .map((code) => ({ code, usd: toNum(byReason[code]), group: idleGroup(code) }))
    .filter((r): r is Row => r.usd !== null)
    .sort((a, b) => b.usd - a.usd);

  const recon = reconcileIdle(byReason, idleUsd, IDLE_REASONS_ALL);
  const missing = recon.missing;

  if (rows.length === 0) {
    return (
      <DataState
        title="The attribution record arrived empty"
        text="An idle-capital attribution was reported but it contains not a single reason. That is an empty record, not a distribution — and an empty record does not mean capital is idle for no reason."
      />
    );
  }

  // The bars are drawn against what the reasons actually add up to. The
  // reported idle balance is printed next to it, and when the two disagree the
  // strip below says by how much.
  const sum = recon.sum;
  const positive = rows.reduce((a, r) => a + Math.max(0, r.usd), 0);
  const basis = positive > 0 ? positive : 0;
  // Signed so the direction survives: POSITIVE means idle cash that nobody
  // attributed to anything, which is the defect §22 exists to catch.
  const unexplained = recon.unexplained;
  const mismatch = recon.mismatch;

  const groups = IDLE_GROUP_ORDER.map((g) => ({
    group: g,
    usd: rows.filter((r) => r.group === g).reduce((a, r) => a + Math.max(0, r.usd), 0),
  }));
  const unclassified = rows
    .filter((r) => r.group === null)
    .reduce((a, r) => a + Math.max(0, r.usd), 0);

  if (basis <= 0) {
    return (
      <div className="idle">
        <div className="idle-head">
          <span className="idle-total">{formatMoney(idleUsd ?? sum)}</span>
          <span className="idle-total-k">idle capital</span>
        </div>
        <p className="xnote">
          Every reported reason is zero. This is the reading that there is no idle
          capital: the whole account is either in the market or outside this
          attribution. Zero is a measurement and is not drawn here the way a missing one is.
        </p>
      </div>
    );
  }

  return (
    <div className="idle">
      <div className="idle-head">
        <span className="idle-total">{formatMoney(idleUsd ?? sum)}</span>
        <span className="idle-total-k">
          idle capital{idlePct === null ? "" : ` · ${formatPct(idlePct)} of equity`}
        </span>
        {idleUsd === null && (
          <span className="idle-total-n" title="No total balance was reported">
            sum of the reasons
          </span>
        )}
      </div>

      {mismatch && unexplained !== null && (
        <div className="idle-gap" role="alert">
          <span className="idle-gap-tag">
            {unexplained > 0 ? "UNEXPLAINED CASH" : "OVER-ATTRIBUTED CASH"}
          </span>
          <span className="idle-gap-v">{formatMoney(Math.abs(unexplained))}</span>
          <p className="idle-gap-x">
            The contract guarantees the reasons sum to the idle balance; in this data they
            do not: the reasons total <b>{formatMoney(sum)}</b> against a reported idle balance of{" "}
            <b>{formatMoney(idleUsd)}</b>.{" "}
            {unexplained > 0 ? (
              <>
                So <b>{formatMoney(unexplained)}</b> is sitting idle and is attributed to no
                reason at all. That money is not working somewhere and nobody is saying
                why — which is exactly the one defect §22 is chasing.
              </>
            ) : (
              <>
                The reasons explain <b>{formatMoney(-unexplained)}</b> more than the reported
                idle balance. Either a dollar was written to two reasons at once or the
                balance was under-reported; either way the shares below are smaller than
                they should be.
              </>
            )}{" "}
            The bars below are drawn against the reasons' own total; the difference is
            not rounded away, because this is a measurement problem and not a display
            kusurudur.
          </p>
        </div>
      )}

      <Dominant rows={rows} basis={basis} />

      <div className="idle-stack" role="img" aria-label="Distribution of idle capital across reason groups">
        {groups.map(
          (g) =>
            g.usd > 0 && (
              <span
                key={g.group}
                className={`idle-seg ${groupClass(g.group)}`}
                style={{ flexGrow: g.usd }}
                title={`${IDLE_GROUP_LABELS[g.group]} · ${formatMoney(g.usd)} · ${formatRate(
                  g.usd / basis,
                  0,
                )}`}
              >
                {formatRate(g.usd / basis, 0)}
              </span>
            ),
        )}
        {unclassified > 0 && (
          <span
            className="idle-seg is-other"
            style={{ flexGrow: unclassified }}
            title={`Unclassified reasons · ${formatMoney(unclassified)}`}
          >
            {formatRate(unclassified / basis, 0)}
          </span>
        )}
      </div>

      <div className="idle-groups">
        {groups.map((g) => (
          <div key={g.group} className={`igrp ${groupClass(g.group)}`}>
            <div className="igrp-head">
              <i aria-hidden />
              <span className="igrp-k">{IDLE_GROUP_LABELS[g.group]}</span>
              <span className="igrp-v">{formatMoney(g.usd)}</span>
            </div>
            <p className="igrp-x">{IDLE_GROUP_NOTES[g.group]}</p>
            <p className="igrp-verdict">{IDLE_GROUP_VERDICTS[g.group]}</p>
          </div>
        ))}
        {unclassified > 0 && (
          <div className="igrp is-other">
            <div className="igrp-head">
              <i aria-hidden />
              <span className="igrp-k">UNCLASSIFIED</span>
              <span className="igrp-v">{formatMoney(unclassified)}</span>
            </div>
            <p className="igrp-x">
              The worker reported a reason code this screen does not recognise. The code
              is shown as-is; an unrecognised reason is not hidden.
            </p>
          </div>
        )}
      </div>

      <ul className="idle-list">
        {rows.map((r) => (
          <li key={r.code} className={`idle-row ${groupClass(r.group)}`}>
            <span className="idle-k" title={IDLE_REASON_NOTES[r.code] ?? r.code}>
              {idleReasonLabel(r.code)}
            </span>
            <span className="idle-bar">
              <span
                className={`idle-fill ${groupClass(r.group)}`}
                style={{ width: `${(Math.max(0, r.usd) / basis) * 100}%` }}
              />
            </span>
            <span className="idle-n">{formatMoney(r.usd)}</span>
            <span className="idle-p">{formatRate(Math.max(0, r.usd) / basis, 0)}</span>
          </li>
        ))}
      </ul>

      {missing.length > 0 && (
        <p className="idle-missing">
          Reasons not reported ({missing.length}):{" "}
          {missing.map((m) => idleReasonLabel(m)).join(" · ")}. An unreported reason does
          not count as zero dollars; it never appeared in the attribution at all, and those
          two statements are not interchangeable on this panel.
        </p>
      )}
    </div>
  );
}

export function IdleCapital({
  capital,
  loading,
}: {
  capital: CapitalState | null | undefined;
  loading: boolean;
}) {
  const c = capital ?? null;
  const byReason = c?.idle_by_reason ?? null;
  const idleUsd = toNum(c?.idle_usd);
  const idlePct = toNum(c?.idle_pct);

  const meta = byReason ? (
    <span className="pill is-off" title="Reasons reported in the attribution">
      {Object.keys(byReason).filter((k) => toNum(byReason[k]) !== null).length} reasons
      bildirildi
    </span>
  ) : null;

  return (
    <Panel
      idx="02"
      title="Why capital is idle"
      note="the idle balance, in dollars, reason by reason"
      meta={meta}
      footer="The reasons are in dollars because the decision is in dollars: you cannot trade on a percentage of a percentage. What this panel separates is a single sentence — NO QUALIFIED SIGNAL says there was nothing to buy, and widening the universe is exactly the fix; EXECUTION COST says candidates existed and could not cover their own round-trip cost, in which case widening the universe does not help and makes things worse by adding more expensive pairs. They are opposite conclusions drawn from the same idle balance. What rejected candidates went on to do is measured in §16."
    >
      {loading && !c ? (
        <Skeleton rows={5} />
      ) : !c ? (
        <div className="fin-empty">
          <div className="fin-empty-head">
            <span className="state-mark" aria-hidden />
            <p className="fin-empty-t">Idle capital has not been attributed yet</p>
          </div>
          <p className="fin-empty-x">
            The portfolio layer is not reporting an attribution to this screen. That does
            not mean capital is idle for no reason — it means no dollar has been written
            to a reason yet, and those two statements are not interchangeable here.
            When the attribution arrives, every idle dollar will be written to one of the
            nine reasons below, and which one dominates will be this phase's answer.
          </p>
          <ReasonReference />
        </div>
      ) : !byReason ? (
        <div className="fin-empty">
          <div className="fin-empty-head">
            <span className="state-mark" aria-hidden />
            <p className="fin-empty-t">Utilisation reported, attribution not reported</p>
          </div>
          <p className="fin-empty-x">
            Capital utilisation was measured{idleUsd === null ? "" : ` and ${formatMoney(idleUsd)} appears idle`}
            , but no per-reason distribution of that amount arrived. Knowing the total is
            not knowing the reason: that difference is exactly why this panel exists.
          </p>
          <ReasonReference />
        </div>
      ) : (
        <Attribution byReason={byReason} idleUsd={idleUsd} idlePct={idlePct} />
      )}
    </Panel>
  );
}

/**
 * The six reasons, printed whenever there is no attribution to show.
 *
 * Same job as the gate list in the FINALISTS panel: an empty panel only
 * means something if the vocabulary it will be filled with is visible next to
 * it. It is also the fastest way to see that these six are not interchangeable.
 */
function ReasonReference() {
  return (
    <div className="gateref">
      <p className="gateref-t">Every idle dollar is written to one of these nine reasons</p>
      <ol className="gateref-l">
        {IDLE_REASONS_ALL.map((code, i) => (
          <li key={code}>
            <span className="gateref-n">{String(i + 1).padStart(2, "0")}</span>
            <span>
              <b className={`iref ${groupClass(idleGroup(code))}`}>{idleReasonLabel(code)}</b>{" "}
              — {IDLE_REASON_NOTES[code] ?? DASH}
            </span>
          </li>
        ))}
      </ol>
      <p className="gateref-f">
        All nine say &ldquo;money is not working&rdquo;, but they ask for different things:
        NO QUALIFIED SIGNAL asks for a wider universe; EXECUTION COST and LIQUIDITY
        LIMIT ask for a cheaper venue; correlation / risk / reserve / cooldown ask for
        nothing at all — they are decisions already taken. SCANNER DELAY and
        CAPITAL-STATE DEFECT say nothing about the market; they are our own faults,
        and until they are zero they contaminate the reading of all the others.
      </p>
    </div>
  );
}
