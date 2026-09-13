/**
 * The dashboard payload contract.
 *
 * These types mirror `api/app/types.py` as it is projected by
 * `GET /edge-after-costs/api/dashboard`. The rules that matter when reading them:
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
  /**
   * How long the round trip was held. The worker reports it directly; when it
   * does not, the `exit_ms − entry_ms` span is the fallback. Phase 6 reads
   * this per strategy and never pools it — see `lib/phase6.ts`.
   */
  holding_seconds?: Num;
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

  // ---- Phase 3 -------------------------------------------------------------
  // The tape recorder is research infrastructure, not trading infrastructure:
  // it is how the maker study gets sub-minute data a 1-minute bar cannot hold.
  // Both are optional and absent on a V2-shaped payload.
  recorder?: RecorderState | null;
  tape?: TapeState | null;
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

  // ---- Phase 3 -------------------------------------------------------------
  /** The strategy search. Absent until the research worker serves it. */
  tournament?: TournamentSummary | null;
  /**
   * Candidates that reached a gate report. An EMPTY array is the expected
   * outcome and is rendered as a statement, not as an empty table.
   */
  finalists?: FinalistRow[] | null;
  /** What is running in live paper. `null` is a legitimate answer. */
  champion?: ChampionState | null;

  // ---- Phase 7: liveness ---------------------------------------------------
  /**
   * Proof the scanner is running, in the form an operator can read. Added
   * after the dashboard was reported dead while the worker was evaluating
   * about once a second: "evaluations: 1080" buried in a health blob does not
   * answer "is this thing alive", and a rate plus an age does.
   */
  scan?: ScanState | null;
  /**
   * The pairs on the bench and the number that benched them. The bot had
   * ranked six pairs and benched five; the ranking was invisible, so the
   * screen was indistinguishable from one showing no scanning at all.
   */
  watchlist?: WatchlistState | null;

  // ---- Phase 4 -------------------------------------------------------------
  /**
   * The predictive entry engine. Absent until the worker serves it, and that
   * is the expected state — Phase 3 ended with no champion, so there may
   * never be a model here at all. The panels say so rather than waiting.
   */
  predictive?: PredictiveState | null;

  // ---- Phase 5 -------------------------------------------------------------
  /**
   * Capital utilisation and the idle-capital attribution. Absent until the
   * portfolio layer is extended to serve it; the panels render that absence
   * as "not yet measured" rather than as a fully idle book.
   */
  capital?: CapitalState | null;
  /** The post-hoc missed-opportunity audit. Never a live signal. */
  missed?: MissedAudit | null;

  // ---- Phase 6 -------------------------------------------------------------
  /**
   * Multi-asset selection and capital recycling. All four are absent until the
   * worker is extended; each panel derives what it honestly can from the keys
   * above and says, in its own footer, which figures were derived and which
   * were measured. See `lib/phase6.ts`.
   */
  /** Per-symbol funnel counts — the only evidence for WHY a coin never trades. */
  funnel?: FunnelReport | null;
  /** Per-coin trades, P&L, signals and capital, plus the diagnostic flag. */
  concentration?: ConcentrationState | null;
  /** Close → capital released → rescan → next entry, in milliseconds. */
  recycling?: RecyclingState | null;
  /** Net performance by holding bucket, BY STRATEGY, never pooled. */
  holding_buckets?: HoldingBucketState | null;
  /** The live opportunity board the allocator actually chooses from. */
  opportunities?: OpportunityBoard | null;
}

export interface HealthResponse {
  status: string;
}

export interface AuthMeResponse {
  authenticated: boolean;
  email?: string | null;
}

// ===========================================================================
// PHASE 3 — the profitability search: tournament, robustness gates, champion
// ---------------------------------------------------------------------------
// Every field below is OPTIONAL. The research worker is being extended
// concurrently and serves V2-shaped payloads until that lands, so each panel
// has to read correctly against a payload that carries none of these keys.
// The rule from V1 and V2 does not change here: an absent measurement renders
// as an em dash, never as a zero, and never as a result.
//
// The one distinction this section exists to protect: an IN-SAMPLE number is
// not a result. It is the number the parameters were chosen on. It is carried
// in its own object, rendered in its own column group, and never mixed into
// the out-of-sample reading that the promotion gates actually judge.
// ===========================================================================

