import { Panel } from "@/components/ui/Panel";
import { Skeleton } from "@/components/ui/DataState";
import {
  DASH,
  REJECT_FAMILY_LABELS,
  REJECT_FAMILY_NOTES,
  type RejectFamily,
  calibrationReading,
  decisionLabel,
  formatInt,
  formatMicroMoney,
  formatProbability,
  formatRate,
  formatRelativeMs,
  formatSignedMicroMoney,
  liquidityLabel,
  makerTakerLabel,
  predictRejectLabel,
  rejectFamily,
  regimeLabel,
  sideLabel,
  spreadLabel,
  toNum,
  tone,
} from "@/lib/format";
import type { PredictiveExplanation, PredictiveState, RejectionTally } from "@/lib/types";

/**
 * WHY IT OPENED / WHY IT REJECTED — items 33 and 34.
 *
 * Two readings of the same decision stream, in one panel because they are one
 * question. The cards say what the model and the risk layer actually held
 * when a trade was opened; the distribution says what stopped every candidate
 * that was not.
 *
 * **Nothing on this panel is composed prose.** Every token in a card is one
 * stored field rendered by one formatter, and a field the worker did not send
 * prints an em dash in place rather than being quietly dropped — a sentence
 * that silently shortens when data is missing reads as a complete explanation
 * and is not one.
 *
 * The distribution exists because "found no edge" and "refuses everything"
 * produce an identical flat P&L and are completely different defects. Grouping
 * the nine reasons into model / venue / scope is what makes them
 * distinguishable at a glance instead of on a calculator.
 */

const FAMILY_ORDER: RejectFamily[] = ["model", "venue", "scope"];

const KNOWN_REASONS = [
  "LOW_PROBABILITY",
  "NEGATIVE_EV",
  "COST_TOO_HIGH",
  "MODEL_UNCERTAIN",
  "CORRELATED_RISK",
  "SPREAD_TOO_WIDE",
  "NO_LIQUIDITY",
  "STALE_DATA",
  "REGIME_NOT_SUPPORTED",
];

function decisionClass(code: string | null | undefined): string {
  const k = code ? String(code).toUpperCase() : "";
  if (k === "BUY" || k === "ENTER" || k === "OPEN" || k === "ACCEPT") return "buy";
  if (k === "SELL") return "sell";
  if (k === "WATCH") return "watch";
  return "none";
}

/** One stored field. The key is always drawn, so an absent value is visible. */
function Tok({
  k,
  v,
  cls,
  title,
}: {
  k: string;
  v: string;
  cls?: string;
  title?: string;
}) {
  return (
    <span className="xtok" title={title}>
      <span className="xtok-k">{k}</span>
      <span className={`xtok-v ${cls ?? ""}`}>{v}</span>
    </span>
  );
}

function ExplanationCard({
  e,
  usableProbabilities,
  probNote,
  now,
}: {
  e: PredictiveExplanation;
  usableProbabilities: boolean;
  probNote: string;
  now: number;
}) {
  const loss = toNum(e.expected_net_loss);

  return (
    <article className={`xcard ${decisionClass(e.decision)}`}>
      <header className="xcard-head">
        <span className="xcard-sym" title={e.symbol ?? undefined}>
          {e.symbol ?? DASH}
        </span>
        <span className="xcard-side">{e.side ? sideLabel(String(e.side)) : DASH}</span>
        <span className={`decision ${decisionClass(e.decision)}`} title={e.decision ?? undefined}>
          {decisionLabel(e.decision)}
        </span>
        <span className="xcard-meta">
          {e.trade_id ? `#${e.trade_id}` : DASH}
          {toNum(e.ts_ms) !== null && ` · ${formatRelativeMs(e.ts_ms, now)}`}
        </span>
      </header>

      <div className="xcard-lines">
        <p className="xline">
          <Tok
            k="P(win)"
            v={formatProbability(e.p_win)}
            cls={usableProbabilities ? "" : "is-uncal"}
            title={probNote}
          />
          <Tok
            k="Expected net win"
            v={formatSignedMicroMoney(e.expected_net_win)}
            cls={toNum(e.expected_net_win) === null ? "muted" : "pos"}
          />
          <Tok
            k="Expected loss"
            v={loss === null ? DASH : formatMicroMoney(-Math.abs(loss))}
            cls={loss === null ? "muted" : "neg"}
          />
        </p>
        <p className="xline">
          <Tok
            k="Expected value"
            v={formatSignedMicroMoney(e.expected_value)}
            cls={tone(e.expected_value) ?? "muted"}
          />
          <Tok
            k="Regime"
            v={e.regime ? regimeLabel(e.regime) : DASH}
            cls={e.regime ? "" : "muted"}
            title={e.regime ?? undefined}
          />
          <Tok
            k="Spread"
            v={spreadLabel(e.spread_ok)}
            cls={e.spread_ok === true ? "pos" : e.spread_ok === false ? "warn" : "muted"}
          />
          <Tok
            k="Likidite"
            v={liquidityLabel(e.liquidity)}
            cls={
              e.liquidity === null || e.liquidity === undefined || e.liquidity === ""
                ? "muted"
                : ""
            }
          />
        </p>
        <p className="xline">
          <Tok
            k="Execution"
            v={e.execution_style ? makerTakerLabel(String(e.execution_style)) : DASH}
            cls={
              String(e.execution_style ?? "").toUpperCase() === "MAKER"
                ? "pos"
                : e.execution_style
                  ? "dim"
                  : "muted"
            }
          />
          <Tok
            k="Model"
            v={e.model_version ?? DASH}
            cls={e.model_version ? "dim" : "muted"}
          />
        </p>
      </div>
    </article>
  );
}

