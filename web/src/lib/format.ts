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
    return `${(n / 1000).toLocaleString("en-US", { maximumFractionDigits: 2 })} sn`;
  return `${(n / 60000).toLocaleString("en-US", { maximumFractionDigits: 1 })} dk`;
}

/** A holding time as a duration: 42 sn · 7 dk 12 sn · 1 sa 03 dk. */
export function formatDuration(seconds: Numeric): string {
  const n = toNum(seconds);
  if (n === null) return DASH;
  const s = Math.max(0, Math.round(n));
  if (s < 60) return `${s} sn`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} dk ${String(s % 60).padStart(2, "0")} sn`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} sa ${String(m % 60).padStart(2, "0")} dk`;
  return `${Math.floor(h / 24)} g ${String(h % 24).padStart(2, "0")} sa`;
}

/** An epoch-ms timestamp as a wall clock, to the second. */
export function formatClockMs(value: Numeric): string {
  const n = toNum(value);
  if (n === null || n <= 0) return DASH;
  return new Date(n).toLocaleTimeString("tr-TR", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function formatDateTimeMs(value: Numeric): string {
  const n = toNum(value);
  if (n === null || n <= 0) return DASH;
  return new Date(n).toLocaleString("tr-TR", {
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
  if (sec < 2) return "şimdi";
  if (sec < 60) return `${sec} sn önce`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} dk önce`;
  const hour = Math.floor(min / 60);
  if (hour < 24) return `${hour} sa önce`;
  return `${Math.floor(hour / 24)} gün önce`;
}

/** Volume multiples read better as "1,8×" than as a bare number. */
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
// Turkish label here; the raw code always survives in a `title` attribute so
// an operator reading a log and an operator reading the screen see the same
// system.
// --------------------------------------------------------------------------

export const SIDE_LABELS: Record<string, string> = {
  LONG: "UZUN",
  SHORT: "KISA",
};

export const DECISION_LABELS: Record<string, string> = {
  BUY: "AL",
  SELL: "SAT",
  WATCH: "İZLE",
  NO_TRADE: "İŞLEM YOK",
};

export const REGIME_LABELS: Record<string, string> = {
  TRENDING: "TRENDLİ",
  RANGING: "YATAY",
  BREAKOUT: "KIRILIM",
  HIGH_VOLATILITY: "YÜKSEK OYNAKLIK",
  LOW_VOLATILITY: "DÜŞÜK OYNAKLIK",
  ABNORMAL: "ANORMAL",
  UNKNOWN: "BİLİNMİYOR",
};

export const EXIT_REASON_LABELS: Record<string, string> = {
  TAKE_PROFIT: "KÂR AL",
  TRAILING_PROFIT: "TAKİPLİ KÂR",
  STOP_LOSS: "ZARAR KES",
  TIME_STOP: "SÜRE DOLDU",
  SIGNAL_REVERSAL: "SİNYAL DÖNDÜ",
  RISK_EXIT: "RİSK ÇIKIŞI",
};

/** Why the risk layer refused a candidate (RejectReason in types.py). */
export const REJECT_REASON_LABELS: Record<string, string> = {
  STALE_BOOK: "Emir defteri bayat",
  WIDE_SPREAD: "Makas çok geniş",
  THIN_DEPTH: "Derinlik yetersiz",
  NEGATIVE_EDGE: "Net edge negatif",
  EDGE_BELOW_MIN: "Edge alt sınırın altında",
  RISK_BUDGET: "Risk bütçesi dolu",
  MAX_POSITIONS: "Azami pozisyon sayısı",
  DUPLICATE_SYMBOL: "Bu coinde zaten pozisyon var",
  DAILY_LOSS_LIMIT: "Günlük zarar limiti",
  WEEKLY_LOSS_LIMIT: "Haftalık zarar limiti",
  LOSS_STREAK_COOLDOWN: "Zarar serisi soğuması",
  ABNORMAL_VOLATILITY: "Anormal oynaklık",
  DATA_FAILURE: "Veri hatası",
  KILL_SWITCH: "Acil durdurma açık",
  BELOW_MIN_NOTIONAL: "Asgari tutarın altında",
  BAD_REWARD_RISK: "Getiri/risk oranı yetersiz",
  SLIPPAGE_TOO_HIGH: "Kayma çok yüksek",
  NOT_SUPPORTED_SHORT: "Kısa yön V1'de kapalı",
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
export function baseAsset(symbol: string): string {
  return symbol.replace(/(USDT|USDC|USD)$/i, "") || symbol;
}

// --------------------------------------------------------------------------
// V2 vocabulary — fee-aware net targets, maker-first execution, the 25/60
// minute horizon and the portfolio layer.
// --------------------------------------------------------------------------

export const EXIT_REASON_LABELS_V2: Record<string, string> = {
  EXIT_WEAK: "ZAYIF ÇIKIŞ",
  ROTATE_CAPITAL: "SERMAYE ROTASYONU",
};

Object.assign(EXIT_REASON_LABELS, EXIT_REASON_LABELS_V2);

Object.assign(REJECT_REASON_LABELS, {
  NET_TARGET_UNREACHABLE: "NET hedefe ulaşılamaz",
  CASH_RESERVE: "Nakit rezervi korunuyor",
  PORTFOLIO_RISK_BUDGET: "Portföy risk bütçesi dolu",
  CORRELATED_EXPOSURE: "Korelasyonlu maruziyet sınırı",
  DIRECTIONAL_EXPOSURE: "Yönlü maruziyet sınırı",
});

/** MAKER is 0% on Binance.US — the badge is a cost statement, not a label. */
export const MAKER_TAKER_LABELS: Record<string, string> = {
  MAKER: "MAKER",
  TAKER: "TAKER",
};

export const CHECKPOINT_LABELS: Record<string, string> = {
  PENDING: "BEKLEMEDE",
  KEEP: "TUT",
  TRAIL: "TAKİPLİ",
  DUE: "KONTROL ZAMANI",
};

export const ALLOCATION_MODE_LABELS: Record<string, string> = {
  FEW_LARGE: "AZ SAYIDA BÜYÜK",
  MANY_SMALL: "ÇOK SAYIDA KÜÇÜK",
  DYNAMIC: "DİNAMİK",
};

export const NET_TP_PROFILE_LABELS: Record<string, string> = {
  MICRO: "MICRO · %0,25",
  BASE: "BASE · %0,50",
  FAST: "FAST · %0,75",
  EXTENDED: "EXTENDED · %1,00",
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
 * The expected hold window may arrive as a label the worker chose ("25–60 dk")
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
  if (left <= 0) return "süre doldu";
  return `${formatDuration(left)} kaldı`;
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