/**
 * Where an arm stands against the out-of-sample promotion gates.
 *   FINALIST            — passed every gate out-of-sample
 *   MARGINAL            — positive but below the 1.20 profit-factor bar
 *   FAILED              — did not pass
 *   INSUFFICIENT_SAMPLE — fewer than 30 out-of-sample trades; not yet judged
 */
export type OosStatus = "FINALIST" | "MARGINAL" | "FAILED" | "INSUFFICIENT_SAMPLE";

/**
 * One half of a walk-forward split. `in_sample` is where the parameters were
 * fitted; `out_of_sample` is the only half a promotion decision may read.
 */
export interface SampleStats {
  trades?: Num;
  profit_factor?: Num;
  expectancy?: Num;
  net_pnl?: Num;
}

/** A single candidate in the tournament — one family × coin × regime × horizon. */
export interface TournamentArm {
  key?: string | null;
  family?: string | null;
  symbol?: string | null;
  regime?: string | null;
  horizon_seconds?: Num;
  exit_profile?: string | null;
  stop_profile?: string | null;

  // The arm's full-sample record. Useful as the raw research log; it is NOT
  // what the gates read, and the table says so above these columns.
  trades?: Num;
  win_rate?: Num;
  avg_win?: Num;
  avg_loss?: Num;
  profit_factor?: Num;
  expectancy?: Num;
  fees?: Num;
  slippage?: Num;
  net_pnl?: Num;
  max_drawdown?: Num;

  in_sample?: SampleStats | null;
  out_of_sample?: SampleStats | null;
  oos_status?: OosStatus | string | null;
}

export interface TournamentSummary {
  /** When the search finished. */
  generated_ms?: Num;
  candidates_evaluated?: Num;
  wall_clock_seconds?: Num;
  /** The data window the search ran over, as the worker names it. */
  period?: string | null;
  /** The fee assumption the whole run was costed at. */
  fee_bps?: Num;
  arms?: TournamentArm[] | null;
}

/** One cost-stress replay: the same arm re-run under a harsher assumption. */
export interface StressResult {
  label?: string | null;
  survived?: boolean | null;
  net_pnl?: Num;
}

export interface MonteCarloResult {
  median_drawdown?: Num;
  p95_drawdown?: Num;
  worst_loss_streak?: Num;
  /** Probability of ending below the starting equity, in [0, 1]. */
  prob_below_start?: Num;
  samples?: Num;
}

/**
 * A candidate that reached the gate report stage. `reasons` carries one line
 * per gate — pass or fail — written by `robustness.evaluate_gates`.
 */
export interface FinalistRow {
  key?: string | null;
  family?: string | null;
  symbol?: string | null;
  regime?: string | null;
  horizon_seconds?: Num;
  passed?: boolean | null;
  reasons?: string[] | null;
  stress?: StressResult[] | null;
  monte_carlo?: MonteCarloResult | null;
}

/**
 * The strategy currently running in live paper, if any.
 *
 * `oos_profit_factor` and `live_profit_factor` are two different measurements
 * of the same strategy and they are expected to disagree. The dashboard shows
 * them side by side rather than picking one, because the size of the gap is
 * the reading.
 */
export interface ChampionState {
  strategy?: string | null;
  version?: string | null;
  started_ms?: Num;
  paper_equity?: Num;
  oos_profit_factor?: Num;
  live_profit_factor?: Num;
  expectancy?: Num;
  max_drawdown?: Num;
  trades?: Num;
}

/** The tape recorder feeding the maker study. */
export interface RecorderState {
  enabled?: boolean | null;
  messages?: Num;
  mb_written?: Num;
  /** Why it is off, when it is off. */
  disabled_reason?: string | null;
}

/** What has actually been written to disk so far. */
export interface TapeState {
  files?: Num;
  mb?: Num;
  /** Hours of tape collected — the maker study's progress bar. */
  hours?: Num;
  /** Epoch ms of the oldest / newest retained hour. */
  oldest?: Num;
  newest?: Num;
}