interface ReasonRow {
  code: string;
  count: number;
  family: RejectFamily | null;
}

function Rejections({ tally }: { tally: RejectionTally | null | undefined }) {
  if (!tally) {
    return (
      <p className="xnote">
        No rejection counter was reported. That does not mean no candidate was rejected —
        it means the counter was never produced, and without exactly this counter an engine
        that finds no edge cannot be told apart from an engine that rejects everything.
      </p>
    );
  }

  const rows: ReasonRow[] = Object.entries(tally)
    .map(([code, raw]) => ({ code, count: toNum(raw) ?? 0, family: rejectFamily(code) }))
    .filter((r) => toNum(tally[r.code]) !== null)
    .sort((a, b) => b.count - a.count);

  const total = rows.reduce((a, r) => a + r.count, 0);
  const missing = KNOWN_REASONS.filter((k) => toNum(tally[k]) === null);

  if (rows.length === 0) {
    return (
      <p className="xnote">
        The rejection counter arrived but contains not a single reason. That is an empty
        record, not a distribution.
      </p>
    );
  }

  const families = FAMILY_ORDER.map((f) => ({
    family: f,
    count: rows.filter((r) => r.family === f).reduce((a, r) => a + r.count, 0),
  }));
  const unclassified = rows.filter((r) => r.family === null).reduce((a, r) => a + r.count, 0);
  const top = [...families].sort((a, b) => b.count - a.count)[0];

  return (
    <div className="rej">
      <div className="rej-head">
        <span className="rej-total">{formatInt(total)}</span>
        <span className="rej-total-k">rejected candidates</span>
      </div>

      {total > 0 && (
        <>
          <div className="rej-stack" role="img" aria-label="Rejection reasons by family">
            {families.map(
              (f) =>
                f.count > 0 && (
                  <span
                    key={f.family}
                    className={`rej-seg is-${f.family}`}
                    style={{ flexGrow: f.count }}
                    title={`${REJECT_FAMILY_LABELS[f.family]} · ${formatInt(f.count)} · ${formatRate(
                      f.count / total,
                      0,
                    )}`}
                  >
                    {formatRate(f.count / total, 0)}
                  </span>
                ),
            )}
            {unclassified > 0 && (
              <span
                className="rej-seg is-other"
                style={{ flexGrow: unclassified }}
                title={`Unclassified reasons · ${formatInt(unclassified)}`}
              >
                {formatRate(unclassified / total, 0)}
              </span>
            )}
          </div>

          <div className="rej-legend">
            {families.map((f) => (
              <span key={f.family} className={`rej-dot is-${f.family}`} title={REJECT_FAMILY_NOTES[f.family]}>
                <i aria-hidden />
                {REJECT_FAMILY_LABELS[f.family]}
                <b>{formatInt(f.count)}</b>
              </span>
            ))}
            {unclassified > 0 && (
              <span className="rej-dot is-other">
                <i aria-hidden />
                Unclassified
                <b>{formatInt(unclassified)}</b>
              </span>
            )}
          </div>

          {top && top.count > 0 && (
            <p className="rej-read">
              {formatRate(top.count / total, 0)} of the rejections were{" "}
              <strong>{REJECT_FAMILY_LABELS[top.family].toLocaleLowerCase("tr-TR")}</strong>{" "}
              grubundan geliyor. {REJECT_FAMILY_NOTES[top.family]}
            </p>
          )}
        </>
      )}

      <ul className="rej-list">
        {rows.map((r) => (
          <li key={r.code} className={`rej-row is-${r.family ?? "other"}`}>
            <span className="rej-k" title={r.code}>
              {predictRejectLabel(r.code)}
            </span>
            <span className="rej-bar">
              <span
                className={`rej-fill is-${r.family ?? "other"}`}
                style={{ width: total > 0 ? `${(r.count / total) * 100}%` : "0%" }}
              />
            </span>
            <span className="rej-n">{formatInt(r.count)}</span>
            <span className="rej-p">{total > 0 ? formatRate(r.count / total, 0) : DASH}</span>
          </li>
        ))}
      </ul>

      {missing.length > 0 && (
        <p className="rej-missing">
          Reasons not reported ({missing.length}):{" "}
          {missing.map((m) => predictRejectLabel(m)).join(" · ")}. An unreported reason
          does not count as having fired zero times; it never appeared in the counter at all.
        </p>
      )}
    </div>
  );
}

