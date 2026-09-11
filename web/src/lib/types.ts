/**
 * The dashboard payload contract.
 *
 * These types mirror `api/app/types.py` as it is projected by
 * `GET /cryptobot/api/dashboard`. The rules that matter when reading them:
 *
 *   * All timestamps are epoch **milliseconds** and end in `_ms`.
 *   * `bps` is basis points (1 bps = 0.01%); money is USD.
 *   * Any figure the worker has not measured arrives as `null`, never as 0.
 *     The UI renders null as an em dash — a missing measurement and a zero
 *     measurement are different statements and are never conflated.
 */

/** A number the API may legitimately not have yet. */
export type Num = number | null;

export type Side = "LONG" | "SHORT";

export type DecisionCode = "BUY" | "SELL" | "WATCH" | "NO_TRADE";

export type RegimeCode =
  | "TRENDING"
  | "RANGING"
  | "BREAKOUT"
  | "HIGH_VOLATILITY"
  | "LOW_VOLATILITY"
  | "ABNORMAL"
  | "UNKNOWN";

export type ExitReasonCode =
  | "TAKE_PROFIT"
  | "TRAILING_PROFIT"
  | "STOP_LOSS"
  | "TIME_STOP"
  | "SIGNAL_REVERSAL"
  | "RISK_EXIT"
  // V2 — the 25-minute checkpoint may retire a position that is still alive.
  | "EXIT_WEAK"
  | "ROTATE_CAPITAL";

export type RejectReasonCode =
  | "STALE_BOOK"
  | "WIDE_SPREAD"
  | "THIN_DEPTH"
  | "NEGATIVE_EDGE"
  | "EDGE_BELOW_MIN"
  | "RISK_BUDGET"
  | "MAX_POSITIONS"
  | "DUPLICATE_SYMBOL"
  | "DAILY_LOSS_LIMIT"
  | "WEEKLY_LOSS_LIMIT"
  | "LOSS_STREAK_COOLDOWN"
  | "ABNORMAL_VOLATILITY"
  | "DATA_FAILURE"
  | "KILL_SWITCH"
  | "BELOW_MIN_NOTIONAL"
  | "BAD_REWARD_RISK"
  | "SLIPPAGE_TOO_HIGH"
  | "NOT_SUPPORTED_SHORT"
  // V2 — refusals that come from the net gate and the portfolio layer.
  | "NET_TARGET_UNREACHABLE"
  | "CASH_RESERVE"
  | "PORTFOLIO_RISK_BUDGET"
  | "CORRELATED_EXPOSURE"
  | "DIRECTIONAL_EXPOSURE";

/**
 * How the entry was (or is expected to be) filled. Binance.US charges 0% maker,
 * so this is not a label — it is the single biggest term in the cost stack.
 */
export type MakerTaker = "MAKER" | "TAKER";

/**
 * Where a position stands against its 25-minute checkpoint.
 *   PENDING — not yet reached; KEEP / TRAIL — reassessed and kept;
 *   DUE     — past 25 minutes and the checkpoint has not run.
 */
export type CheckpointStatus = "PENDING" | "KEEP" | "TRAIL" | "DUE";

export interface DashboardHeader {
  starting_equity: Num;
  data_connected: boolean;
}

export interface DashboardMetrics {
  equity: Num;
  cash: Num;
  pnl_today: Num;
  pnl_total: Num;
  open_positions: Num;
  trades: Num;
  /** Rate in [0, 1]. */
  win_rate: Num;
  profit_factor: Num;
  /** USD per trade. */
  expectancy: Num;
  /** USD, reported as a positive magnitude. */
  max_drawdown: Num;
  /** Traded notional / starting equity. */
  capital_turnover: Num;
  avg_holding_seconds: Num;

