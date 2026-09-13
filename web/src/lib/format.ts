/**
 * Rendering rules for this terminal:
 *
 *   * Money is "$1.234,56" in US grouping — "$1,234.56" — even though the
 *     surrounding copy is Turkish, because the exchange quotes in USD and
 *     switching the separators mid-table is how misreadings happen.
 *   * A value we do not have renders as EM DASH, never as 0. The bot starting
 *     with zero trades and the bot failing to report its trades are different
 *     statements; this module keeps them apart.
 *   * Every figure is set in the mono face by the CSS — that is what makes a
 *     column read as instrument output instead of as prose.
 */

export const DASH = "—";

export type Numeric = number | string | null | undefined;

/** Parse an API numeric. Returns null for anything absent or unparseable. */
export function toNum(value: Numeric): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

export function formatMoney(value: Numeric, digits = 2): string {
  const n = toNum(value);
  if (n === null) return DASH;
  const sign = n < 0 ? "-" : "";
  return `${sign}$${Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

/** Money with an explicit + on gains — used wherever direction is the point. */
export function formatSignedMoney(value: Numeric, digits = 2): string {
  const n = toNum(value);
  if (n === null) return DASH;
  if (n > 0) return `+${formatMoney(n, digits)}`;
  return formatMoney(n, digits);
}

/**
 * A scalping P&L line is routinely a few cents, and rounding those to "$0.00"
 * prints a zero for a figure that is not zero — so precision follows
 * magnitude: 2 dp from a dollar up, 4 dp from a cent up, 6 dp below that.
 */
export function formatMicroMoney(value: Numeric): string {
  const n = toNum(value);
  if (n === null) return DASH;
  const abs = Math.abs(n);
  if (abs === 0) return formatMoney(0, 2);
  if (abs >= 1) return formatMoney(n, 2);
  if (abs >= 0.01) return formatMoney(n, 4);
  return formatMoney(n, 6);
}

export function formatSignedMicroMoney(value: Numeric): string {
  const n = toNum(value);
  if (n === null) return DASH;
  return n > 0 ? `+${formatMicroMoney(n)}` : formatMicroMoney(n);
}

/**
 * A crypto price spans $0.00002 (SHIB) to $100,000 (BTC) in the same column,
 * so a fixed decimal count is wrong at one end or the other.
 */
export function formatPrice(value: Numeric): string {
  const n = toNum(value);
  if (n === null) return DASH;
  const abs = Math.abs(n);
  const digits = abs >= 1000 ? 2 : abs >= 1 ? 4 : abs >= 0.01 ? 5 : 8;
  return n.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function formatInt(value: Numeric): string {
  const n = toNum(value);
  if (n === null) return DASH;
  return Math.round(n).toLocaleString("en-US");
}

export function formatNumber(value: Numeric, digits = 2): string {
  const n = toNum(value);
  if (n === null) return DASH;
  return n.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/** A rate in [0, 1] rendered as a percentage. */
export function formatRate(value: Numeric, digits = 1): string {
  const n = toNum(value);
  if (n === null) return DASH;
  return `%${(n * 100).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

/** A percentage already expressed in points (1.0 === 1%). */
export function formatPct(value: Numeric, digits = 1): string {
  const n = toNum(value);
  if (n === null) return DASH;
  return `%${n.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

/** Basis points, always signed — a negative edge is the headline, not a detail. */
export function formatBps(value: Numeric, digits = 1): string {
  const n = toNum(value);
  if (n === null) return DASH;
  const body = n.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
  return `${n > 0 ? "+" : ""}${body}`;
}

/** Unsigned bps — for spread, which has no direction. */
export function formatBpsAbs(value: Numeric, digits = 1): string {
  const n = toNum(value);
  if (n === null) return DASH;
  return n.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/**
 * Profit factor is unbounded when there are no losses. Printing "Infinity"
 * (or a huge number) would suggest a measured figure; "∞" says plainly that
 * the denominator is still zero.
 */
export function formatFactor(value: Numeric): string {
  const n = toNum(value);
  if (n === null) return DASH;
  if (!Number.isFinite(n) || n >= 999) return "∞";
  return n.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export function formatMs(value: Numeric): string {
  const n = toNum(value);
  if (n === null) return DASH;
  if (n < 1000) return `${Math.round(n)} ms`;
  if (n < 60000)
    return `${(n / 1000).toLocaleString("en-US", { maximumFractionDigits: 2 })} s`;
  return `${(n / 60000).toLocaleString("en-US", { maximumFractionDigits: 1 })} m`;
}

/** A holding time as a duration: 42s · 7m 12s · 1h 03m. */
export function formatDuration(seconds: Numeric): string {
  const n = toNum(seconds);
  if (n === null) return DASH;
  const s = Math.max(0, Math.round(n));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${String(m % 60).padStart(2, "0")}m`;
  return `${Math.floor(h / 24)}d ${String(h % 24).padStart(2, "0")}h`;
}

/** An epoch-ms timestamp as a wall clock, to the second. */
export function formatClockMs(value: Numeric): string {
  const n = toNum(value);
  if (n === null || n <= 0) return DASH;
  return new Date(n).toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function formatDateTimeMs(value: Numeric): string {
  const n = toNum(value);
  if (n === null || n <= 0) return DASH;
  return new Date(n).toLocaleString("en-GB", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** Relative age of an epoch-ms timestamp. `now` is injected so a ticking
    clock can drive it without every caller reading Date.now() separately. */
export function formatRelativeMs(value: Numeric, now: number = Date.now()): string {
  const n = toNum(value);
  if (n === null || n <= 0) return DASH;
  const sec = Math.max(0, Math.round((now - n) / 1000));
  if (sec < 2) return "just now";
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hour = Math.floor(min / 60);
  if (hour < 24) return `${hour}h ago`;
  return `${Math.floor(hour / 24)}d ago`;
}

/** Volume multiples read better as "1.8×" than as a bare number. */
export function formatRatio(value: Numeric): string {
  const n = toNum(value);
  if (n === null) return DASH;
  return `${n.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}×`;
}

/** Sign of a P&L figure, for tone classes. Null is toneless, not neutral. */
export function tone(value: Numeric): "pos" | "neg" | "flat" | undefined {
  const n = toNum(value);
  if (n === null) return undefined;
  if (n > 0) return "pos";
  if (n < 0) return "neg";
  return "flat";
}

// --------------------------------------------------------------------------
// Vocabulary. Every enum in api/app/types.py that the dashboard renders has a
// display label here; the raw code always survives in a `title` attribute so
// an operator reading a log and an operator reading the screen see the same
// system.
// --------------------------------------------------------------------------

export const SIDE_LABELS: Record<string, string> = {
  LONG: "LONG",
  SHORT: "SHORT",
};

export const DECISION_LABELS: Record<string, string> = {
  BUY: "BUY",
  SELL: "SELL",
  WATCH: "WATCH",
  NO_TRADE: "NO TRADE",
};

export const REGIME_LABELS: Record<string, string> = {
  TRENDING: "TRENDING",
  RANGING: "RANGING",
  BREAKOUT: "BREAKOUT",
  HIGH_VOLATILITY: "HIGH VOLATILITY",
  LOW_VOLATILITY: "LOW VOLATILITY",
  ABNORMAL: "ABNORMAL",
  UNKNOWN: "UNKNOWN",
};

export const EXIT_REASON_LABELS: Record<string, string> = {
  TAKE_PROFIT: "TAKE PROFIT",
  TRAILING_PROFIT: "TRAILING PROFIT",
  STOP_LOSS: "STOP LOSS",
  TIME_STOP: "TIME STOP",
  SIGNAL_REVERSAL: "SIGNAL REVERSAL",
  RISK_EXIT: "RISK EXIT",
};

/** Why the risk layer refused a candidate (RejectReason in types.py). */
export const REJECT_REASON_LABELS: Record<string, string> = {
  STALE_BOOK: "Order book is stale",
  WIDE_SPREAD: "Spread too wide",
  THIN_DEPTH: "Insufficient depth",
  NEGATIVE_EDGE: "Net edge is negative",
  EDGE_BELOW_MIN: "Edge below the minimum",
  RISK_BUDGET: "Risk budget exhausted",
  MAX_POSITIONS: "Max concurrent positions",
  DUPLICATE_SYMBOL: "Already holding this asset",
  DAILY_LOSS_LIMIT: "Daily loss limit",
  WEEKLY_LOSS_LIMIT: "Weekly loss limit",
  LOSS_STREAK_COOLDOWN: "Loss-streak cooldown",
  ABNORMAL_VOLATILITY: "Abnormal volatility",
  DATA_FAILURE: "Data failure",
  KILL_SWITCH: "Kill switch engaged",
  BELOW_MIN_NOTIONAL: "Below minimum notional",
  BAD_REWARD_RISK: "Reward/risk ratio too low",
  SLIPPAGE_TOO_HIGH: "Slippage too high",
  NOT_SUPPORTED_SHORT: "Short side not supported",
};

function labelOf(map: Record<string, string>, value: string | null | undefined): string {
  if (!value) return DASH;
  return map[value] ?? value;
}

export const sideLabel = (v: string | null | undefined) => labelOf(SIDE_LABELS, v);
export const decisionLabel = (v: string | null | undefined) =>
  labelOf(DECISION_LABELS, v);
export const regimeLabel = (v: string | null | undefined) => labelOf(REGIME_LABELS, v);
export const exitReasonLabel = (v: string | null | undefined) =>
  labelOf(EXIT_REASON_LABELS, v);
export const rejectReasonLabel = (v: string | null | undefined) =>
  labelOf(REJECT_REASON_LABELS, v);

/** Strategy module names arrive as identifiers; make them readable without
    inventing a translation for a module we have never heard of. */
export function strategyLabel(value: string | null | undefined): string {
  if (!value) return DASH;
  return value.replace(/_/g, " ").toUpperCase();
}

/** BTCUSD → BTC. The quote leg is always USD here, so printing it in every
    row of every table is noise. */
export function baseAsset(symbol: string | null | undefined): string {
  if (!symbol) return DASH;
  return symbol.replace(/(USDT|USDC|USD)$/i, "") || symbol;
}

// --------------------------------------------------------------------------
// V2 vocabulary — fee-aware net targets, maker-first execution, the 25/60
// minute horizon and the portfolio layer.
// --------------------------------------------------------------------------

export const EXIT_REASON_LABELS_V2: Record<string, string> = {
  EXIT_WEAK: "WEAK EXIT",
  ROTATE_CAPITAL: "CAPITAL ROTATION",
};

Object.assign(EXIT_REASON_LABELS, EXIT_REASON_LABELS_V2);

Object.assign(REJECT_REASON_LABELS, {
  NET_TARGET_UNREACHABLE: "NET target unreachable",
  CASH_RESERVE: "Cash reserve protected",
  PORTFOLIO_RISK_BUDGET: "Portfolio risk budget exhausted",
  CORRELATED_EXPOSURE: "Correlated exposure limit",
  DIRECTIONAL_EXPOSURE: "Directional exposure limit",
});

/** MAKER is 0% on Binance.US — the badge is a cost statement, not a label. */
export const MAKER_TAKER_LABELS: Record<string, string> = {
  MAKER: "MAKER",
  TAKER: "TAKER",
};

export const CHECKPOINT_LABELS: Record<string, string> = {
  PENDING: "PENDING",
  KEEP: "KEEP",
  TRAIL: "TRAIL",
  DUE: "DUE",
};

export const ALLOCATION_MODE_LABELS: Record<string, string> = {
  FEW_LARGE: "FEW / LARGE",
  MANY_SMALL: "MANY / SMALL",
  DYNAMIC: "DYNAMIC",
};

export const NET_TP_PROFILE_LABELS: Record<string, string> = {
  MICRO: "MICRO · 0.25%",
  BASE: "BASE · 0.50%",
  FAST: "FAST · 0.75%",
  EXTENDED: "EXTENDED · 1.00%",
};

export function makerTakerLabel(v: string | null | undefined): string {
  if (!v) return DASH;
  return MAKER_TAKER_LABELS[v] ?? v;
}

export function checkpointLabel(v: string | null | undefined): string {
  if (!v) return DASH;
  return CHECKPOINT_LABELS[v] ?? v;
}

export function allocationModeLabel(v: string | null | undefined): string {
  if (!v) return DASH;
  return ALLOCATION_MODE_LABELS[v] ?? v;
}

export function netTpProfileLabel(v: string | null | undefined): string {
  if (!v) return DASH;
  return NET_TP_PROFILE_LABELS[v] ?? v;
}

/**
 * The expected hold window may arrive as a label the worker chose ("25-60m")
 * or as a plain number of seconds. Both are rendered as a window; neither is
 * guessed at when absent.
 */
export function formatHoldWindow(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return DASH;
  if (typeof value === "number") {
    return Number.isFinite(value) ? formatDuration(value) : DASH;
  }
  const asNumber = Number(value);
  if (value.trim() !== "" && Number.isFinite(asNumber)) return formatDuration(asNumber);
  return value;
}

/**
 * Time remaining until an epoch-ms deadline. `now` is injected so the whole
 * screen counts down off one clock. Past the deadline this says so rather than
 * printing a negative duration.
 */
export function formatCountdownMs(value: Numeric, now: number): string {
  const n = toNum(value);
  if (n === null || n <= 0) return DASH;
  const left = Math.round((n - now) / 1000);
  if (left <= 0) return "expired";
  return `${formatDuration(left)} left`;
}

/**
 * A cost figure. Costs are always drawn as the negative they are — printing
 * "$0.04" next to a gross gain invites reading the cost as income.
 */
export function formatCost(value: Numeric): string {
  const n = toNum(value);
  if (n === null) return DASH;
  if (n === 0) return formatMicroMoney(0);
  return formatMicroMoney(-Math.abs(n));
}

/**
 * Signal strength arrives either as a 0–1 fraction or as a 0–100 reading. The
 * bar needs a percentage either way; anything outside those ranges is clamped
 * rather than drawn off the end of its track.
 */
export function strengthPct(value: Numeric): number | null {
  const n = toNum(value);
  if (n === null) return null;
  const pct = Math.abs(n) <= 1 ? n * 100 : n;
  return Math.max(0, Math.min(100, pct));
}

// --------------------------------------------------------------------------
// Phase 3 vocabulary — the strategy tournament, the promotion gates and the
// seven-regime classifier the research families are conditioned on.
// --------------------------------------------------------------------------

/** Regime7 from `app/research/regimes.py`. The V1 codes above stay valid; this
    adds the finer set the tournament partitions its arms by. */
Object.assign(REGIME_LABELS, {
  TRENDING_UP: "TREND ↑",
  TRENDING_DOWN: "TREND ↓",
  VOLATILITY_EXPANSION: "VOLATILITY EXPANSION",
  PANIC: "PANIC",
});

/**
 * Where an arm stands against the out-of-sample gates.
 *
 * These are verdicts, not descriptions. "MARGINAL" exists so a profit factor
 * between 1.0 and 1.2 can be reported without ever being called a finalist,
 * and "INSUFFICIENT SAMPLE" exists so an arm with nine lucky trades is not
 * mistaken for one that failed — it has not been judged at all.
 */
export const OOS_STATUS_LABELS: Record<string, string> = {
  FINALIST: "FINALIST",
  MARGINAL: "MARGINAL",
  FAILED: "ELIMINATED",
  INSUFFICIENT_SAMPLE: "INSUFFICIENT SAMPLE",
};

export const OOS_STATUS_NOTES: Record<string, string> = {
  FINALIST: "Cleared every out-of-sample gate",
  MARGINAL: "Positive but below the 1.20 profit-factor bar — not a finalist",
  FAILED: "Failed at least one out-of-sample gate",
  INSUFFICIENT_SAMPLE:
    "Fewer than 30 out-of-sample trades — this arm cannot be judged yet, it has not been eliminated",
};

export function oosStatusLabel(v: string | null | undefined): string {
  if (!v) return DASH;
  return OOS_STATUS_LABELS[v] ?? v;
}

/** Tone class for a verdict. Unknown verdicts stay toneless rather than
    borrowing a colour they have not earned. */
export function oosStatusTone(v: string | null | undefined): string {
  switch (v) {
    case "FINALIST":
      return "is-finalist";
    case "MARGINAL":
      return "is-marginal";
    case "FAILED":
      return "is-failed";
    case "INSUFFICIENT_SAMPLE":
      return "is-thin";
    default:
      return "is-unknown";
  }
}

/** Strategy family identifiers arrive as module-style names. */
export const familyLabel = strategyLabel;

/** Hours of recorded tape. Below a day the hour count is the reading; above
    it, days are, because "312 sa" stops being legible as a duration. */
export function formatHours(value: Numeric): string {
  const n = toNum(value);
  if (n === null) return DASH;
  if (n < 24) {
    return `${n.toLocaleString("en-US", { maximumFractionDigits: 1 })} sa`;
  }
  const days = Math.floor(n / 24);
  const rem = Math.round(n % 24);
  return `${days} g ${String(rem).padStart(2, "0")} sa`;
}

/** Megabytes written. */
export function formatMb(value: Numeric): string {
  const n = toNum(value);
  if (n === null) return DASH;
  if (Math.abs(n) >= 1024) {
    return `${(n / 1024).toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })} GB`;
  }
  return `${n.toLocaleString("en-US", {
    minimumFractionDigits: n < 10 ? 2 : 0,
    maximumFractionDigits: n < 10 ? 2 : 0,
  })} MB`;
}

/**
 * A research holding horizon. The tournament sweeps from seconds to hours in
 * one column, so this reads as a duration rather than as a raw second count.
 */
export function formatHorizon(value: Numeric): string {
  const n = toNum(value);
  if (n === null) return DASH;
  return formatDuration(n);
}

/**
 * Classify one gate line without inventing a verdict for it.
 *
 * `GateReport.reasons` carries one line per gate, pass or fail, written by the
 * research module. The line's own wording is the only evidence of which it is,
 * so a line that does not announce a verdict is rendered as a neutral
 * statement — colouring it green because it sits under a passing candidate
 * would be the dashboard asserting something it was not told.
 */
export function gateVerdict(line: string): "pass" | "fail" | "unknown" {
  const s = line.trim();
  if (/^(?:✗|✖|×|❌)/.test(s)) return "fail";
  if (/^(?:✓|✔|✅)/.test(s)) return "pass";
  if (/^\[\s*[xX✓]\s*\]/.test(s)) return "pass";
  if (/^\[\s*\]/.test(s)) return "fail";
  if (/^(?:fail|failed|reject|rejected)\b/i.test(s)) return "fail";
  if (/^(?:pass|passed|ok)\b/i.test(s)) return "pass";
  if (/\b(?:FAIL|FAILED)\b/.test(s)) return "fail";
  if (/\b(?:PASS|PASSED)\b/.test(s)) return "pass";
  return "unknown";
}

// --------------------------------------------------------------------------
// Phase 4 vocabulary — the predictive entry engine, its calibration and the
// reasons it refuses.
//
// One rule governs this whole block and it is the reason the helpers below
// return a *reading* rather than a formatted string: an uncalibrated model's
// output is a number, not a probability, and no caller is allowed to print it
// as a confidence. Turning that check into a shared function is what stops it
// from being forgotten in one of the three panels.
// --------------------------------------------------------------------------

/** Why the predictive engine refused a candidate (Phase 4 spec, item 34). */
export const PREDICT_REJECT_REASON_LABELS: Record<string, string> = {
  LOW_PROBABILITY: "Probability below threshold",
  NEGATIVE_EV: "Expected value is negative",
  COST_TOO_HIGH: "Execution cost consumes the expectancy",
  MODEL_UNCERTAIN: "Model is uncertain",
  CORRELATED_RISK: "Correlated risk limit",
  SPREAD_TOO_WIDE: "Spread too wide",
  NO_LIQUIDITY: "No liquidity",
  STALE_DATA: "Data is stale",
  REGIME_NOT_SUPPORTED: "Regime outside the model's scope",
};

Object.assign(REJECT_REASON_LABELS, PREDICT_REJECT_REASON_LABELS);

/**
 * The nine reasons, grouped by what a large count in that group would mean.
 *
 * This grouping is the diagnosis the brief asks for. "Found no edge" and
 * "refuses everything" produce the same flat P&L and are different problems:
 * a tall `model` bar says the model looked and found nothing, a tall `venue`
 * bar says it never got to look, and a tall `scope` bar says the engine is
 * outside the conditions it was trained and sized for.
 */
export type RejectFamily = "model" | "venue" | "scope";

export const PREDICT_REJECT_FAMILY: Record<string, RejectFamily> = {
  LOW_PROBABILITY: "model",
  NEGATIVE_EV: "model",
  MODEL_UNCERTAIN: "model",
  COST_TOO_HIGH: "venue",
  SPREAD_TOO_WIDE: "venue",
  NO_LIQUIDITY: "venue",
  STALE_DATA: "venue",
  REGIME_NOT_SUPPORTED: "scope",
  CORRELATED_RISK: "scope",
};

export const REJECT_FAMILY_LABELS: Record<RejectFamily, string> = {
  model: "Model found no edge",
  venue: "Venue conditions and cost",
  scope: "Scope and portfolio limits",
};

export const REJECT_FAMILY_NOTES: Record<RejectFamily, string> = {
  model:
    "The model looked and found neither sufficient probability nor positive expected value. This is the engine working and reading no edge in the market.",
  venue:
    "The model never got to decide: spread, depth, cost or data freshness eliminated the candidate first. This is not the absence of edge, it is the inability to measure it.",
  scope:
    "The candidate fell outside the regime the model was trained on, or outside the correlated risk the portfolio can carry. It says nothing about edge.",
};

export function predictRejectLabel(v: string | null | undefined): string {
  if (!v) return DASH;
  return PREDICT_REJECT_REASON_LABELS[v] ?? REJECT_REASON_LABELS[v] ?? v;
}

export function rejectFamily(code: string): RejectFamily | null {
  return PREDICT_REJECT_FAMILY[code] ?? null;
}

// ------------------------------------------------------------- calibration

/**
 * The promotion gate from the Phase 4 spec: a maximum absolute reliability
 * error above this means a 70% prediction does not win ~70% of the time, so
 * the number is not a probability and must not be shown as one.
 */
export const CALIBRATION_MAX_ABS_ERROR = 0.1;

export const CALIBRATION_STATUS_LABELS: Record<string, string> = {
  CALIBRATED: "CALIBRATED",
  UNCALIBRATED: "NOT CALIBRATED",
  POOR: "POOR CALIBRATION",
  BAD: "POOR CALIBRATION",
  FAILED: "CALIBRATION FAILED",
  UNMEASURED: "NOT MEASURED",
  NOT_MEASURED: "NOT MEASURED",
  PENDING: "NOT MEASURED",
  UNKNOWN: "NOT MEASURED",
};

export const CALIBRATION_METHOD_LABELS: Record<string, string> = {
  isotonic: "isotonic",
  platt: "Platt",
  sigmoid: "Platt",
  none: "none",
};

export function calibrationMethodLabel(v: string | null | undefined): string {
  if (!v) return DASH;
  return CALIBRATION_METHOD_LABELS[v] ?? CALIBRATION_METHOD_LABELS[v.toLowerCase()] ?? v;
}

/**
 * Which of the four things the calibration block is actually saying.
 *
 *   trusted      — measured, calibrated, inside the 0.10 reliability gate
 *   poor         — measured and outside it
 *   uncalibrated — the model itself reports it was never calibrated
 *   unmeasured   — nobody measured it, which is NOT the same as it passing
 */
export type ConfidenceKind = "trusted" | "poor" | "uncalibrated" | "unmeasured" | "absent";

export interface ConfidenceReading {
  kind: ConfidenceKind;
  /** What goes in the cell. Never a number unless `usable` is true. */
  text: string;
  /** The reason, in one line — for the cell's title and the panel's strip. */
  note: string;
  /** Whether this model's outputs may be read as probabilities at all. */
  usable: boolean;
}

const UNMEASURED_NOTE =
  "Calibration was never measured. An unmeasured calibration is not a passing one — as it stands, this model's outputs do not count as probabilities.";
const UNCALIBRATED_NOTE =
  "The model was not calibrated. Its output can still produce a ranking, but there is no guarantee that a stated 70% wins 70% of the time.";
const POOR_NOTE = `Calibration was measured and failed: the maximum absolute reliability error is above the ${CALIBRATION_MAX_ABS_ERROR
  .toLocaleString("en-US", { minimumFractionDigits: 2 })} limit. This output is a number, not a probability.`;
const TRUSTED_NOTE =
  "Calibration was measured and is inside the limit: the probability the model reports matches the frequency it is realised at.";

const UNMEASURED_STATUSES = new Set(["UNMEASURED", "NOT_MEASURED", "PENDING", "UNKNOWN"]);
const POOR_STATUSES = new Set(["POOR", "BAD", "FAILED"]);

export interface CalibrationLike {
  max_abs_error?: Numeric;
  calibrated?: boolean | null;
  status?: string | null;
}

/**
 * Read the calibration block into a verdict.
 *
 * The order of the checks is the honesty: a model is only trusted when it
 * says it was calibrated AND the reliability error was measured AND that
 * measurement is inside the gate. Anything else — including a calibration
 * block that simply says nothing — falls through to "unmeasured", because
 * absent evidence is not evidence of a pass.
 */
export function calibrationReading(
  cal: CalibrationLike | null | undefined,
): ConfidenceReading {
  const unmeasured: ConfidenceReading = {
    kind: "unmeasured",
    text: "NOT MEASURED",
    note: UNMEASURED_NOTE,
    usable: false,
  };
  if (!cal) return unmeasured;

  const status = cal.status ? String(cal.status).toUpperCase() : null;
  const err = toNum(cal.max_abs_error);

  if (cal.calibrated === false) {
    return {
      kind: "uncalibrated",
      text: "NOT CALIBRATED",
      note: UNCALIBRATED_NOTE,
      usable: false,
    };
  }
  if (status && UNMEASURED_STATUSES.has(status)) return unmeasured;
  if (err !== null && err > CALIBRATION_MAX_ABS_ERROR) {
    return { kind: "poor", text: "POOR CALIBRATION", note: POOR_NOTE, usable: false };
  }
  if (status && POOR_STATUSES.has(status)) {
    return { kind: "poor", text: "POOR CALIBRATION", note: POOR_NOTE, usable: false };
  }
  if (cal.calibrated === true && err !== null) {
    return { kind: "trusted", text: "CALIBRATED", note: TRUSTED_NOTE, usable: true };
  }
  return unmeasured;
}

/**
 * The confidence cell for one candidate.
 *
 * Three gates, in order: the model's calibration has to be trustworthy, the
 * candidate has to claim its confidence is known, and the figure has to
 * actually be there. A failure at any of them prints the failure, never a
 * number — a confidence percentage on an uncalibrated model is the single
 * most authoritative-looking lie this screen could tell.
 */
export function confidenceReading(
  value: Numeric,
  known: boolean | null | undefined,
  cal: CalibrationLike | null | undefined,
): ConfidenceReading {
  const base = calibrationReading(cal);
  if (!base.usable) return base;
  if (known === false) {
    return {
      kind: "unmeasured",
      text: "UNKNOWN",
      note: "The model reported no confidence for this candidate. An unreported confidence is not a low confidence.",
      usable: false,
    };
  }
  const v = toNum(value);
  if (v === null) {
    return {
      kind: "absent",
      text: DASH,
      note: "No confidence figure arrived for this candidate.",
      usable: false,
    };
  }
  return { kind: "trusted", text: formatRate(v, 0), note: TRUSTED_NOTE, usable: true };
}

export function calibrationStatusLabel(v: string | null | undefined): string {
  if (!v) return DASH;
  return CALIBRATION_STATUS_LABELS[String(v).toUpperCase()] ?? v;
}

/** Tone class for a calibration verdict. */
export function confidenceTone(kind: ConfidenceKind): string {
  switch (kind) {
    case "trusted":
      return "is-trusted";
    case "poor":
      return "is-poor";
    case "uncalibrated":
      return "is-uncal";
    case "unmeasured":
      return "is-unmeasured";
    default:
      return "is-absent";
  }
}

// ------------------------------------------------------ predictive figures

/**
 * A probability in [0, 1]. Rendered without decimals: the spec's own example
 * reads "%68", and a third decimal on a figure whose reliability error is
 * measured in whole percentage points would be false precision.
 */
export function formatProbability(value: Numeric): string {
  const n = toNum(value);
  if (n === null) return DASH;
  return formatRate(n, 0);
}

/** Brier score — lower is better, 0.25 is the coin flip. */
export function formatScore(value: Numeric, digits = 4): string {
  const n = toNum(value);
  if (n === null) return DASH;
  return n.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/** A `[start_ms, end_ms]` split leg, as a date range. */
export function formatPeriod(period: [number, number] | null | undefined): string {
  if (!period || period.length < 2) return DASH;
  const a = toNum(period[0]);
  const b = toNum(period[1]);
  if (a === null || b === null || a <= 0 || b <= 0) return DASH;
  const day = (ms: number) =>
    new Date(ms).toLocaleDateString("en-GB", {
      day: "2-digit",
      month: "2-digit",
      year: "2-digit",
    });
  return `${day(a)} → ${day(b)}`;
}

/** The span of a split leg, in days. */
export function periodDays(period: [number, number] | null | undefined): number | null {
  if (!period || period.length < 2) return null;
  const a = toNum(period[0]);
  const b = toNum(period[1]);
  if (a === null || b === null || b <= a) return null;
  return (b - a) / 86_400_000;
}

export function formatDays(value: Numeric): string {
  const n = toNum(value);
  if (n === null) return DASH;
  return `${n.toLocaleString("en-US", { maximumFractionDigits: n < 10 ? 1 : 0 })} d`;
}

/** Liquidity arrives either as a label the worker chose or as a depth figure. */
export const LIQUIDITY_LABELS: Record<string, string> = {
  STRONG: "strong",
  GOOD: "good",
  ADEQUATE: "adequate",
  OK: "adequate",
  THIN: "thin",
  WEAK: "weak",
  NONE: "none",
  UNKNOWN: DASH,
};

export function liquidityLabel(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return DASH;
  if (typeof value === "number") {
    return Number.isFinite(value) ? formatMoney(value, 0) : DASH;
  }
  return LIQUIDITY_LABELS[value.toUpperCase()] ?? value;
}

/** `spread_ok` is a boolean gate, and an unreported gate is not a passed one. */
export function spreadLabel(ok: boolean | null | undefined): string {
  if (ok === true) return "acceptable";
  if (ok === false) return "wide";
  return DASH;
}

Object.assign(DECISION_LABELS, {
  ENTER: "OPENED",
  OPEN: "OPENED",
  ACCEPT: "OPENED",
  REJECT: "REJECTED",
  REJECTED: "REJECTED",
  SKIP: "NO TRADE",
});

// --------------------------------------------------------------------------
// Phase 5 vocabulary — capital utilisation, the idle-capital attribution and
// the post-hoc missed-opportunity audit.
//
// One argument governs this whole block: the six idle reasons are not six
// flavours of the same thing. Two of them are opposite findings, and which
// one dominates is the entire Phase 5 question — so the grouping below is a
// diagnosis, not a colour scheme, and every caller reads it from here rather
// than re-deciding per panel.
// --------------------------------------------------------------------------

/** The deployment band the Phase 5 brief sets: 60–85% of equity at work. */
export const DEPLOY_BAND: readonly [number, number] = [60, 85];

/** Above this the book has no meaningful cash buffer left. */
export const DEPLOY_CEILING = 95;

/** `IdleReason` in `app/portfolio.py`, in the order the brief lists them. */
export const IDLE_REASONS: readonly string[] = [
  "NO_QUALIFIED_SIGNAL",
  "CORRELATION_LIMIT",
  "RISK_LIMIT",
  "LIQUIDITY_LIMIT",
  "EXECUTION_COST",
  "CASH_RESERVE",
];

export const IDLE_REASON_LABELS: Record<string, string> = {
  NO_QUALIFIED_SIGNAL: "No qualified signal",
  CORRELATION_LIMIT: "Correlation limit",
  RISK_LIMIT: "Risk budget exhausted",
  LIQUIDITY_LIMIT: "Liquidity limit",
  EXECUTION_COST: "Execution cost",
  CASH_RESERVE: "Cash reserve",
};

export const IDLE_REASON_NOTES: Record<string, string> = {
  NO_QUALIFIED_SIGNAL:
    "The scanner looked and found no candidate that cleared the gates. This capital is idle because there was nothing to buy.",
  CORRELATION_LIMIT:
    "There was a qualified candidate; the portfolio declined it because it counts as the same bet it already holds.",
  RISK_LIMIT:
    "There was a candidate; no position was added on top because the open risk budget is exhausted.",
  LIQUIDITY_LIMIT:
    "There was a candidate; book depth could not carry the intended size, so the notional was cut or cancelled.",
  EXECUTION_COST:
    "There was a candidate, and it was not rejected for being weak — it was rejected because fees, spread and slippage together consumed the entire expected move.",
  CASH_RESERVE:
    "This capital is held back deliberately. A reserve is a decision, not a defect, and does not mean the same thing as the other five reasons.",
};

/**
 * The three things a tall bar can mean. This is the reading the Phase 5 brief
 * is built around:
 *
 *   supply — there was nothing to buy. A wider universe is exactly the fix.
 *   venue  — there were candidates and they were not worth their own costs.
 *            A wider universe does not help and plausibly hurts: non-Tier-0
 *            pairs cost ~20 bps a round trip against ~2 bps on Tier-0.
 *   policy — there were candidates and the portfolio deliberately refused
 *            them. Says nothing about the venue at all.
 */
export type IdleGroup = "supply" | "venue" | "policy" | "defect";

export const IDLE_REASON_GROUP: Record<string, IdleGroup> = {
  NO_QUALIFIED_SIGNAL: "supply",
  EXECUTION_COST: "venue",
  LIQUIDITY_LIMIT: "venue",
  CORRELATION_LIMIT: "policy",
  RISK_LIMIT: "policy",
  CASH_RESERVE: "policy",
};

export const IDLE_GROUP_ORDER: readonly IdleGroup[] = ["supply", "venue", "policy", "defect"];

export const IDLE_GROUP_LABELS: Record<IdleGroup, string> = {
  supply: "THERE WAS NOTHING TO BUY",
  venue: "CANDIDATES EXISTED, NOT WORTH THEIR OWN COST",
  policy: "CANDIDATES EXISTED, DELIBERATELY DECLINED",
  // Phase 6. The first three are readings about the market or about a
  // decision; this one is a defect report and must not be mistaken for either.
  defect: "CAPITAL IDLE BECAUSE OF OUR OWN DEFECT",
};

/** The one-line conclusion each group licenses about widening the universe. */
export const IDLE_GROUP_VERDICTS: Record<IdleGroup, string> = {
  defect:
    "Widening the universe does not fix this; until this row is zero, the reading of every other reason is unreliable too.",
  supply:
    "Scanning more markets WOULD put this capital to work: the constraint is a narrow universe.",
  venue:
    "Scanning more markets would NOT put this capital to work: the constraint is cost, and new pairs are more expensive (~20 bps against ~2 bps on Tier-0).",
  policy:
    "This capital is idle because of a portfolio decision, not a search problem. Widening the universe does not change it.",
};

export const IDLE_GROUP_NOTES: Record<IdleGroup, string> = {
  defect:
    "Capital is not working because of the system's own latency or an accounting error — not because of the market and not because of a portfolio decision. This group is not a market reading.",
  supply:
    "The scanner ran and produced no candidate that cleared the gates — an absence of supply, not an absence of edge.",
  venue:
    "Candidates were produced and execution cost or book depth eliminated them. Edge was measured and came in under cost.",
  policy:
    "Candidates were produced and qualified; correlation, the risk budget or the cash reserve deliberately kept them out.",
};

export function idleReasonLabel(v: string | null | undefined): string {
  if (!v) return DASH;
  return IDLE_REASON_LABELS[v] ?? v;
}

export function idleReasonNote(v: string | null | undefined): string | undefined {
  if (!v) return undefined;
  return IDLE_REASON_NOTES[v];
}

export function idleGroup(code: string): IdleGroup | null {
  return IDLE_REASON_GROUP[code] ?? null;
}

// ------------------------------------------------------- missed-opportunity

/**
 * Where the aggregate `missed_net_bps` stops being noise.
 *
 * Both numbers are costs from the Phase 5 brief's own table rather than taste:
 * a Tier-0 round trip is ~2 bps, so anything inside that is smaller than the
 * cost of acting on it; a non-Tier-0 round trip is ~20 bps, so a figure that
 * large means the refusals gave up a whole round trip's worth of move.
 */
export const MISSED_NOISE_BPS = 2;
export const MISSED_ALARM_BPS = 20;

export type MissedVerdictKind = "saved" | "noise" | "leaking" | "bleeding" | "absent";

export interface MissedVerdict {
  kind: MissedVerdictKind;
  /** The verdict in three or four words. */
  text: string;
  /** Why, in one line. */
  note: string;
}

/**
 * Read the aggregate. `missed_net_bps` is what the REFUSED moves went on to
 * do, so its sign is the calibration reading and the sign is easy to invert by
 * accident: negative means the refusals avoided losses and the gate earned its
 * keep; positive means the gate refused money that was there.
 */
export function missedVerdict(value: Numeric): MissedVerdict {
  const n = toNum(value);
  if (n === null) {
    return {
      kind: "absent",
      text: "NOT MEASURED",
      note: "No aggregate missed move was reported. An unmeasured audit is not a clean audit.",
    };
  }
  if (n <= -MISSED_NOISE_BPS) {
    return {
      kind: "saved",
      text: "THE GATE SAVED MONEY",
      note: "The refused moves went negative after costs: these rejections avoided losses rather than missing opportunities.",
    };
  }
  if (Math.abs(n) < MISSED_NOISE_BPS) {
    return {
      kind: "noise",
      text: "WITHIN NOISE",
      note: `The aggregate is below a single Tier-0 round-trip cost (~${MISSED_NOISE_BPS} bps). Chasing this difference does not cost less than the chase itself.`,
    };
  }
  if (n < MISSED_ALARM_BPS) {
    return {
      kind: "leaking",
      text: "THE GATE IS LEAVING MONEY",
      note: "The refused moves went positive after costs: the gate is turning away edge that was genuinely there.",
    };
  }
  return {
    kind: "bleeding",
    text: "THE GATE IS TOO TIGHT",
    note: `The refused moves earned more than a full non-Tier-0 round-trip cost (~${MISSED_ALARM_BPS} bps). At this magnitude, filtering is a loss rather than a protection.`,
  };
}

/** Tone class for a missed-audit verdict. */
export function missedVerdictTone(kind: MissedVerdictKind): string {
  switch (kind) {
    case "saved":
      return "is-saved";
    case "noise":
      return "is-noise";
    case "leaking":
      return "is-leaking";
    case "bleeding":
      return "is-bleeding";
    default:
      return "is-absent";
  }
}

/** Percentage points, as a difference rather than as a level: "75.8 pts". */
export function formatPoints(value: Numeric, digits = 1): string {
  const n = toNum(value);
  if (n === null) return DASH;
  return `${n.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })} pts`;
}

// --------------------------------------------------------------------------
// Phase 6 vocabulary — the funnel, the concentration diagnostic, capital
// recycling and the strategy-specific holding buckets.
//
// The block opens by EXTENDING the Phase 5 idle vocabulary rather than
// starting a second one. There is exactly one idle-capital attribution on this
// screen and it now has nine reasons instead of six; a second panel with its
// own four reasons would let the same idle dollar be counted twice and
// explained differently in two places.
// --------------------------------------------------------------------------

/** `IdleReason` additions frozen in PHASE6_SPEC (`app/portfolio.py`). */
export const IDLE_REASONS_V6: readonly string[] = [
  "COOLDOWN",
  "SCANNER_DELAY",
  "CAPITAL_STATE_BUG",
  "OTHER",
];

Object.assign(IDLE_REASON_LABELS, {
  COOLDOWN: "Cooldown window",
  SCANNER_DELAY: "Scanner delay",
  CAPITAL_STATE_BUG: "Capital-state defect",
  OTHER: "Other",
});

Object.assign(IDLE_REASON_NOTES, {
  COOLDOWN:
    "Entry was deliberately paused after a loss streak or a risk event. This capital is idle because of a decision, not a shortfall.",
  SCANNER_DELAY:
    "Capital was released but the scan cycle had not come back round yet. Whether a candidate existed is unknown — the only thing known is that it was never asked. This is an infrastructure delay, not a market reading.",
  CAPITAL_STATE_BUG:
    "Accounting did not see money as free that should have been free. This is a defect: it says nothing about the market, and until it is fixed it contaminates all eight of the other reasons.",
  OTHER:
    "The worker could not classify this dollar. An unclassified dollar is not an explained dollar; growth in this row means the attribution itself is degrading.",
});

/**
 * The fourth group: money that is idle because WE are broken.
 *
 * Phase 5 had three groups and each of them was a statement about the market
 * or about a portfolio decision. `SCANNER_DELAY` and `CAPITAL_STATE_BUG` are
 * neither — they say the measurement apparatus lost the money, which is a
 * defect report and demands a different response from "the universe is too
 * narrow". Folding them into `policy` would let a bug read as a decision.
 */
Object.assign(IDLE_REASON_GROUP, {
  COOLDOWN: "policy",
  SCANNER_DELAY: "defect",
  CAPITAL_STATE_BUG: "defect",
  // OTHER stays deliberately UNGROUPED. It is the residue bucket and grouping
  // it would claim a diagnosis the worker explicitly declined to make.
});

/** The nine reasons, Phase 5's six first so the existing order is preserved. */
export const IDLE_REASONS_ALL: readonly string[] = [
  ...IDLE_REASONS,
  ...IDLE_REASONS_V6,
];

// ------------------------------------------------------------------ funnel

/** `STAGES` from `app/research/funnel.py` — frozen, in ladder order. */
export const FUNNEL_STAGE_LABELS: Record<string, string> = {
  discovered: "Listed on the venue",
  subscribed: "Data subscribed",
  data_healthy: "Data healthy",
  features: "Features computed",
  strategy_evaluated: "Strategy evaluated",
  model_evaluated: "Model evaluated",
  candidate: "Became a candidate",
  cost_gate: "Cost gate",
  liquidity_gate: "Liquidity gate",
  risk_gate: "Risk gate",
  correlation_gate: "Correlation gate",
  ranked: "Ranked",
  selected: "Selected",
  traded: "Traded",
};

export function funnelStageLabel(v: string | null | undefined): string {
  if (!v) return DASH;
  return FUNNEL_STAGE_LABELS[v] ?? v;
}

// --------------------------------------------------------- concentration

/**
 * The diagnostic's vocabulary.
 *
 * "NOT ACTIVE" is the resting state and it is a measurement, not a blank: it
 * means the share was computed over a real sample and came in under the
 * threshold. It is worded and toned as a finished reading for exactly that
 * reason — today BTC is 41.7% of 139 trades and this flag should look
 * deliberately quiet, not broken.
 */
export const CONCENTRATION_VERDICT_LABELS: Record<string, string> = {
  flagged: "BTC_CONCENTRATION · ACTIVE",
  clear: "BTC_CONCENTRATION · NOT ACTIVE",
  thin: "BTC_CONCENTRATION · INSUFFICIENT SAMPLE",
  unmeasured: "BTC_CONCENTRATION · NOT MEASURED",
};

export const CONCENTRATION_VERDICT_NOTES: Record<string, string> = {
  flagged:
    "A single asset took more than 70% of the trades. This is a diagnosis: the flag suppresses nothing, penalises no asset and does not change the ranking. If BTC wins on net EV, BTC trades. All the flag asks is that the reason be read off the funnel.",
  clear:
    "The leading asset's share is under the threshold — measured, and it passed. If concentration exists it is not in the ranking but in what was allowed to become a candidate, and this panel's funnel columns are what say so.",
  thin:
    "The share was computed but the sample is too small to conclude anything from. A 100% share across a handful of trades is a small sample, not concentration; the two are not interchangeable here.",
  unmeasured:
    "The share was not computed: neither the trade count nor the funnel reached this screen. An unmeasured concentration is not an absent one.",
};

export function concentrationTone(kind: string): string {
  switch (kind) {
    case "flagged":
      return "is-flagged";
    case "clear":
      return "is-clear";
    case "thin":
      return "is-thin";
    default:
      return "is-unmeasured";
  }
}

// ------------------------------------------------------------- recycling

/**
 * How fast capital has to come back to work before the delay is the strategy.
 *
 * A scalper whose median hold is ~32 s and which then leaves the freed cash
 * idle for 40 s has halved its own turnover without taking a single extra
 * risk. So the bands are set against the hold, not against a round number:
 * under 2 s is instant, under 10 s is acceptable, past a minute the recycling
 * delay is costing more than most of the trades earn.
 */
export const RECYCLE_FAST_MS = 2_000;
export const RECYCLE_SLOW_MS = 10_000;
export const RECYCLE_BAD_MS = 60_000;

export function recycleTone(ms: Numeric): string {
  const n = toNum(ms);
  if (n === null) return "is-empty";
  if (n <= RECYCLE_FAST_MS) return "is-fast";
  if (n <= RECYCLE_SLOW_MS) return "is-ok";
  if (n <= RECYCLE_BAD_MS) return "is-slow";
  return "is-bad";
}

/** Milliseconds, at millisecond precision — this is the one place on the
    screen where a sub-second figure is the measurement and not noise. */
export function formatExactMs(value: Numeric): string {
  const n = toNum(value);
  if (n === null) return DASH;
  if (Math.abs(n) < 10_000) {
    return `${Math.round(n).toLocaleString("en-US")} ms`;
  }
  return formatMs(n);
}

/** Seconds, to one decimal — the close→entry figure the brief asks for. */
export function formatSeconds(value: Numeric, digits = 1): string {
  const n = toNum(value);
  if (n === null) return DASH;
  return `${n.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}s`;
}

// -------------------------------------------------------- opportunity board

export const OPPORTUNITY_STATUS_LABELS: Record<string, string> = {
  QUEUED: "QUEUED",
  SELECTED: "SELECTED",
  EXPIRED: "EXPIRED",
  REJECT: "REJECTED",
};

export function opportunityStatusLabel(v: string | null | undefined): string {
  if (!v) return DASH;
  return OPPORTUNITY_STATUS_LABELS[String(v).toUpperCase()] ?? v;
}

export function opportunityStatusTone(v: string | null | undefined): string {
  switch (String(v ?? "").toUpperCase()) {
    case "SELECTED":
      return "is-selected";
    case "QUEUED":
      return "is-queued";
    case "EXPIRED":
      return "is-expired";
    case "REJECT":
      return "is-reject";
    default:
      return "is-unknown";
  }
}
