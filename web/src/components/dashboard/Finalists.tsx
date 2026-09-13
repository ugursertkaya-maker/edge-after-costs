import { Panel } from "@/components/ui/Panel";
import { Skeleton } from "@/components/ui/DataState";
import {
  DASH,
  baseAsset,
  familyLabel,
  formatHorizon,
  formatInt,
  formatMoney,
  formatRate,
  formatSignedMicroMoney,
  gateVerdict,
  regimeLabel,
  toNum,
  tone,
} from "@/lib/format";
import type { FinalistRow, MonteCarloResult, StressResult } from "@/lib/types";

/**
 * The promotion gates, verbatim from the Phase 3 brief.
 *
 * They are rendered whether or not anything passed them, because the list is
 * the standard — and an empty finalists panel only means something if the bar
 * that nothing cleared is visible next to it. They are also the reason this
 * panel exists at all: the gates are not tuned to produce a winner, and a
 * screen that quietly dropped one of them would be indistinguishable from a
 * screen that never had it.
 */
const GATES: { n: number; text: string }[] = [
  { n: 1, text: "Out-of-sample NET expectancy above zero" },
  {
    n: 2,
    text: "Profit factor >= 1.20 — between 1.00 and 1.20 is MARGINAL, not a finalist",
  },
  { n: 3, text: "Max drawdown reasonable against a $500 account" },
  {
    n: 4,
    text: "Not dependent on a single trade — expectancy stays positive with the best trade removed",
  },
  {
    n: 5,
    text: "Positive across multiple test windows — a majority of the walk-forward windows",
  },
  {
    n: 6,
    text: "Survives cost stress — fees +25% and +50%, slippage x2, latency x2, worse spread, random order delay",
  },
  { n: 7, text: "Minimum sample: at least 30 out-of-sample trades" },
];

function GateReference() {
  return (
    <div className="gateref">
      <p className="gateref-t">Gates in force — out-of-sample, all of them at once</p>
      <ol className="gateref-l">
        {GATES.map((g) => (
          <li key={g.n}>
            <span className="gateref-n">{String(g.n).padStart(2, "0")}</span>
            <span>{g.text}</span>
          </li>
        ))}
      </ol>
      <p className="gateref-f">
        These thresholds are not lowered to find a winner. A strategy that passes a
        lowered gate produces an invented history, not a profit.
      </p>
    </div>
  );
}

function Stress({ items }: { items: StressResult[] }) {
  return (
    <div className="stress">
      <p className="fgroup-t">Cost stress</p>
      <div className="stress-list">
        {items.map((s, i) => {
          const survived = s.survived;
          const cls =
            survived === true ? "is-pass" : survived === false ? "is-fail" : "is-unknown";
          return (
            <span
              key={`${s.label ?? "stres"}-${i}`}
              className={`schip ${cls}`}
              title={
                survived === true
                  ? "Survived this scenario"
                  : survived === false
                    ? "Failed under this scenario"
                    : "No verdict reported for this scenario"
              }
            >
              <span className="schip-k">{s.label ?? DASH}</span>
              <span className={`schip-v ${tone(s.net_pnl) ?? "muted"}`}>
                {formatSignedMicroMoney(s.net_pnl)}
              </span>
            </span>
          );
        })}
      </div>
    </div>
  );
}

function MonteCarlo({ mc }: { mc: MonteCarloResult }) {
  const prob = toNum(mc.prob_below_start);
  return (
    <div className="mc">
      <p className="fgroup-t">
        Monte Carlo{" "}
        {toNum(mc.samples) !== null && (
          <span className="fgroup-n">{formatInt(mc.samples)} samples</span>
        )}
      </p>
      <dl className="cgrid">
        <div className="citem">
          <dt>Medyan drawdown</dt>
          <dd className={toNum(mc.median_drawdown) === null ? "muted" : "neg"}>
            {toNum(mc.median_drawdown) === null
              ? DASH
              : formatMoney(-Math.abs(toNum(mc.median_drawdown) as number))}
          </dd>
        </div>
        <div className="citem">
          <dt>%95 drawdown</dt>
          <dd className={toNum(mc.p95_drawdown) === null ? "muted" : "neg"}>
            {toNum(mc.p95_drawdown) === null
              ? DASH
              : formatMoney(-Math.abs(toNum(mc.p95_drawdown) as number))}
          </dd>
        </div>
        <div className="citem">
          <dt>Longest losing streak</dt>
          <dd className={toNum(mc.worst_loss_streak) === null ? "muted" : ""}>
            {toNum(mc.worst_loss_streak) === null
              ? DASH
              : `${formatInt(mc.worst_loss_streak)} trades`}
          </dd>
        </div>
        <div className="citem">
          <dt>Finishing below the starting equity</dt>
          <dd className={prob === null ? "muted" : prob > 0.3 ? "neg" : prob > 0.1 ? "warn" : ""}>
            {formatRate(prob)}
          </dd>
        </div>
      </dl>
    </div>
  );
}

