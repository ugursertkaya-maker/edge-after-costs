import { Panel } from "@/components/ui/Panel";
import { DataState, Skeleton } from "@/components/ui/DataState";
import { Stat } from "@/components/dashboard/bits";
import {
  DASH,
  MISSED_ALARM_BPS,
  MISSED_NOISE_BPS,
  formatBps,
  formatInt,
  formatRate,
  missedVerdict,
  missedVerdictTone,
  rejectReasonLabel,
  toNum,
} from "@/lib/format";
import type { MissedAudit } from "@/lib/types";

/**
 * MISSED OPPORTUNITIES — was the refusal right?
 *
 * Every rejection the risk layer makes is a claim: *this candidate was not
 * worth taking.* This panel is the only place that claim is ever checked, by
 * replaying what the refused move went on to do. Two outcomes, and they are
 * opposite readings of the same gate:
 *
 *   MISSED_WINNER     — the refusal gave up money that was there.
 *   CORRECT_REJECTION — the refusal avoided a loss and earned its keep.
 *
 * The aggregate `missed_net_bps` is the calibration reading and its SIGN is
 * the whole answer: negative means the refusals saved money, strongly positive
 * means the gate is throwing money away. A dashboard that printed the
 * magnitude without the direction would be unreadable, so the verdict is
 * written in words next to the figure and never left to the reader's sign
 * arithmetic.
 *
 * This is POST-HOC analysis computed with future data. It cannot and must not
 * reach an entry decision — any path where it could is a leak and a defect —
 * and the strip at the top of the panel says so, in one line, so nobody reads
 * a row here as a live signal.
 */

interface ReasonRow {
  code: string;
  missed: number;
  correct: number;
}

function pairOf(value: unknown): [number | null, number | null] {
  if (!Array.isArray(value)) return [null, null];
  return [toNum(value[0] as number), toNum(value[1] as number)];
}

/** The one line that keeps this panel from being mistaken for a live signal. */
function PostHocStrip() {
  return (
    <div className="phoc" role="note">
      <span className="phoc-tag">RETROSPECTIVE</span>
      <span className="phoc-x">
        This panel measures, with subsequent data, what rejected candidates went on to do.
        It is an audit, not a signal: no row here enters a live decision, and if a code
        path exists by which it could, that path is a leak and a defect.
      </span>
    </div>
  );
}

function Split({
  missed,
  correct,
  unlabelled,
}: {
  missed: number;
  correct: number;
  unlabelled: number;
}) {
  const total = missed + correct + unlabelled;
  if (total <= 0) return null;
  return (
    <div className="mo-split" role="img" aria-label="Ratio of correct rejections to missed gains">
      {correct > 0 && (
        <span
          className="mo-seg is-correct"
          style={{ flexGrow: correct }}
          title={`Correct rejection · ${formatInt(correct)} · ${formatRate(correct / total, 0)}`}
        >
          {formatRate(correct / total, 0)}
        </span>
      )}
      {missed > 0 && (
        <span
          className="mo-seg is-missed"
          style={{ flexGrow: missed }}
          title={`Missed gain · ${formatInt(missed)} · ${formatRate(missed / total, 0)}`}
        >
          {formatRate(missed / total, 0)}
        </span>
      )}
      {unlabelled > 0 && (
        <span
          className="mo-seg is-other"
          style={{ flexGrow: unlabelled }}
          title={`Unlabelled rejection · ${formatInt(unlabelled)}`}
        >
          {formatRate(unlabelled / total, 0)}
        </span>
      )}
    </div>
  );
}