// ===========================================================================
// PHASE 4 — the predictive entry engine
// ---------------------------------------------------------------------------
// Every field below is OPTIONAL and the whole `predictive` key is expected to
// be absent for most of this phase: the worker that fills it is being built
// concurrently, and Phase 3 ended with no champion at all. So the absent case
// is the design case, not an edge case, and each panel renders it as a
// statement rather than as an empty table.
//
// The one distinction this section exists to protect: a probability from an
// UNCALIBRATED model is a number, not a probability. `calibration` is what
// decides which of the two the screen is allowed to call it, and no panel
// here renders a confidence figure that the calibration block has not earned.
// ===========================================================================

/** Why the predictive engine refused a candidate (Phase 4 spec, item 34). */
export type PredictiveRejectReasonCode =
  | "LOW_PROBABILITY"
  | "NEGATIVE_EV"
  | "COST_TOO_HIGH"
  | "MODEL_UNCERTAIN"
  | "CORRELATED_RISK"
  | "SPREAD_TOO_WIDE"
  | "NO_LIQUIDITY"
  | "STALE_DATA"
  | "REGIME_NOT_SUPPORTED";

/** `[start_ms, end_ms]` — one leg of the chronological split. */
export type PeriodMs = [number, number];

/**
 * What `calibrate.evaluate_calibration` measured.
 *
 * `max_abs_error` is the promotion gate the Phase 4 spec names: a maximum
 * absolute reliability error above 0.10 means a 70% prediction does not win
 * 70% of the time, and the dashboard refuses to print a confidence figure
 * derived from it.
 */
export interface PredictiveCalibration {
  brier?: Num;
  log_loss?: Num;
  auc?: Num;
  pr_auc?: Num;
  /** Largest gap between predicted and realised frequency across the bins. */
  max_abs_error?: Num;
  calibrated?: boolean | null;
  /** "isotonic" | "platt" | "none", as the research module names it. */
  method?: string | null;
  status?: string | null;
}

/** The champion artifact's out-of-sample record — the only half that judges. */
export interface PredictiveOos {
  trades?: Num;
  win_rate?: Num;
  profit_factor?: Num;
  expectancy?: Num;
  net_pnl?: Num;
  /** USD, reported as a positive magnitude. */
  max_drawdown?: Num;
}

/**
 * One live candidate as the entry engine scored it.
 *
 * `confidence_known` is separate from `confidence` on purpose: a model that
 * cannot say how sure it is reports that, and the UI must not read a missing
 * confidence as a low one or a present one as a trustworthy one.
 */
export interface PredictiveCandidate {
  symbol?: string | null;
  regime?: string | null;
  model?: string | null;
  /** P(take profit first), in [0, 1]. */
  p_tp?: Num;
  /** P(stop first), in [0, 1]. */
  p_sl?: Num;
  /** P(neither barrier inside the horizon), in [0, 1]. */
  p_timeout?: Num;
  /** USD the win branch is modelled to net. */
  expected_net_win?: Num;
  /** USD the loss branch is modelled to cost. May arrive as a magnitude. */
  expected_net_loss?: Num;
  /** p·win − p·loss − cost. The column the decision is actually made on. */
  expected_value?: Num;
  execution_cost?: Num;
  confidence?: Num;
  confidence_known?: boolean | null;
  decision?: DecisionCode | string | null;
  reject_reason?: PredictiveRejectReasonCode | string | null;
}

/**
 * The stored model and risk values behind one paper decision.
 *
 * This is a record, not a narrative. Every token the explanation panel prints
 * comes from a field here; nothing on that panel is composed prose.
 */
export interface PredictiveExplanation {
  trade_id?: string | null;
  symbol?: string | null;
  side?: Side | string | null;
  decision?: string | null;
  /** P(profitable NET exit before the stop), in [0, 1]. */
  p_win?: Num;
  expected_net_win?: Num;
  expected_net_loss?: Num;
  expected_value?: Num;
  regime?: string | null;
  spread_ok?: boolean | null;
  /** A label ("STRONG") or a measured depth reading, as the worker sends it. */
  liquidity?: string | number | null;
  execution_style?: MakerTaker | string | null;
  model_version?: string | null;
  ts_ms?: Num;
}