  // ---- V2 ------------------------------------------------------------------
  // The three values that must never be collapsed into one. `real_net_profit`
  // is the only one of them that is profit; the other two exist so the
  // operator can see what the price move earned and what capturing it cost.
  /** Sum of gross P&L — what the price moves earned, before any cost. */
  profit_before_costs?: Num;
  /** Fees + spread + slippage, reported as a positive magnitude. */
  total_costs?: Num;
  /** What actually landed in the account. */
  real_net_profit?: Num;
}

export interface ScannerRow {
  symbol: string;
  price: Num;
  long_score: Num;
  short_score: Num;
  regime: RegimeCode | string | null;
  /** Momentum in bps (the ranker's own composite return reading). */
  momentum: Num;
  /** Last minute's quote volume over its trailing mean. */
  volume_ratio: Num;
  spread_bps: Num;
  /** Order-book imbalance in [-1, 1]. */
  imbalance: Num;
  net_edge_bps: Num;
  strategy: string | null;
  decision: DecisionCode | string | null;
  reject_reason: RejectReasonCode | string | null;
}

/**
 * The V2 per-trade cost stack.
 *
 * Every member is optional: the worker that fills these is being extended
 * separately, so the API serves V1-shaped rows until that lands and the UI
 * must render an em dash rather than inventing a zero. The identity the
 * dashboard asserts is
 *
 *     gross_pnl_usd − total_cost_usd = net_pnl_usd
 *
 * and the three terms are always shown separately, never netted for the
 * operator.
 */
export interface CostBreakdown {
  /** What the price move earned, before any cost. Never a profit figure. */
  gross_pnl_usd?: Num;
  entry_fee_usd?: Num;
  /** Open positions: the fee the exit is modelled to cost. */
  expected_exit_fee_usd?: Num;
  /** Closed trades: the fee the exit actually cost. */
  exit_fee_usd?: Num;
  spread_cost_usd?: Num;
  slippage_usd?: Num;
  /** Fees + spread + slippage, positive magnitude. */
  total_cost_usd?: Num;
  /** Gross minus every cost above — the only figure that is profit. */
  net_pnl_usd?: Num;
  /** How the entry filled: 0% maker vs a taker fee is most of the stack. */
  maker_or_taker?: MakerTaker | string | null;
  /** The price at which the round trip has paid for itself. */
  break_even_price?: Num;
  /** The price whose NET result meets the active profile's target. */
  net_tp_price?: Num;
  /** That target as a percentage of the position (1.0 === 1%). */
  net_tp_pct?: Num;
}

export interface PositionCore {
  id: string;
  symbol: string;
  side: Side | string;
  entry_price: Num;
  current_price: Num;
  notional_usd: Num;
  risk_usd: Num;
  take_profit: Num;
  stop_loss: Num;
  unrealized_usd: Num;
  fees_usd: Num;
  holding_seconds: Num;
  strategy: string | null;

  // ---- V2 ------------------------------------------------------------------
  /** Modelled loss if the stop fills — NOT the position size. */
  account_risk_usd?: Num;
  /** Seconds since entry. Falls back to `holding_seconds` when absent. */
  age_seconds?: Num;
  /** When the position opened. Derived from age when the worker omits it. */
  entry_ms?: Num;
  /** The horizon the entry was taken for: a label ("25–60 dk") or seconds. */
  expected_hold_window?: string | number | null;
  checkpoint_status?: CheckpointStatus | string | null;
  /** Epoch ms at which the 60-minute time stop closes this position. */
  time_stop_ms?: Num;
  /** Ranker score behind the entry: a 0–1 fraction or a 0–100 reading. */
  signal_strength?: Num;
}

export interface PositionRow extends PositionCore, CostBreakdown {}

export interface TradeCore {
  id: string;
  symbol: string;
  strategy: string | null;
  entry_ms: Num;
  exit_ms: Num;
  entry_price: Num;
  exit_price: Num;
  exit_reason: ExitReasonCode | string | null;

