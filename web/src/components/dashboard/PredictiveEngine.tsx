import { Panel } from "@/components/ui/Panel";
import { DataState, Skeleton } from "@/components/ui/DataState";
import {
  CALIBRATION_MAX_ABS_ERROR,
  DASH,
  baseAsset,
  calibrationReading,
  confidenceReading,
  confidenceTone,
  decisionLabel,
  formatMicroMoney,
  formatMoney,
  formatInt,
  formatProbability,
  formatSignedMicroMoney,
  predictRejectLabel,
  regimeLabel,
  toNum,
  tone,
} from "@/lib/format";
import type { ConfidenceReading } from "@/lib/format";
import type {
  PredictiveCalibration,
  PredictiveCandidate,
  PredictiveState,
} from "@/lib/types";

/**
 * PREDICTIVE ENTRY ENGINE — item 31.
 *
 * The panel exists to answer one question per candidate: given this market
 * state, what is the expected value after costs, and is it positive enough to
 * act on. Everything is laid out around the EXPECTED VALUE column for that
 * reason — the probabilities to its left are inputs, the cost to its right is
 * the toll, and the decision on the far right is the consequence.
 *
 * The rule that shapes the whole design: **an uncalibrated model's output is
 * a number, not a probability.** When the calibration block has not been
 * measured, or was measured and failed, the probability columns are hatched
 * exactly as the in-sample columns are in the tournament, the confidence cell
 * prints the reason instead of a figure, and a strip above the table says so
 * in words. A percentage is the most authoritative-looking thing this screen
 * can print, and it is not printed on trust.
 */

type Group = "pid" | "prob" | "ev" | "dec";

const GROUPS: { group: Group; label: string; note: string }[] = [
  { group: "pid", label: "Candidate", note: "asset · regime · the version that produced the model" },
  {
    group: "prob",
    label: "Model probabilities",
    note: "probability of the three outcomes — only a probability if calibration was measured and passed",
  },
  {
    group: "ev",
    label: "Expectancy after costs",
    note: "win and loss branches, expected value and execution cost — all NET",
  },
  { group: "dec", label: "Verdict", note: "confidence and decision" },
];

function cellTone(v: number | null): string {
  if (v === null) return "muted";
  return tone(v) ?? "flat";
}

/**
 * The calibration strip.
 *
 * It is drawn above the table rather than inside a tooltip because it governs
 * how every figure below it should be read. When the model is trusted the
 * strip still appears — a screen that only warns when something is wrong
 * gives the operator no way to tell "fine" from "not checked".
 */
function CalibrationStrip({ reading }: { reading: ConfidenceReading }) {
  return (
    <div className={`calstrip ${confidenceTone(reading.kind)}`} role="status">
      <span className="calstrip-tag">{reading.text}</span>
      <span className="calstrip-x">{reading.note}</span>
    </div>
  );
}

/**
 * The engine's own decision rule, printed whenever there is nothing to judge.
 *
 * Same job as the gate list in the FINALISTS panel: an empty engine panel
 * only means something if the bar nothing cleared is visible next to it. The
 * two thresholds are read from the payload when the worker sends them and
 * said to be unreported when it does not — they are never assumed.
 */
function EngineRule({ p }: { p: PredictiveState | null | undefined }) {
  const pt = toNum(p?.probability_threshold);
  const ev = toNum(p?.ev_threshold_usd);

  const rules: { n: number; text: string }[] = [
    {
      n: 1,
      text:
        pt === null
          ? "Win probability must be above an unreported probability threshold"
          : `Win probability >= ${formatProbability(pt)} — the model's frozen probability threshold`,
    },
    {
      n: 2,
      text:
        ev === null
          ? "Expected value must be above an unreported USD threshold"
          : `Expected value > ${formatMicroMoney(ev)} — after costs, per trade`,
    },
    {
      n: 3,
      text: `Calibration measured, with a maximum absolute reliability error <= ${CALIBRATION_MAX_ABS_ERROR.toLocaleString(
        "en-US",
        { minimumFractionDigits: 2 },
      )}`,
    },
    {
      n: 4,
      text: "Execution cost must come out of the win branch — a NET target, not a gross one",
    },
    {
      n: 5,
      text: "Regime inside the model's training scope, spread and depth acceptable, data fresh",
    },
  ];

  return (
    <div className="gateref">
      <p className="gateref-t">For a candidate to become a BUY — all of them at once</p>
      <ol className="gateref-l">
        {rules.map((r) => (
          <li key={r.n}>
            <span className="gateref-n">{String(r.n).padStart(2, "0")}</span>
            <span>{r.text}</span>
          </li>
        ))}
      </ol>
      <p className="gateref-f">
        Expected value decides, not win rate: a high-hit-rate setup that pays badly is a
        losing setup. And a percentage produced by an uncalibrated model is a number,
        not a probability — this panel does not display it as one.
      </p>
    </div>
  );
}