/**
 * `{REASON: count}`. Kept as an open record rather than a closed map so a
 * reason the worker adds later still appears on screen instead of vanishing.
 */
export type RejectionTally = Record<string, number | null | undefined>;

export interface PredictiveState {
  model_version?: string | null;
  model_name?: string | null;
  feature_version?: string | null;
  /** The `BarrierSpec.key()` the model was labelled against. */
  spec_key?: string | null;
  /** The last bar the model was allowed to see. */
  trained_to_ms?: Num;
  train_period?: PeriodMs | null;
  validate_period?: PeriodMs | null;
  /** Touched once, and last. */
  test_period?: PeriodMs | null;
  probability_threshold?: Num;
  ev_threshold_usd?: Num;
  calibration?: PredictiveCalibration | null;
  oos?: PredictiveOos | null;
  candidates?: PredictiveCandidate[] | null;
  rejections?: RejectionTally | null;
  explanations?: PredictiveExplanation[] | null;
}

// ===========================================================================
// PHASE 5 — capital utilisation and the idle-capital attribution
// ---------------------------------------------------------------------------
// Every field below is OPTIONAL and the whole `capital` / `missed` pair is
// expected to be absent until the worker that fills them lands. The rule from
// V1 does not change: an absent measurement renders as an em dash, never as a
// zero — "no idle capital" and "idle capital was never attributed" are
// opposite statements and the panels keep them apart.
//
// The distinction this section exists to protect: the six idle reasons do not
// mean the same kind of thing. `NO_QUALIFIED_SIGNAL` says there was nothing
// to buy — a wider universe is the fix. `EXECUTION_COST` says there were
// candidates and they were not worth their own round trip — a wider universe
// makes that strictly worse, because every non-Tier-0 pair costs ~20 bps
// against ~2 bps on Tier-0. Phase 5 is settled by which of the two dominates,
// so the dashboard never shows them as six interchangeable slices.
// ===========================================================================

/** `IdleReason` in `app/portfolio.py`. */
export type IdleReasonCode =
  | "NO_QUALIFIED_SIGNAL"
  | "CORRELATION_LIMIT"
  | "RISK_LIMIT"
  | "LIQUIDITY_LIMIT"
  | "EXECUTION_COST"
  | "CASH_RESERVE";

/**
 * `{IdleReason: usd}` — **dollars, not percentages of a percentage.**
 *
 * Kept as an open record rather than a closed map, like `RejectionTally`, so a
 * reason the worker adds later still reaches the screen instead of vanishing.
 * The contract guarantees these sum to `idle_usd`; when a payload arrives
 * where they do not, the panel prints the discrepancy rather than hiding it
 * behind a rounded total.
 */
export type IdleByReason = Record<string, number | null | undefined>;

/**
 * `IdleAttribution.as_dict()` plus the utilisation readings around it.
 *
 * Percentages follow the house convention and are expressed in POINTS
 * (`1.0 === 1%`), exactly as `PortfolioState.open_risk_pct` is.
 */
export interface CapitalState {
  equity?: Num;
  /** Notional currently at work. */
  deployed_usd?: Num;
  /** Deployed / equity, in points. */
  deployed_pct?: Num;
  idle_usd?: Num;
  idle_pct?: Num;
  /** The deployment target the run is aiming at, in points. */
  target_pct?: Num;
  open_positions?: Num;
  /** Candidates that cleared every gate this cycle. */
  qualified_opportunities?: Num;
  /** Candidates that did not. */
  rejected_opportunities?: Num;
  /** Traded notional per day / equity. */
  turnover_per_day?: Num;
  avg_position_usd?: Num;
  avg_holding_seconds?: Num;
  net_pnl_today?: Num;
  /** NET P&L per $100 of capital actually put to work. */
  net_pnl_per_100_deployed?: Num;
  idle_by_reason?: IdleByReason | null;
}