  // ---- V2 ------------------------------------------------------------------
  // The cost stack arrives via CostBreakdown below. These two are not in the
  // frozen additions list but section G asks for position size and account
  // risk per completed trade, so they are read when the worker supplies them.
  notional_usd?: Num;
  risk_usd?: Num;
  /** The stop the trade actually carried. */
  stop_loss?: Num;
}

/**
 * A closed round trip. `gross_pnl_usd`, `slippage_usd` and `net_pnl_usd` are
 * V1 columns and are always present; `fees_usd` is the V1 total fee and stays
 * the fallback whenever the entry/exit split has not been reported.
 */
export interface TradeRow extends TradeCore, CostBreakdown {
  gross_pnl_usd: Num;
  fees_usd: Num;
  slippage_usd: Num;
  net_pnl_usd: Num;
}

/**
 * The portfolio layer's own state. V2 removed the 1–3 concurrent-position cap,
 * so "how much of the account is at work, and is it really one bet" became a
 * reading in its own right rather than a consequence of the position count.
 */
export interface PortfolioState {
  open_positions: Num;
  /** Notional currently at work. */
  capital_deployed: Num;
  available_cash: Num;
  /** Cash held back on purpose — never deployed just because signals exist. */
  cash_reserve: Num;
  /** Sum of modelled loss across open stops. */
  open_risk_usd: Num;
  /** The same as a percentage of equity (1.0 === 1%). */
  open_risk_pct: Num;
  /** How much of the open exposure is one correlated bet (1.0 === 1%). */
  correlated_exposure_pct: Num;
  avg_position_usd: Num;
  median_position_usd: Num;
  avg_holding_seconds: Num;
  positions_under_25m: Num;
  positions_25_60m: Num;
  positions_over_60m: Num;
  /** Traded notional per day / equity. */
  capital_turnover_per_day: Num;
  /** NET profit earned per dollar of capital put to work. */
  net_profit_per_dollar_deployed: Num;
  /** FEW_LARGE / MANY_SMALL / DYNAMIC, as the worker names it. */
  allocation_mode: string | null;
  /** MICRO / BASE / FAST / EXTENDED. */
  net_tp_profile: string | null;
}

export interface StrategyRow {
  strategy: string;
  trades: Num;
  win_rate: Num;
  avg_win: Num;
  avg_loss: Num;
  profit_factor: Num;
  expectancy: Num;
  fees: Num;
  net_pnl: Num;
  max_drawdown: Num;
  avg_holding_seconds: Num;
  capital_turnover: Num;
}

/** `[epoch_ms, value]`. */
export type SeriesPoint = [number, number];
/** `[epoch_ms, simulator_version]` — where the simulator's rules changed. */
export type VersionBoundary = [number, string];

export interface EquityCurve {
  points: SeriesPoint[];
  realized: SeriesPoint[];
  version_boundaries: VersionBoundary[];
}

export interface HealthState {
  ws_connected: boolean;
  rest_ok: boolean;
  book_age_ms: Num;
  updates_per_sec: Num;
  reconnects: Num;
  dropped: Num;
  worker_cpu_pct: Num;
  worker_mem_mb: Num;
  db_ok: boolean;
  last_evaluation_ms: Num;
  mode: string | null;
}

export interface LedgerRef {
  name: string;
  label: string;
  starting_equity: Num;
  equity: Num;
}

export interface DashboardPayload {
  mode: string | null;
  live_orders_enabled: boolean;
  simulator_version: string | null;
  exchange: string | null;
  /** When the worker built this payload. */
  generated_ms: Num;
  header: DashboardHeader;
  metrics: DashboardMetrics;
  scanner: ScannerRow[];
  positions: PositionRow[];
  trades: TradeRow[];
  /** V2 portfolio layer. Absent on a V1-shaped payload. */
  portfolio?: PortfolioState | null;
  strategies: StrategyRow[];
  equity_curve: EquityCurve;
  health: HealthState;
  ledgers: LedgerRef[];
}

export interface HealthResponse {
  status: string;
}

export interface AuthMeResponse {
  authenticated: boolean;
  email?: string | null;
}