export function MissedOpportunities({
  missed,
  loading,
}: {
  missed: MissedAudit | null | undefined;
  loading: boolean;
}) {
  const m = missed ?? null;

  const bps = toNum(m?.missed_net_bps);
  const verdict = missedVerdict(bps);
  const vtone = missedVerdictTone(verdict.kind);

  const winners = toNum(m?.missed_winners);
  const correct = toNum(m?.correct_rejections);
  const examined = toNum(m?.rejections_examined);

  const judged = (winners ?? 0) + (correct ?? 0);
  const unlabelled =
    examined !== null && winners !== null && correct !== null
      ? Math.max(0, examined - judged)
      : 0;
  const correctShare =
    winners !== null && correct !== null && judged > 0 ? correct / judged : null;

  const byReason = m?.by_reason ?? null;
  const rows: ReasonRow[] = byReason
    ? Object.keys(byReason)
        .map((code) => {
          const [a, b] = pairOf(byReason[code]);
          return { code, missed: a ?? 0, correct: b ?? 0, known: a !== null || b !== null };
        })
        .filter((r) => r.known)
        .map(({ code, missed: ms, correct: cr }) => ({ code, missed: ms, correct: cr }))
        .sort((x, y) => y.missed - x.missed || y.correct - x.correct)
    : [];

  const rowMissed = rows.reduce((a, r) => a + r.missed, 0);
  const rowCorrect = rows.reduce((a, r) => a + r.correct, 0);
  const totalsDisagree =
    rows.length > 0 &&
    winners !== null &&
    correct !== null &&
    (rowMissed !== winners || rowCorrect !== correct);
  const rowMax = rows.reduce((a, r) => Math.max(a, r.missed + r.correct), 0);

  const meta = m ? (
    <>
      <span className={`pill ${verdict.kind === "saved" ? "is-pos" : verdict.kind === "noise" ? "is-off" : "is-warn"}`} title={verdict.note}>
        {verdict.text}
      </span>
      {toNum(m.windows) !== null && (
        <span className="panel-note" title="Historical windows examined">
          {formatInt(m.windows)} pencere
        </span>
      )}
      {examined !== null && (
        <span className="panel-note">{formatInt(examined)} ret incelendi</span>
      )}
    </>
  ) : null;

  return (
    <Panel
      idx="16"
      title="Missed opportunities"
      note="what rejected candidates went on to do — a retrospective audit"
      meta={meta}
      footer={`The sign of the total is the verdict: a negative total says the refused moves went negative after costs — that the gate earned money; a strongly positive total says the gate turned away edge that was genuinely there. Values near zero (+/-${MISSED_NOISE_BPS} bps, one Tier-0 round trip) are not a difference worth chasing; ${MISSED_ALARM_BPS} bps is a whole non-Tier-0 round trip. This audit measures only the gate's calibration; it is not a justification for loosening thresholds, because every loosening raises both the missed gains and the losses at once.`}
    >
      {loading && !m ? (
        <Skeleton rows={4} />
      ) : !m ? (
        <div className="fin-empty">
          <div className="fin-empty-head">
            <span className="state-mark" aria-hidden />
            <p className="fin-empty-t">The rejection audit has not run yet</p>
          </div>
          <p className="fin-empty-x">
            What rejected candidates went on to do has not been measured. This does not
            mean &ldquo;no opportunity was missed&rdquo; — it means nobody has looked, and on
            this screen an unmeasured audit is not shown as a clean one. When the audit
            runs, every rejection will fall into one of two boxes: a CORRECT REJECTION
            that saved money, and a MISSED GAIN that left it on the table.
          </p>
          <PostHocStrip />
        </div>
      ) : (
        <div className="mo">
          <PostHocStrip />

          <div className={`mo-head ${vtone}`}>
            <div className="mo-figure">
              <span className="mo-k">Total missed move</span>
              <span className={`mo-v ${bps === null ? "is-empty" : ""}`}>
                {bps === null ? DASH : `${formatBps(bps)} bps`}
              </span>
              <span className="mo-n">after costs, summed across rejected candidates</span>
            </div>
            <div className="mo-verdict">
              <span className={`mo-badge ${vtone}`}>{verdict.text}</span>
              <p className="mo-note">{verdict.note}</p>
            </div>
          </div>

          <Split missed={winners ?? 0} correct={correct ?? 0} unlabelled={unlabelled} />

          <div className="mo-legend">
            <span className="mo-dot is-correct">
              <i aria-hidden />
              Correct rejection — avoided a loss <b>{formatInt(correct)}</b>
            </span>
            <span className="mo-dot is-missed">
              <i aria-hidden />
              Missed gain — money left behind <b>{formatInt(winners)}</b>
            </span>
            {unlabelled > 0 && (
              <span className="mo-dot is-other">
                <i aria-hidden />
                Unlabelled — no verdict possible <b>{formatInt(unlabelled)}</b>
              </span>
            )}
          </div>

          <div className="pfolio-grid">
            <Stat label="Windows" value={formatInt(m.windows)} note="historical slices examined" />
            <Stat
              label="Rejections examined"
              value={formatInt(m.rejections_examined)}
              note="candidates entering the audit"
            />
            <Stat
              label="Missed gain"
              value={formatInt(m.missed_winners)}
              cls={winners === null ? "" : winners > 0 ? "warn" : ""}
              note="would have earned after costs"
            />
            <Stat
              label="Correct rejection"
              value={formatInt(m.correct_rejections)}
              cls={correct === null ? "" : correct > 0 ? "pos" : ""}
              note="was right to be rejected"
            />
            <Stat
              label="Correct-rejection rate"
              value={correctShare === null ? DASH : formatRate(correctShare, 1)}
              note="among rejections with a verdict"
            />
          </div>

          {rows.length === 0 ? (
            <DataState
              title="No breakdown by rejection reason was reported"
              text="The audit totals arrived but not which reason left money behind. A total does not say which gate is too tight — only the per-reason breakdown does."
            />
          ) : (
            <ul className="mo-list">
              <li className="mo-row is-head">
                <span className="mo-rk">Ret sebebi</span>
                <span className="mo-rb">correct · missed</span>
                <span className="mo-rn">Missed</span>
                <span className="mo-rn">Correct</span>
                <span className="mo-rp">Miss rate</span>
              </li>
              {rows.map((r) => {
                const tot = r.missed + r.correct;
                const share = tot > 0 ? r.missed / tot : null;
                const scale = rowMax > 0 ? (tot / rowMax) * 100 : 0;
                return (
                  <li key={r.code} className="mo-row">
                    <span className="mo-rk" title={r.code}>
                      {rejectReasonLabel(r.code)}
                    </span>
                    <span className="mo-rb">
                      <span className="mo-rbar" style={{ width: `${scale}%` }}>
                        {r.correct > 0 && (
                          <span
                            className="mo-rfill is-correct"
                            style={{ width: tot > 0 ? `${(r.correct / tot) * 100}%` : "0%" }}
                          />
                        )}
                        {r.missed > 0 && (
                          <span
                            className="mo-rfill is-missed"
                            style={{ width: tot > 0 ? `${(r.missed / tot) * 100}%` : "0%" }}
                          />
                        )}
                      </span>
                    </span>
                    <span className="mo-rn is-missed">{formatInt(r.missed)}</span>
                    <span className="mo-rn is-correct">{formatInt(r.correct)}</span>
                    <span className="mo-rp">{share === null ? DASH : formatRate(share, 0)}</span>
                  </li>
                );
              })}
            </ul>
          )}

          {totalsDisagree && (
            <p className="idle-mismatch" role="status">
              The per-reason breakdown does not reconcile with the totals above: the
              breakdown has <b>{formatInt(rowMissed)}</b> missed and <b>{formatInt(rowCorrect)}</b>
              correct rejections, while <b>{formatInt(winners)}</b> and <b>{formatInt(correct)}</b>{" "}
              were reported above. The panel shows both; it does not pick one and hide the
              other, because it does not know which is right.
            </p>
          )}
        </div>
      )}
    </Panel>
  );
}