function decisionClass(code: string | null | undefined): string {
  switch (code) {
    case "BUY":
    case "ENTER":
    case "OPEN":
    case "ACCEPT":
      return "buy";
    case "SELL":
      return "sell";
    case "WATCH":
      return "watch";
    default:
      return "none";
  }
}

function Row({
  c,
  calibration,
}: {
  c: PredictiveCandidate;
  calibration: PredictiveCalibration | null | undefined;
}) {
  // `confidenceReading` consults the model's calibration first and only then
  // the candidate's own figure, so an uncalibrated model can never reach the
  // branch that prints a percentage.
  const reading: ConfidenceReading = confidenceReading(
    c.confidence,
    c.confidence_known,
    calibration,
  );

  const evValue = toNum(c.expected_value);
  const loss = toNum(c.expected_net_loss);
  const cost = toNum(c.execution_cost);

  return (
    <tr>
      <td className={`l stick c-pid ${c.symbol ? "sym" : "muted"}`} title={c.symbol ?? undefined}>
        {c.symbol ? baseAsset(c.symbol) : DASH}
      </td>
      <td className="l c-pid">
        <span className="tag" title={c.regime ?? undefined}>
          {regimeLabel(c.regime)}
        </span>
      </td>
      <td className="l c-pid dim" title={c.model ?? undefined}>
        {c.model ?? DASH}
      </td>

      <td className="c-prob" title="P(take-profit barrier first)">
        {formatProbability(c.p_tp)}
      </td>
      <td className="c-prob" title="P(stop barrier first)">
        {formatProbability(c.p_sl)}
      </td>
      <td className="c-prob" title="P(neither barrier touched inside the horizon)">
        {formatProbability(c.p_timeout)}
      </td>

      <td
        className={`c-ev ${toNum(c.expected_net_win) === null ? "muted" : "pos"}`}
        title="Modelled NET return of the win branch"
      >
        {formatMicroMoney(c.expected_net_win)}
      </td>
      <td
        className={`c-ev ${loss === null ? "muted" : "neg"}`}
        title="Modelled NET cost of the loss branch"
      >
        {loss === null ? DASH : formatMicroMoney(-Math.abs(loss))}
      </td>
      <td
        className={`c-ev is-head ${cellTone(evValue)}`}
        title="Expected value: probability-weighted win - loss - cost"
      >
        {formatSignedMicroMoney(evValue)}
      </td>
      <td
        className={`c-ev ${cost === null ? "muted" : "neg"}`}
        title="This candidate's share of fees, spread and slippage"
      >
        {cost === null ? DASH : formatMicroMoney(-Math.abs(cost))}
      </td>

      <td className="c-dec">
        <span className={`conf ${confidenceTone(reading.kind)}`} title={reading.note}>
          {reading.text}
        </span>
      </td>
      <td className="c-dec l">
        <span
          className={`decision ${decisionClass(c.decision)}`}
          title={c.decision ?? undefined}
        >
          {decisionLabel(c.decision)}
        </span>
        {c.reject_reason && (
          <span className="pred-why" title={String(c.reject_reason)}>
            {predictRejectLabel(c.reject_reason)}
          </span>
        )}
      </td>
    </tr>
  );
}

