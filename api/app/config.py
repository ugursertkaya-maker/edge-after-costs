"""Central configuration.

Everything the bot does is driven from here.  Values come from the environment
(``.env`` on the server) with safe, conservative defaults so that a missing
variable can never silently enable something dangerous.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _f(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def _i(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _b(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _s(name: str, default: str) -> str:
    raw = os.getenv(name)
    return default if raw is None or raw == "" else raw


# Environment lookups must happen when a ``Settings`` object is *constructed*,
# not when this module is imported — otherwise ``reload_settings()`` would be a
# lie and tests could never exercise a different configuration.  Wrapping each
# default in a ``default_factory`` is what buys that.


def _es(name: str, default: str):
    return field(default_factory=lambda: _s(name, default))


def _ef(name: str, default: float):
    return field(default_factory=lambda: _f(name, default))


def _ei(name: str, default: int):
    return field(default_factory=lambda: _i(name, default))


def _eb(name: str, default: bool):
    return field(default_factory=lambda: _b(name, default))


# --------------------------------------------------------------------------- #
# Trading mode.  There is exactly one legal value in V1.
# --------------------------------------------------------------------------- #

PAPER = "PAPER"
LIVE = "LIVE"


@dataclass(frozen=True)
class ExchangeConfig:
    """Binance.US public market data.  No authenticated calls exist in V1."""

    name: str = "binanceus"
    rest_base: str = _es("EXCHANGE_REST_BASE", "https://api.binance.us")
    ws_base: str = _es("EXCHANGE_WS_BASE", "wss://stream.binance.us:9443")
    # Quote assets we are willing to trade against, most preferred first.
    quote_assets: tuple[str, ...] = ("USD", "USDT")


@dataclass(frozen=True)
class FeeConfig:
    """Fee model.

    Binance.US publishes a "Tier 0" list of pairs that trade at 0% maker /
    0.01% taker regardless of volume; everything else falls into the standard
    volume tiers which start at 0.10% / 0.10% for a new account.

    The default is ``tier0``: the per-pair table is what we would actually pay,
    and averaging it up to 10 bps would be wrong, not conservative.  But see
    docs/ECONOMICS.md — at the standard tier an ATR-sized target is *structurally*
    unprofitable, so the Tier-0 schedule is not a nicety, it is the edge.  Two
    consequences are wired in deliberately:

    * ``UniverseConfig.restrict_to_fee_tier0`` stops the scanner from looking at
      pairs whose fee tier cannot fund the target in the first place, so the
      modelled rate always matches the pair being traded.
    * Every reported result is produced twice — once here, and once under
      ``FEE_MODEL=standard`` as the stress case.  If the stress run is negative,
      that is the finding, not a bug.

    ``FEE_STRESS_BPS`` forces a flat rate on both sides for sensitivity runs.
    """

    model: str = _es("FEE_MODEL", "tier0")  # tier0 (actual) | standard (stress)
    standard_maker_bps: float = _ef("FEE_STANDARD_MAKER_BPS", 10.0)
    standard_taker_bps: float = _ef("FEE_STANDARD_TAKER_BPS", 10.0)
    tier0_maker_bps: float = _ef("FEE_TIER0_MAKER_BPS", 0.0)
    tier0_taker_bps: float = _ef("FEE_TIER0_TAKER_BPS", 1.0)
    stress_bps: float = _ef("FEE_STRESS_BPS", 0.0)  # >0 overrides both sides

    # Published Tier-0 pairs (0% maker / 0.01% taker).  Verified 2026-09-10.
    tier0_symbols: frozenset[str] = frozenset(
        {
            "BTCUSD", "ETHUSD", "SOLUSD", "BNBUSD", "ADAUSD", "AVAXUSD",
            "VETUSD", "POLUSD", "ATOMUSD", "ONEUSD", "DOTUSD", "NEARUSD",
            "FETUSD", "SUIUSD", "LPTUSD",
            "XTZUSDT", "ROSEUSDT", "KSMUSDT", "KAVAUSDT", "TUSDT",
            "ONTUSDT", "CELRUSDT", "AUDIOUSDT",
        }
    )

    def maker_bps(self, symbol: str) -> float:
        if self.stress_bps > 0:
            return self.stress_bps
        if self.model == "tier0" and symbol in self.tier0_symbols:
            return self.tier0_maker_bps
        return self.standard_maker_bps

    def taker_bps(self, symbol: str) -> float:
        if self.stress_bps > 0:
            return self.stress_bps
        if self.model == "tier0" and symbol in self.tier0_symbols:
            return self.tier0_taker_bps
        return self.standard_taker_bps


@dataclass(frozen=True)
class UniverseConfig:
    """Rules that decide which symbols are even eligible to be scanned."""

    max_symbols: int = _ei("UNIVERSE_MAX_SYMBOLS", 12)
    hot_symbols: int = _ei("UNIVERSE_HOT_SYMBOLS", 5)
    # Calibrated against Binance.US reality on 2026-09-10, not against a
    # generic "liquid exchange" prior. Binance.US has near-zero fees *because*
    # its volume collapsed: BTC/USD turns over ~$1.6M a day with ~3k prints,
    # two orders of magnitude below Coinbase. The books are nevertheless tight
    # (0.3-1.1 bps) and deep enough for our size ($23k-420k within 10 bps of
    # mid), which is what actually matters for a $50-150 position. Thresholds
    # set for a generic exchange reject every single pair here — measure before
    # raising these. See docs/ECONOMICS.md.
    min_quote_volume_24h: float = _ef("UNIVERSE_MIN_QUOTE_VOLUME_24H", 100_000.0)
    min_trades_24h: int = _ei("UNIVERSE_MIN_TRADES_24H", 200)
    max_spread_bps: float = _ef("UNIVERSE_MAX_SPREAD_BPS", 8.0)
    min_depth_usd: float = _ef("UNIVERSE_MIN_DEPTH_USD", 5_000.0)
    min_volatility_bps: float = _ef("UNIVERSE_MIN_VOLATILITY_BPS", 8.0)
    # 24h high/low range. 400 bps would reject majors on any ordinary 5% day.
    max_volatility_bps: float = _ef("UNIVERSE_MAX_VOLATILITY_BPS", 1_500.0)
    refresh_seconds: int = _ei("UNIVERSE_REFRESH_SECONDS", 900)
    # Micro-profit only survives where the round trip is ~1 bps, i.e. on the
    # published Tier-0 pairs.  Off the Tier-0 list the standard 10 bps/side
    # swallows an ATR-sized target whole, so the default is to refuse to scan
    # pairs the economics cannot support.  See docs/EXCHANGE_DECISION.md.
    restrict_to_fee_tier0: bool = _eb("UNIVERSE_RESTRICT_TIER0", True)
    # Hard denylist regardless of metrics (stablecoin pairs, leveraged tokens).
    deny_substrings: tuple[str, ...] = ("UPUSD", "DOWNUSD", "BULL", "BEAR")
    deny_bases: tuple[str, ...] = ("USDT", "USDC", "BUSD", "DAI", "TUSD", "USDP", "PYUSD")


@dataclass(frozen=True)
class RiskConfig:
    """The $500 risk model (§14) plus the global circuit breakers (§21)."""

    starting_equity: float = _ef("STARTING_EQUITY", 500.0)
    comparison_ledgers: tuple[float, ...] = (1_000.0, 5_000.0)

    risk_per_trade_pct: float = _ef("RISK_PER_TRADE_PCT", 0.5)  # % of equity at stop
    max_concurrent_positions: int = _ei("MAX_CONCURRENT_POSITIONS", 3)
    max_position_pct_equity: float = _ef("MAX_POSITION_PCT_EQUITY", 35.0)
    max_total_exposure_pct: float = _ef("MAX_TOTAL_EXPOSURE_PCT", 90.0)
    max_concurrent_risk_pct: float = _ef("MAX_CONCURRENT_RISK_PCT", 1.5)
    max_positions_per_symbol: int = _ei("MAX_POSITIONS_PER_SYMBOL", 1)

    daily_loss_limit_pct: float = _ef("DAILY_LOSS_LIMIT_PCT", 3.0)
    weekly_loss_limit_pct: float = _ef("WEEKLY_LOSS_LIMIT_PCT", 7.0)
    loss_streak_len: int = _ei("LOSS_STREAK_LEN", 4)
    loss_streak_cooldown_minutes: int = _ei("LOSS_STREAK_COOLDOWN_MINUTES", 45)

    # Entry gates
    max_entry_spread_bps: float = _ef("MAX_ENTRY_SPREAD_BPS", 6.0)
    max_slippage_bps: float = _ef("MAX_SLIPPAGE_BPS", 12.0)
    min_book_depth_usd: float = _ef("MIN_BOOK_DEPTH_USD", 2_000.0)
    max_book_age_ms: int = _ei("MAX_BOOK_AGE_MS", 2_500)
    # Calibrated to the venue, not to a textbook. Measured live on Binance.US:
    # gross edge ~2.5-2.9 bps, fee 1.0, spread 0.25-0.5, slippage 0.75-1.45,
    # leaving ~+0.9 bps net. A 4 bps gate is larger than anything this market
    # offers at this horizon and turns the bot into a permanent no-op, which
    # produces no evidence either way. 1.0 bps still admits only setups the
    # model scores as net POSITIVE — it does not lower the bar to zero — but it
    # is a thin margin, well inside the error of the slippage and hit-rate
    # estimates. Treat trades near the gate as unproven. See docs/ECONOMICS.md.
    min_net_edge_bps: float = _ef("MIN_NET_EDGE_BPS", 1.0)
    min_net_edge_usd: float = _ef("MIN_NET_EDGE_USD", 0.02)
    abnormal_vol_sigma: float = _ef("ABNORMAL_VOL_SIGMA", 6.0)

    min_notional_usd: float = _ef("MIN_NOTIONAL_USD", 10.0)


@dataclass(frozen=True)
class ExecConfig:
    """Realistic paper-execution parameters (§18)."""

    latency_ms: int = _ei("SIM_LATENCY_MS", 250)
    latency_profiles_ms: tuple[int, ...] = (20, 50, 100, 250, 500, 1000)
    # Extra adverse move applied on top of book-walk, in bps, to represent
    # queue/adverse-selection effects the book snapshot cannot show.
    adverse_bps: float = _ef("SIM_ADVERSE_BPS", 0.5)
    # Probability a resting limit order is skipped even when price trades
    # through it (queue position not reached).
    limit_miss_ratio: float = _ef("SIM_LIMIT_MISS_RATIO", 0.35)
    partial_fill_enabled: bool = _eb("SIM_PARTIAL_FILL", True)
    max_book_walk_levels: int = _ei("SIM_MAX_BOOK_WALK_LEVELS", 20)


@dataclass(frozen=True)
class ExitConfig:
    """Take-profit / stop-loss engine defaults (§12, §13)."""

    tp_model: str = _es("TP_MODEL", "adaptive")  # adaptive | atr | fixed_pct
    tp_atr_mult: float = _ef("TP_ATR_MULT", 1.1)
    tp_fixed_pct: float = _ef("TP_FIXED_PCT", 0.35)
    tp_min_bps_over_cost: float = _ef("TP_MIN_BPS_OVER_COST", 6.0)

    sl_model: str = _es("SL_MODEL", "atr")  # atr | structure | fixed_pct
    sl_atr_mult: float = _ef("SL_ATR_MULT", 1.0)
    sl_fixed_pct: float = _ef("SL_FIXED_PCT", 0.45)
    emergency_stop_pct: float = _ef("EMERGENCY_STOP_PCT", 2.5)

    min_reward_risk: float = _ef("MIN_REWARD_RISK", 0.9)
    trailing_enabled: bool = _eb("TRAILING_ENABLED", True)
    trailing_arm_frac: float = _ef("TRAILING_ARM_FRAC", 0.6)  # of TP distance
    trailing_give_back_frac: float = _ef("TRAILING_GIVE_BACK_FRAC", 0.35)
    time_stop_seconds: int = _ei("TIME_STOP_SECONDS", 2_700)
    # If price has not moved at least this far after half the time stop, leave.
    stale_exit_frac: float = _ef("STALE_EXIT_FRAC", 0.25)


@dataclass(frozen=True)
class BacktestConfigDefaults:
    """Where downloaded history is cached.

    On the server this is a docker volume; on a laptop the default path is not
    writable and ``app.backtest.data`` falls back to a temp directory, because a
    backtest that re-downloads is merely slow and one that crashes is useless.
    """

    cache_dir: str = _es("BACKTEST_CACHE_DIR", "/data/backtest-cache")


@dataclass(frozen=True)
class ProfitConfig:
    """Fee-aware NET take-profit engine (V2).

    A fixed dollar target is economically incoherent: $0.50 is 5% of a $10
    position and 0.25% of a $200 one.  Targets are therefore expressed as a NET
    percentage of the position, and the engine solves backwards through every
    cost line to the gross price that actually delivers it.
    """

    # Research profiles, all NET of fees + spread + slippage + latency buffer.
    profile: str = _es("NET_TP_PROFILE", "base")  # micro | base | fast | extended
    micro_pct: float = _ef("NET_TP_MICRO_PCT", 0.25)
    base_pct: float = _ef("NET_TP_BASE_PCT", 0.50)
    fast_pct: float = _ef("NET_TP_FAST_PCT", 0.75)
    extended_pct: float = _ef("NET_TP_EXTENDED_PCT", 1.00)

    # Extra margin demanded above the modelled cost, to cover execution
    # uncertainty the model cannot see.
    latency_buffer_bps: float = _ef("NET_TP_LATENCY_BUFFER_BPS", 1.0)
    uncertainty_margin: float = _ef("NET_TP_UNCERTAINTY_MARGIN", 1.25)

    # Volatility sanity: a NET target the market cannot reach in the horizon is
    # not a target, it is a time stop with extra steps.
    max_target_atr_mult: float = _ef("NET_TP_MAX_ATR_MULT", 6.0)

    def target_pct(self, name: str | None = None) -> float:
        return {
            "micro": self.micro_pct, "base": self.base_pct,
            "fast": self.fast_pct, "extended": self.extended_pct,
        }.get(name or self.profile, self.base_pct)

    @property
    def all_profiles(self) -> tuple[str, ...]:
        return ("micro", "base", "fast", "extended")


@dataclass(frozen=True)
class ExecutionStyleConfig:
    """Maker-first execution (V2).

    Binance.US currently charges 0% maker.  Resting is therefore strictly
    cheaper than crossing whenever it fills — but it does not always fill, and
    an unfilled maker order must never be auto-converted into a market order:
    it has to re-qualify on taker economics first.
    """

    maker_first: bool = _eb("MAKER_FIRST", True)
    maker_offset_ticks: int = _ei("MAKER_OFFSET_TICKS", 1)
    maker_wait_seconds: int = _ei("MAKER_WAIT_SECONDS", 20)
    maker_requote_limit: int = _ei("MAKER_REQUOTE_LIMIT", 2)
    # Cross the spread only if the trade still clears the NET gate as a taker.
    allow_taker_fallback: bool = _eb("ALLOW_TAKER_FALLBACK", True)
    cancel_on_signal_loss: bool = _eb("CANCEL_ON_SIGNAL_LOSS", True)


@dataclass(frozen=True)
class PortfolioConfig:
    """Many small positions, bounded by risk rather than by a position count.

    The old 1-3 position cap is replaced by a capital-and-risk budget.  The
    count ceiling that remains is a safety valve, not the normal constraint.
    """

    max_open_positions: int = _ei("MAX_OPEN_POSITIONS", 25)   # emergency ceiling only
    total_open_risk_pct: float = _ef("TOTAL_OPEN_RISK_PCT", 3.0)  # 2 | 3 | 5 profiles
    cash_reserve_pct: float = _ef("CASH_RESERVE_PCT", 20.0)       # 10 | 20 | 30
    min_position_usd: float = _ef("MIN_POSITION_USD", 10.0)
    max_position_usd: float = _ef("MAX_POSITION_USD", 125.0)
    max_positions_per_symbol: int = _ei("MAX_POSITIONS_PER_SYMBOL", 1)

    # Correlation: eight simultaneous crypto longs are one large crypto long.
    correlation_window_bars: int = _ei("CORRELATION_WINDOW_BARS", 120)
    max_correlated_risk_pct: float = _ef("MAX_CORRELATED_RISK_PCT", 2.0)
    correlation_threshold: float = _ef("CORRELATION_THRESHOLD", 0.65)
    max_directional_exposure_pct: float = _ef("MAX_DIRECTIONAL_EXPOSURE_PCT", 70.0)

    # Allocation: better setups get more capital, but only via edge and risk.
    allocation_mode: str = _es("ALLOCATION_MODE", "dynamic")  # few_large|many_small|dynamic


@dataclass(frozen=True)
class HorizonConfig:
    """The 25-60 minute development window.

    25 minutes is a checkpoint, never a lock: profit taken early is taken, a
    stop hit at minute two is honoured at minute two.
    """

    checkpoint_seconds: int = _ei("HORIZON_CHECKPOINT_SECONDS", 1_500)   # 25 min
    time_stop_seconds: int = _ei("HORIZON_TIME_STOP_SECONDS", 3_600)     # 60 min
    allow_extension: bool = _eb("HORIZON_ALLOW_EXTENSION", True)
    extension_seconds: int = _ei("HORIZON_EXTENSION_SECONDS", 1_800)
    # At the checkpoint, a position doing nothing yields to a better idea.
    rotate_if_edge_ratio: float = _ef("HORIZON_ROTATE_IF_EDGE_RATIO", 1.75)
    rotate_min_progress_frac: float = _ef("HORIZON_ROTATE_MIN_PROGRESS", 0.25)


@dataclass(frozen=True)
class WorkerConfig:
    evaluate_interval_ms: int = _ei("EVAL_INTERVAL_MS", 1_000)
    position_monitor_interval_ms: int = _ei("POSITION_MONITOR_INTERVAL_MS", 500)
    snapshot_interval_seconds: int = _ei("SNAPSHOT_INTERVAL_SECONDS", 60)
    max_cpu_soft_pct: float = _ef("MAX_CPU_SOFT_PCT", 140.0)
    tick_retention_hours: int = _ei("TICK_RETENTION_HOURS", 48)
    event_retention_days: int = _ei("EVENT_RETENTION_DAYS", 30)


@dataclass(frozen=True)
class Settings:
    env: str = _es("ENV", "production")
    debug: bool = _eb("DEBUG", False)
    mode: str = _es("TRADING_MODE", PAPER)
    base_path: str = _es("BASE_PATH", "/cryptobot")
    public_base_url: str = _es("PUBLIC_BASE_URL", "https://your-domain.example/cryptobot")
    secret_key: str = _es("SECRET_KEY", "dev-insecure-key-change-me")
    cookie_secure: bool = _eb("COOKIE_SECURE", True)
    bootstrap_admin_email: str = _es("BOOTSTRAP_ADMIN_EMAIL", "")
    bootstrap_admin_password: str = _es("BOOTSTRAP_ADMIN_PASSWORD", "")
    database_url: str = _es(
        "DATABASE_URL", "postgresql+psycopg://cryptobot:cryptobot@127.0.0.1:5432/cryptobot"
    )
    redis_url: str = _es("REDIS_URL", "redis://127.0.0.1:6379/0")
    simulator_version: str = _es("SIMULATOR_VERSION", "sim-1.0.0")

    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    fees: FeeConfig = field(default_factory=FeeConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecConfig = field(default_factory=ExecConfig)
    exits: ExitConfig = field(default_factory=ExitConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    backtest: BacktestConfigDefaults = field(default_factory=BacktestConfigDefaults)
    profit: ProfitConfig = field(default_factory=ProfitConfig)
    execution_style: ExecutionStyleConfig = field(default_factory=ExecutionStyleConfig)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    horizon: HorizonConfig = field(default_factory=HorizonConfig)

    @property
    def is_paper(self) -> bool:
        return self.mode == PAPER


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    """Drop the cache — used by tests that patch the environment."""
    get_settings.cache_clear()
    return get_settings()