/** `[missed_winners, correct_rejections]` for one rejection reason. */
export type MissedPair = [number, number];

/**
 * `MissedAudit` from `app/research/missed.py`.
 *
 * This is POST-HOC analysis computed with future data: it asks what a refused
 * candidate went on to do, which is knowable only afterwards. It never reaches
 * an entry decision — any path where it could is a leak and a defect — and the
 * panel says so in its own line so nobody reads it as a live signal.
 */
export interface MissedAudit {
  windows?: Num;
  rejections_examined?: Num;
  /** Refusals that would have paid, after costs. */
  missed_winners?: Num;
  /** Refusals that saved money. */
  correct_rejections?: Num;
  /** What the rejected moves went on to do, aggregated. Negative = the gate
      saved money; strongly positive = the gate is throwing money away. */
  missed_net_bps?: Num;
  /** `{RejectReason: [missed, correct]}`. Open record, like `IdleByReason`. */
  by_reason?: Record<string, MissedPair | null | undefined> | null;
}

// ===========================================================================
// PHASE 6 — multi-asset selection, strategy-specific holding, capital recycling
// ---------------------------------------------------------------------------
// Every field below is OPTIONAL and the whole Phase 6 group is expected to be
// absent: the worker that fills it is being extended by other owners while
// these panels are written. The rule does not change — an absent measurement
// is an em dash and an absent block is a panel that says it has not been
// measured, never an empty table that reads as a zero.
//
// Two distinctions this section exists to protect:
//
//   * `BTC_CONCENTRATION` is a DIAGNOSTIC. Nothing suppresses, penalises or
//     re-ranks BTC because it fired. The spec is explicit: if BTC wins on net
//     EV, BTC trades. The flag says "look at the funnel", nothing more.
//
//   * A holding bucket is read PER STRATEGY. `flow_impulse` holds ~32 s by
//     design and `momentum_scalp` ~71 s by design; a pooled "30–60 s" row
//     averages a strategy that is working with one that is not and calls the
//     result a finding.
// ===========================================================================

/** One stage of `app/research/funnel.py:STAGES`, frozen in PHASE6_SPEC. */
export type FunnelStage =
  | "discovered"
  | "subscribed"
  | "data_healthy"
  | "features"
  | "strategy_evaluated"
  | "model_evaluated"
  | "candidate"
  | "cost_gate"
  | "liquidity_gate"
  | "risk_gate"
  | "correlation_gate"
  | "ranked"
  | "selected"
  | "traded";

/** `SymbolFunnel.as_dict()`. */
export interface SymbolFunnel {
  symbol?: string | null;
  /** Every key in STAGES, monotone non-increasing. */
  stages?: Record<string, number | null | undefined> | null;
  /** `stage -> reason -> count`. */
  drop_reasons?: Record<string, Record<string, number | null | undefined>> | null;
  deepest_stage?: FunnelStage | string | null;
  /** The stage that stopped this symbol — the wall, not the last thing that
      happened. This is the CAUSE the concentration flag has to print. */
  bottleneck_stage?: FunnelStage | string | null;
  bottleneck_reason?: string | null;
}

/** `FunnelReport.as_dict()`. */
export interface FunnelReport {
  symbols?: Record<string, SymbolFunnel> | null;
  /** How many SYMBOLS (not events) reached each stage. */
  stage_totals?: Record<string, number | null | undefined> | null;
  /** Symbols per bottleneck stage — the venue's biggest losses, in order. */
  wall?: Record<string, number | null | undefined> | null;
  /** Share of trades per symbol as a FRACTION in [0, 1], not points. */
  concentration?: Record<string, number | null | undefined> | null;
  /** Monotonicity violations. A non-empty list means the instrumentation, not
      the trader, is what is being measured. */
  violations?: string[] | null;
}