export function PredictiveEngine({
  predictive,
  loading,
}: {
  predictive: PredictiveState | null | undefined;
  loading: boolean;
}) {
  const p = predictive ?? null;
  const cal = calibrationReading(p?.calibration);
  const rows = p?.candidates ?? null;

  const buys = rows?.filter((c) => decisionClass(c.decision) === "buy").length ?? 0;

  const meta = p ? (
    <>
      {p.model_version && (
        <span className="pill" title="Version of the inference bundle">
          {p.model_version}
        </span>
      )}
      <span className={`pill ${cal.usable ? "is-pos" : "is-warn"}`} title={cal.note}>
        {cal.text}
      </span>
      {toNum(p.probability_threshold) !== null && (
        <span className="panel-note" title="Frozen probability threshold">
          probability threshold {formatProbability(p.probability_threshold)}
        </span>
      )}
      {toNum(p.ev_threshold_usd) !== null && (
        <span className="panel-note" title="Minimum expected value per trade">
          EV threshold {formatMicroMoney(p.ev_threshold_usd)}
        </span>
      )}
      {rows && (
        <span className="panel-note">
          {formatInt(rows.length)} candidates · {formatInt(buys)} BUY
        </span>
      )}
    </>
  ) : null;

  return (
    <Panel
      idx="13"
      title="Predictive entry engine"
      note="per-candidate win probability, expected value after costs, and the decision"
      meta={meta}
      flush
      footer="The decision is made on expected value, not win rate: a high-hit-rate setup that pays badly is a losing setup. The probability columns are drawn solid only when calibration was measured and cleared the limit; on an unmeasured or failing model they are hatched, and the confidence cell states the reason instead of a percentage."
    >
      {loading && !p ? (
        <Skeleton rows={6} />
      ) : !p ? (
        <div className="pred-empty">
          <div className="fin-empty-head">
            <span className="state-mark" aria-hidden />
            <p className="fin-empty-t">The predictive engine is not live yet</p>
          </div>
          <p className="fin-empty-x">
            Worker bu ekrana bir model bildirmiyor. Bu, &ldquo;model kenar
            find an edge&rdquo; — it means there is no promoted model yet, and those two
            statements are not interchangeable on this panel.
            Phase 3 closed with zero champions; so never having a model here is also a
            possible and acceptable outcome. Once an inference bundle is trained and
            frozen, every candidate will be put through the conditions below one by one
            and the result will appear in this table.
          </p>
          <EngineRule p={null} />
        </div>
      ) : !rows ? (
        <DataState
          title="No candidate list was reported"
          text="A model record arrived but it has no evaluated-candidate field. That is a missing output, not zero candidates — an unmeasured result is not shown as zero on this screen."
        />
      ) : rows.length === 0 ? (
        <div className="pred-empty">
          <div className="fin-empty-head">
            <span className="fin-empty-mark" aria-hidden />
            <p className="fin-empty-t">No candidate was produced this cycle</p>
          </div>
          <p className="fin-empty-x">
            The model ran and found not a single candidate to evaluate. An empty candidate
            list and a candidate list that was entirely rejected are different readings: the
            first says no setup in the universe cleared pre-selection, the second says setups
            did clear it and were eliminated on expected value. Which one happened is visible
            in the rejection distribution in §15.
          </p>
          <EngineRule p={p} />
        </div>
      ) : (
        <>
          <CalibrationStrip reading={cal} />
          <div className="tbl-wrap">
            <table className={`tbl tbl-pred${cal.usable ? "" : " is-uncal"}`}>
              <thead>
                <tr className="grp">
                  {GROUPS.map((g) => {
                    const span =
                      g.group === "pid" ? 3 : g.group === "prob" ? 3 : g.group === "ev" ? 4 : 2;
                    return (
                      <th key={g.group} className={`gh g-${g.group}`} colSpan={span} title={g.note}>
                        {g.label}
                        {g.group === "prob" && !cal.usable && (
                          <span className="gh-warn" title={cal.note}>
                            {" "}
                            · {cal.text} — a number, not a probability
                          </span>
                        )}
                      </th>
                    );
                  })}
                </tr>
                <tr className="cols">
                  <th className="l stick c-pid">Coin</th>
                  <th className="l c-pid">Regime</th>
                  <th className="l c-pid">Model</th>
                  <th className="c-prob" title="Probability of touching the take-profit barrier first">
                    P(TP first)
                  </th>
                  <th className="c-prob" title="Probability of touching the stop barrier first">
                    P(SL first)
                  </th>
                  <th className="c-prob" title="Probability of touching neither barrier inside the horizon">
                    P(timeout)
                  </th>
                  <th className="c-ev">Expected net win</th>
                  <th className="c-ev">Expected net loss</th>
                  <th className="c-ev is-head">Expected value</th>
                  <th className="c-ev">Execution cost</th>
                  <th className="c-dec">Confidence</th>
                  <th className="c-dec l">Karar</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((c, i) => (
                  <Row
                    key={`${c.symbol ?? "?"}-${c.model ?? "?"}-${i}`}
                    c={c}
                    calibration={p.calibration}
                  />
                ))}
              </tbody>
            </table>
          </div>
          {toNum(p.ev_threshold_usd) !== null && (
            <p className="pred-foot">
              Thresholds in force: probability {formatProbability(p.probability_threshold)} ·
              expected value {formatMoney(p.ev_threshold_usd, 4)} · max reliability error{" "}
              {CALIBRATION_MAX_ABS_ERROR.toLocaleString("en-US", { minimumFractionDigits: 2 })}
            </p>
          )}
        </>
      )}
    </Panel>
  );
}