function Card({ f, index }: { f: FinalistRow; index: number }) {
  const reasons = f.reasons ?? [];
  const verdicts = reasons.map((r) => gateVerdict(r));
  const passed = verdicts.filter((v) => v === "pass").length;
  const failed = verdicts.filter((v) => v === "fail").length;
  const unknown = verdicts.filter((v) => v === "unknown").length;

  const overall =
    f.passed === true ? "is-pass" : f.passed === false ? "is-fail" : "is-unknown";
  const overallText =
    f.passed === true
      ? "ALL GATES PASSED"
      : f.passed === false
        ? "FAILED THE GATES"
        : "NO VERDICT REPORTED";

  const identity = [
    f.symbol ? baseAsset(f.symbol) : null,
    f.regime ? regimeLabel(f.regime) : null,
    toNum(f.horizon_seconds) !== null ? formatHorizon(f.horizon_seconds) : null,
  ].filter(Boolean);

  // With neither a stress suite nor a Monte Carlo block there is nothing to
  // put in the second column, so the card does not keep an empty one.
  const solo = !(f.stress && f.stress.length > 0) && !f.monte_carlo;

  return (
    <article className={`fin ${overall}`}>
      <header className="fin-head">
        <span className="fin-name">{f.family ? familyLabel(f.family) : DASH}</span>
        <span className="fin-sub">
          {identity.length > 0 ? identity.join(" · ") : "kimlik bildirilmedi"}
        </span>
        <span className={`fin-verdict ${overall}`}>{overallText}</span>
      </header>

      <div className={`fin-body${solo ? " is-solo" : ""}`}>
        <div className="fgroup">
          <p className="fgroup-t">
            Gate report{" "}
            <span className="fgroup-n">
              {formatInt(reasons.length)} rows · {passed} passed · {failed} failed
              {unknown > 0 ? ` · ${unknown} belirsiz` : ""}
            </span>
          </p>
          {reasons.length === 0 ? (
            <p className="xnote">
              No gate rationale was reported for this candidate. A pass without a
              rationale does not count as a pass — the gate list below still stands.
            </p>
          ) : (
            <ul className="gates">
              {reasons.map((r, i) => {
                const v = verdicts[i];
                return (
                  <li key={`${index}-${i}`} className={`gate is-${v}`}>
                    <span className="gate-m" aria-hidden>
                      {v === "pass" ? "✓" : v === "fail" ? "✕" : "·"}
                    </span>
                    <span className="gate-t">{r}</span>
                  </li>
                );
              })}
            </ul>
          )}
          {unknown > 0 && reasons.length > 0 && (
            <p className="xnote">
              Rows that do not state their verdict in their own text are drawn neutral.
              The panel does not colour a pass green that it was never told about.
            </p>
          )}
        </div>

        {f.stress && f.stress.length > 0 && <Stress items={f.stress} />}
        {f.monte_carlo && <MonteCarlo mc={f.monte_carlo} />}
      </div>
    </article>
  );
}

/**
 * What survived the gates.
 *
 * The empty case is the expected case and is the most important sentence on
 * this page. It is written as a finding — "nothing passed" — and never as a
 * loading state, an empty table or a blank panel, because those three all read
 * as "the screen is broken" and this one has to read as "the search answered".
 */
export function Finalists({
  finalists,
  loading,
}: {
  finalists: FinalistRow[] | null | undefined;
  loading: boolean;
}) {
  const rows = finalists ?? null;
  // Only an explicit `passed: true` is counted. A gate report that does not
  // state its verdict is a report, not a pass — counting it as one would be
  // the panel promoting a strategy nobody promoted.
  const passedCount = rows?.filter((f) => f.passed === true).length ?? 0;

  const meta =
    rows && rows.length > 0 ? (
      <>
        <span className="pill is-off">{formatInt(rows.length)} gate reports</span>
        {passedCount > 0 && (
          <span className="pill is-pos">{formatInt(passedCount)} finalist</span>
        )}
      </>
    ) : rows ? (
      <span className="pill is-off">finalist yok</span>
    ) : null;

  return (
    <Panel
      idx="11"
      title="Finalistler"
      note="candidates that cleared every robustness gate"
      meta={meta}
      footer={
        rows === null
          ? "A gate report never being produced and no candidate clearing the gates are not the same thing; this panel shows them as two separate screens."
          : rows.length === 0
            ? "An empty finalist list is not a fault but a measurement: none of the candidates tried cleared every gate after costs and out of sample. A search in which nothing works is also a result, and this panel reports it as one."
            : "Every gate is shown with its own rationale row; a row that does not state its verdict in its own text is drawn neutral. A strategy is not a finalist and is not promoted until every gate is passed."
      }
    >
      {loading && rows === null ? (
        <Skeleton rows={4} />
      ) : rows === null ? (
        <div className="fin-empty">
          <div className="fin-empty-head">
            <span className="state-mark" aria-hidden />
            <p className="fin-empty-t">The robustness evaluation has not run yet</p>
          </div>
          <p className="fin-empty-x">
            No gate report was produced. This does not mean &ldquo;no strategy passed&rdquo;
            — it means nobody has sat the exam yet. Those are different statements
            and this panel does not use one for the other. When tournament results
            arrive, every candidate will be put through the gates below one by one.
          </p>
          <GateReference />
        </div>
      ) : rows.length === 0 ? (
        <div className="fin-empty is-none">
          <div className="fin-empty-head">
            <span className="fin-empty-mark" aria-hidden />
            <p className="fin-empty-t">No strategy cleared the gates</p>
          </div>
          <p className="fin-empty-x">
            This is the expected outcome, and an empty panel is the answer to the
            search rather than a defect. None of the candidates tried cleared all
            seven gates below after costs, out of sample and under stress; so there
            is no finalist and no strategy to promote to live paper.
          </p>
          <GateReference />
        </div>
      ) : (
        <div className="fin-list">
          {rows.map((f, i) => (
            <Card key={f.key ?? `${f.family}-${f.symbol}-${i}`} f={f} index={i} />
          ))}
          <GateReference />
        </div>
      )}
    </Panel>
  );
}