/** One coin's line in the concentration monitor. */
export interface ConcentrationRow {
  symbol: string;
  trades?: Num;
  /** NET P&L, USD — after fees, spread and slippage. */
  net_pnl_usd?: Num;
  /** Signals this coin produced (funnel stage `strategy_evaluated`). */
  signals?: Num;
  /** Signals that cleared every gate (funnel stage `ranked`). */
  qualified_signals?: Num;
  /** Signals refused. `signals − qualified_signals` when both are known. */
  rejected_signals?: Num;
  /** CUMULATIVE traded notional, USD — not capital currently deployed. */
  capital_allocated_usd?: Num;
  /** Share of the trade count, in POINTS (41.7 === 41.7%). */
  trade_share_pct?: Num;
  /** Share of allocated capital, in POINTS. */
  capital_share_pct?: Num;
  /** Where this coin dies, when it never reaches `traded`. */
  bottleneck_stage?: FunnelStage | string | null;
  bottleneck_reason?: string | null;
}

export interface ConcentrationState {
  rows?: ConcentrationRow[] | null;
  /** Closed trades the shares were computed over — the denominator. */
  sample_trades?: Num;
  /** Summed traded notional, USD — the capital shares' denominator. */
  sample_notional_usd?: Num;
  /** The leading symbol and its share, when the worker computes them itself. */
  top_symbol?: string | null;
  top_trade_share_pct?: Num;
  top_capital_share_pct?: Num;
  /** The threshold the flag uses, in points. Defaults to 70 on this screen. */
  threshold_pct?: Num;
  /** Trades required before the flag may speak at all. */
  min_sample?: Num;
  /** Whether the worker itself raised BTC_CONCENTRATION. Diagnostic only. */
  flag?: boolean | null;
  /** The worker's own one-line cause, when it has one. */
  cause?: string | null;
}

/** `RecycleEvent` from `app/portfolio.py`, plus the derived properties. */
export interface RecycleEvent {
  closed_trade_id?: string | null;
  /** The coin that closed. `RecycleEvent.as_dict()` calls it `symbol`;
      `closed_symbol` is accepted as an alias. */
  symbol?: string | null;
  closed_symbol?: string | null;
  closed_ms?: Num;
  capital_released_ms?: Num;
  rescan_triggered_ms?: Num;
  next_entry_ms?: Num;
  /** Whether a qualified candidate existed at the moment capital came free.
      `false` with a long gap is NO_QUALIFIED_SIGNAL; `true` with a long gap is
      a recycling defect, and the two must not be read as one delay. */
  qualified_candidate_existed?: boolean | null;
  /** The candidate the rescan found, when it found one. `next_entry_symbol`
      is the name `RecycleEvent.as_dict()` actually uses; `next_symbol` is
      accepted as an alias so either spelling renders. */
  next_entry_symbol?: string | null;
  next_symbol?: string | null;
  next_strategy?: string | null;
  /** `close_to_rescan_ms` — milliseconds. Ours: loop shape, nothing else. */
  close_to_rescan_ms?: Num;
  /** `close_to_entry_ms` — milliseconds. Null when no entry followed, and that
      null IS the measurement: `qualified_candidate_existed` says whether it
      was the market's doing or ours. */
  close_to_entry_ms?: Num;
  /** How long the capital itself took to come back. Should be ~0. */
  release_ms?: Num;
  released_usd?: Num;
  risk_released_usd?: Num;
  available_usd_after?: Num;
  /** Deployment after the close, in POINTS. */
  deployed_pct_after?: Num;
  rescan_handled?: boolean | null;
  /** Cash that did not reconcile — the CAPITAL_STATE_BUG dollars. */
  state_bug_usd?: Num;
  notes?: string[] | null;
}

export interface RecyclingState {
  /** The most recent close. */
  last?: RecycleEvent | null;
  /** The recent history, newest first. */
  events?: RecycleEvent[] | null;
  /** Medians over `events`, in milliseconds. */
  median_close_to_rescan_ms?: Num;
  median_close_to_entry_ms?: Num;
  /** Cash free to deploy right now, USD. */
  available_cash_usd?: Num;
  /** How long that cash has been sitting, in SECONDS. */
  idle_seconds?: Num;
  /** Candidates that cleared every gate on the latest cycle. */
  qualified_opportunities?: Num;
  /** Closes the medians were computed over — the denominator. `closes` is the
      name `CapitalRecycler.latency()` uses; `sample` is accepted as an alias. */
  closes?: Num;
  sample?: Num;
  /** Closes that were followed by an entry. */
  redeployed?: Num;
  max_close_to_rescan_ms?: Num;
  /** Closes where a qualified candidate existed and capital still sat. This
      is the count that separates a market reading from a recycling defect. */
  qualified_but_not_taken?: Num;
  queue_size?: Num;
}