export function Explanations({
  predictive,
  loading,
  now,
}: {
  predictive: PredictiveState | null | undefined;
  loading: boolean;
  now: number;
}) {
  const p = predictive ?? null;
  const cal = calibrationReading(p?.calibration);
  const rows = p?.explanations ?? null;
  const tally = p?.rejections ?? null;

  const rejected = tally
    ? Object.values(tally).reduce<number>((a, v) => a + (toNum(v) ?? 0), 0)
    : null;
  const opened = rows ? rows.filter((e) => decisionClass(e.decision) === "buy").length : null;

  const meta = p ? (
    <>
      {opened !== null && (
        <span
          className="pill is-pos"
          title="Decisions that opened a trade and had their rationale stored — not the closed round-trip count"
        >
          {formatInt(opened)} opening records
        </span>
      )}
      {rejected !== null && (
        <span className="pill is-off" title="Total in the rejection counter">
          {formatInt(rejected)} ret
        </span>
      )}
      {!cal.usable && (
        <span className="pill is-warn" title={cal.note}>
          {cal.text}
        </span>
      )}
    </>
  ) : null;

  return (
    <Panel
      idx="15"
      title="Why it opened / why it rejected"
      note="the model and risk values stored at decision time"
      meta={meta}
      footer="Every row here is drawn from stored fields; none of it is an explanation produced after the fact. A field that did not arrive appears as a dash in place rather than quietly dropping out of the sentence. The rejection distribution is a diagnosis in its own right: an engine that finds no edge and an engine that rejects everything look identical in the P&L table and completely different in the reason distribution."
    >
      {loading && !p ? (
        <Skeleton rows={5} />
      ) : !p ? (
        <div className="pred-empty">
          <div className="fin-empty-head">
            <span className="state-mark" aria-hidden />
            <p className="fin-empty-t">No rationale records</p>
          </div>
          <p className="fin-empty-x">
            Because no predictive engine was reported, there is neither a rationale for an
            opened trade nor a rejection counter. That does not mean the engine rejected
            everything — it means there is no deciding engine yet. Once one is live, every
            paper trade will be stored here together with the probability, expected value,
            regime, spread, liquidity and execution style that produced the decision, and
            every rejected candidate will be counted with its reason.
          </p>
        </div>
      ) : (
        <div className="xwrap">
          <div className="xside">
            <p className="fgroup-t">
              Neden reddetti{" "}
              <span className="fgroup-n">distribution by reason</span>
            </p>
            <Rejections tally={tally} />
          </div>

          <div className="xmain">
            <p className="fgroup-t">
              Why it opened{" "}
              <span className="fgroup-n">
                {rows ? `${formatInt(rows.length)} records` : "no records reported"}
              </span>
            </p>
            {!rows ? (
              <p className="xnote">
                No rationale list was reported. A trade whose rationale was not stored cannot
                be explained afterwards — so no guess is produced here.
              </p>
            ) : rows.length === 0 ? (
              <p className="xnote">
                The engine ran and opened no trade. On its own that is not a fault: in a
                market that cannot cover its own cost, opening nothing is the correct
                behaviour. Whether this is a defect or the right call is read off the
                rejection distribution on the left — look at whether candidates were rejected
                because no edge was found, or because they never reached the model's decision.
              </p>
            ) : (
              <div className="xlist">
                {rows.map((e, i) => (
                  <ExplanationCard
                    key={e.trade_id ?? `${e.symbol ?? "?"}-${i}`}
                    e={e}
                    usableProbabilities={cal.usable}
                    probNote={cal.note}
                    now={now}
                  />
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </Panel>
  );
}
