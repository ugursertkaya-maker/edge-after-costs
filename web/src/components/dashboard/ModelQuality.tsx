import { Panel } from "@/components/ui/Panel";
import { Skeleton } from "@/components/ui/DataState";
import { Stat } from "@/components/dashboard/bits";
import {
  CALIBRATION_MAX_ABS_ERROR,
  DASH,
  calibrationMethodLabel,
  calibrationReading,
  calibrationStatusLabel,
  confidenceTone,
  formatDateTimeMs,
  formatDays,
  formatFactor,
  formatInt,
  formatMoney,
  formatNumber,
  formatPeriod,
  formatProbability,
  formatRate,
  formatScore,
  formatSignedMicroMoney,
  periodDays,
  toNum,
  tone,
} from "@/lib/format";
import type { PeriodMs, PredictiveState } from "@/lib/types";

/**
 * MODEL QUALITY — item 32.
 *
 * The three split periods are drawn as one timeline rather than as three
 * rows of dates, because the ordering is the claim being made. "Test comes
 * last and was touched once" is the difference between a measurement and a
 * fitted number, and a reader should not have to compare six dates to check
 * it — the shape on screen either shows train → validate → test left to
 * right, or it shows that the payload does not.
 */

interface Leg {
  key: "train" | "validate" | "test";
  label: string;
  note: string;
  period: PeriodMs | null | undefined;
}

/**
 * Widths are proportional to each leg's real span, with a floor so a short
 * test window is still legible. The floor is the only cosmetic decision here
 * and it only ever makes a small window bigger, never a large one smaller.
 */
const MIN_SHARE = 0.14;

function Timeline({ legs }: { legs: Leg[] }) {
  const spans = legs.map((l) => periodDays(l.period));
  const known = spans.filter((s): s is number => s !== null);
  const total = known.reduce((a, b) => a + b, 0);

  // Chronology is a claim the payload either supports or does not. Reading the
  // ends of each leg is the only way to know, so it is read rather than
  // assumed — a test window that starts before validation ends is a leak, and
  // it is the single most important thing this panel could catch.
  const ends = legs.map((l) => (l.period ? toNum(l.period[1]) : null));
  const starts = legs.map((l) => (l.period ? toNum(l.period[0]) : null));
  const ordered =
    ends[0] !== null && starts[1] !== null && ends[1] !== null && starts[2] !== null
      ? starts[1] >= ends[0] && starts[2] >= ends[1]
      : null;

  return (
    <div className="tline">
      <div className="tline-bar">
        {legs.map((l, i) => {
          const span = spans[i];
          const share =
            span === null || total <= 0
              ? 1 / legs.length
              : Math.max(MIN_SHARE, span / total);
          return (
            <div
              key={l.key}
              className={`tline-seg is-${l.key}${l.period ? "" : " is-empty"}`}
              style={{ flexGrow: share }}
              title={`${l.label} · ${formatPeriod(l.period)}`}
            >
              <span className="tline-k">{l.label}</span>
              <span className="tline-v">{formatPeriod(l.period)}</span>
              <span className="tline-n">
                {span === null ? "span not reported" : formatDays(span)}
              </span>
            </div>
          );
        })}
      </div>

      <div className="tline-legend">
        {legs.map((l) => (
          <span key={l.key} className={`tline-dot is-${l.key}`}>
            <i aria-hidden />
            {l.note}
          </span>
        ))}
      </div>

      {ordered === false && (
        <p className="tline-warn">
          The reported windows are not chronological: a validation or test window starts
          earlier than the end of the one before it. Because labels look forward as far
          as the horizon, that means leakage, and the out-of-sample figures here do not
          count as a measurement in this state.
        </p>
      )}
      {ordered === null && (
        <p className="tline-note">
          The ordering cannot be verified until all three windows are reported. The
          timeline draws only the windows that arrived; it does not guess the rest.
        </p>
      )}
      {ordered === true && (
        <p className="tline-note">
          The windows are chronological: training, validation and test in that order and
          ilerliyor. Test penceresine bir kez dokunulur — parametreler ona
          without overlap, frozen before the test window is ever looked at.
        </p>
      )}
    </div>
  );
}

