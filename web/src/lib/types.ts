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
  | "RISK_EXIT";

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
  | "NOT_SUPPORTED_SHORT";

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

export interface PositionRow {
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
}

export interface TradeRow {
  id: string;
  symbol: string;
  strategy: string | null;
  entry_ms: Num;
  exit_ms: Num;
  entry_price: Num;
  exit_price: Num;
  gross_pnl_usd: Num;
  fees_usd: Num;
  slippage_usd: Num;
  net_pnl_usd: Num;
  exit_reason: ExitReasonCode | string | null;
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