/** One strategy × bucket cell of the §20 grid. */
export interface HoldingBucketRow {
  strategy?: string | null;
  /** A key from `HOLDING_BUCKETS` in `lib/phase6.ts`. */
  bucket?: string | null;
  trades?: Num;
  net_pnl_usd?: Num;
  gross_pnl_usd?: Num;
  total_cost_usd?: Num;
  /** Rate in [0, 1]. */
  win_rate?: Num;
  /** USD per trade. */
  expectancy_usd?: Num;
}

export interface HoldingBucketState {
  rows?: HoldingBucketRow[] | null;
  /** Closed trades the grid was built from. */
  sample?: Num;
  /** `strategy -> median hold in SECONDS`. The proof the rows differ. */
  median_hold_seconds?: Record<string, number | null | undefined> | null;
}

/** Where a candidate stands on the allocator's board. */
export type OpportunityStatus = "QUEUED" | "SELECTED" | "EXPIRED" | "REJECT";

/** One line of the live opportunity board — `QueuedOpportunity` plus verdict. */
export interface OpportunityRow {
  /** The allocator's ordering. Null for a candidate that never entered it. */
  rank?: Num;
  symbol?: string | null;
  strategy?: string | null;
  episode_id?: string | null;
  /** Expected NET EV in BASIS POINTS — after fees, spread and slippage. */
  net_ev_bps?: Num;
  /** The same expectation in USD, when the worker sizes it. */
  net_ev_usd?: Num;
  /** The notional the EV above was computed against. Without it a USD EV has
      no denominator and cannot be compared across candidates. */
  notional_usd?: Num;
  ranked_at_ms?: Num;
  expires_ms?: Num;
  status?: OpportunityStatus | string | null;
  reject_reason?: RejectReasonCode | string | null;
  /** The ranker's raw score behind the ordering. */
  rank_score?: Num;
  side?: Side | string | null;
  regime?: RegimeCode | string | null;
  /** How many times this entry was revalidated while it waited. */
  refreshed?: Num;
  notes?: string[] | null;
}

export interface OpportunityBoard {
  /** `OpportunityQueue.as_dict()` names this `entries`; `candidates` is
      accepted as an alias so either spelling renders. */
  entries?: OpportunityRow[] | null;
  candidates?: OpportunityRow[] | null;
  generated_ms?: Num;
  /** Entries currently on the board. */
  size?: Num;
  max_size?: Num;
  /** How long a queued opportunity stays valid. */
  ttl_seconds?: Num;
  ttl_ms?: Num;
  /** `{reason: count}` — why entries left the board. Open record. */
  dropped?: Record<string, number | null | undefined> | null;
  /** Symbols evaluated this cycle — the board's denominator. */
  evaluated?: Num;
  qualified?: Num;
  rejected?: Num;
}


/** §07 scan heartbeat. */
export interface ScanState {
  evaluations: Num;
  last_evaluation_ms: Num;
  seconds_since_last: Num;
  evaluations_per_second: Num;
  symbols_evaluated: Num;
  symbols_benched: Num;
  uptime_seconds: Num;
}

/** One benched pair, with the threshold that benched it. */
export interface BenchedSymbol {
  symbol: string;
  tier: string | null;
  edge_ratio: Num;
  warm_threshold: Num;
  hot_threshold: Num;
  round_trip_bps: Num;
  expected_move_bps: Num;
  capacity_max_usd: Num;
  /** How far short of the WARM bar, in the units that decided it. */
  short_of_warm: Num;
}

export interface WatchlistState {
  evaluated: string[];
  benched: BenchedSymbol[];
  warm_threshold: Num;
  hot_threshold: Num;
}