export function ModelQuality({
  predictive,
  loading,
}: {
  predictive: PredictiveState | null | undefined;
  loading: boolean;
}) {
  const p = predictive ?? null;
  const cal = calibrationReading(p?.calibration);
  const c = p?.calibration ?? null;
  const oos = p?.oos ?? null;

  const err = toNum(c?.max_abs_error);
  const legs: Leg[] = [
    {
      key: "train",
      label: "TRAINING",
      note: "training — the model saw this data and was fitted to it",
      period: p?.train_period,
    },
    {
      key: "validate",
      label: "VALIDATION",
      note: "validation — the model and its thresholds were chosen here",
      period: p?.validate_period,
    },
    {
      key: "test",
      label: "TEST",
      note: "test — en sonda, bir kez dokunuldu",
      period: p?.test_period,
    },
  ];

  const meta = p ? (
    <>
      {p.model_version && <span className="pill">{p.model_version}</span>}
      <span className={`pill ${cal.usable ? "is-pos" : "is-warn"}`} title={cal.note}>
        {c?.status ? calibrationStatusLabel(c.status) : cal.text}
      </span>
      {toNum(p.trained_to_ms) !== null && (
        <span className="panel-note" title="The last bar the model was allowed to see">
          trained to {formatDateTimeMs(p.trained_to_ms)}
        </span>
      )}
    </>
  ) : null;

  return (
    <Panel
      idx="14"
      title="Model kalitesi"
      note="training/validation/test windows, calibration and the out-of-sample scorecard"
      meta={meta}
      footer="Calibration is not a detail, it is why this panel exists: if a model that says 70% does not win about 70% of its trades, its output is a number rather than a probability — and so is any expected-value calculation built on it. The out-of-sample columns come from data the frozen parameters never saw; in-sample performance promotes nothing."
    >
      {loading && !p ? (
        <Skeleton rows={4} />
      ) : !p ? (
        <div className="pred-empty">
          <div className="fin-empty-head">
            <span className="state-mark" aria-hidden />
            <p className="fin-empty-t">There is no model to measure</p>
          </div>
          <p className="fin-empty-x">
            No trained and frozen inference bundle was reported, so there is no Brier
            score, no reliability curve and no out-of-sample scorecard to measure
            either. That is not a fault, it is this phase's starting point: Phase 3
            closed with zero champions. When a model is trained, its three windows —
            training, validation and test — will appear here as a timeline, and its
            calibration alongside the limit it either cleared or failed. An unmeasured
            calibration is never shown on this screen as a passing one.
          </p>
        </div>
      ) : (
        <div className="mq">
          <Timeline legs={legs} />

          <div className="mq-cols">
            <div className="fgroup">
              <p className="fgroup-t">
                Identity <span className="fgroup-n">frozen inference bundle</span>
              </p>
              <dl className="cgrid">
                <div className="citem">
                  <dt>Model version</dt>
                  <dd className={p.model_version ? "" : "muted"}>
                    {p.model_version ?? DASH}
                  </dd>
                </div>
                <div className="citem">
                  <dt>Model</dt>
                  <dd className={p.model_name ? "" : "muted"}>{p.model_name ?? DASH}</dd>
                </div>
                <div className="citem">
                  <dt>Feature version</dt>
                  <dd className={p.feature_version ? "dim" : "muted"}>
                    {p.feature_version ?? DASH}
                  </dd>
                </div>
                <div className="citem">
                  <dt>Label key</dt>
                  <dd className={p.spec_key ? "dim" : "muted"} title={p.spec_key ?? undefined}>
                    {p.spec_key ?? DASH}
                  </dd>
                </div>
                <div className="citem">
                  <dt>End of training</dt>
                  <dd className={toNum(p.trained_to_ms) === null ? "muted" : "dim"}>
                    {formatDateTimeMs(p.trained_to_ms)}
                  </dd>
                </div>
                <div className="citem">
                  <dt>Probability threshold</dt>
                  <dd className={toNum(p.probability_threshold) === null ? "muted" : ""}>
                    {formatProbability(p.probability_threshold)}
                  </dd>
                </div>
                <div className="citem">
                  <dt>EV threshold</dt>
                  <dd className={toNum(p.ev_threshold_usd) === null ? "muted" : ""}>
                    {toNum(p.ev_threshold_usd) === null
                      ? DASH
                      : formatMoney(p.ev_threshold_usd, 4)}
                  </dd>
                </div>
              </dl>
            </div>

            <div className="fgroup">
              <p className="fgroup-t">
                Kalibrasyon{" "}
                <span className="fgroup-n">
                  {c?.method ? calibrationMethodLabel(c.method) : "method not reported"}
                </span>
              </p>

              <div className={`calverdict ${confidenceTone(cal.kind)}`}>
                <span className="calverdict-v">
                  {c?.status ? calibrationStatusLabel(c.status) : cal.text}
                </span>
                <span className="calverdict-x">{cal.note}</span>
              </div>

              <dl className="cgrid">
                <div className="citem">
                  <dt>Brier</dt>
                  <dd
                    className={toNum(c?.brier) === null ? "muted" : ""}
                    title="Squared error of the probability forecast — lower is better, 0.25 is a coin flip"
                  >
                    {formatScore(c?.brier)}
                  </dd>
                </div>
                <div className="citem">
                  <dt>Log loss</dt>
                  <dd className={toNum(c?.log_loss) === null ? "muted" : "dim"}>
                    {formatScore(c?.log_loss)}
                  </dd>
                </div>
                <div className="citem">
                  <dt>AUC</dt>
                  <dd className={toNum(c?.auc) === null ? "muted" : "dim"}>
                    {formatNumber(c?.auc, 3)}
                  </dd>
                </div>
                <div className="citem">
                  <dt>PR-AUC</dt>
                  <dd className={toNum(c?.pr_auc) === null ? "muted" : "dim"}>
                    {formatNumber(c?.pr_auc, 3)}
                  </dd>
                </div>
                <div className="citem is-wide">
                  <dt>Max reliability error</dt>
                  <dd
                    className={
                      err === null ? "muted" : err > CALIBRATION_MAX_ABS_ERROR ? "neg" : "pos"
                    }
                    title={`Promotion gate: <= ${CALIBRATION_MAX_ABS_ERROR}`}
                  >
                    {err === null
                      ? DASH
                      : `${formatRate(err, 1)} / limit ${formatRate(
                          CALIBRATION_MAX_ABS_ERROR,
                          0,
                        )}`}
                  </dd>
                </div>
              </dl>
            </div>
          </div>

          <div className="fgroup">
            <p className="fgroup-t">
              Out-of-sample scorecard{" "}
              <span className="fgroup-n">
                frozen parameters · test window · the decision is on this row
              </span>
            </p>
            {!oos ? (
              <p className="xnote">
                No out-of-sample scorecard was reported. A model without a scorecard cannot
                be promoted; these rows being empty means an unmeasured result, not a zero one.
              </p>
            ) : (
              <div className="pfolio-grid">
                <Stat label="Trades" value={formatInt(oos.trades)} note="out-of-sample round trips" />
                <Stat
                  label="Win rate"
                  value={formatRate(oos.win_rate)}
                  note="not a result on its own"
                />
                <Stat
                  label="Profit factor"
                  value={formatFactor(oos.profit_factor)}
                  cls={
                    toNum(oos.profit_factor) === null
                      ? ""
                      : (toNum(oos.profit_factor) as number) >= 1.2
                        ? "pos"
                        : (toNum(oos.profit_factor) as number) >= 1
                          ? "warn"
                          : "neg"
                  }
                  note="promotion bar 1.20"
                />
                <Stat
                  label="Expectancy"
                  value={formatSignedMicroMoney(oos.expectancy)}
                  cls={tone(oos.expectancy) ?? ""}
                  note="NET per trade"
                />
                <Stat
                  label="NET P&L"
                  value={formatSignedMicroMoney(oos.net_pnl)}
                  cls={tone(oos.net_pnl) ?? ""}
                  note="after costs"
                />
                <Stat
                  label="Max drawdown"
                  value={
                    toNum(oos.max_drawdown) === null
                      ? DASH
                      : formatMoney(-Math.abs(toNum(oos.max_drawdown) as number))
                  }
                  cls={toNum(oos.max_drawdown) ? "neg" : ""}
                  note="from the peak"
                />
              </div>
            )}
          </div>
        </div>
      )}
    </Panel>
  );
}
