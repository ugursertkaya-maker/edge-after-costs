"""Central configuration.

Everything the bot does is driven from here.  Values come from the environment
(``.env`` on the server) with safe, conservative defaults so that a missing
variable can never silently enable something dangerous.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field, replace
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


def _money_tuple(name: str, default: tuple[float, ...]) -> tuple[float, ...]:
    """Parse "500,1000" into (500.0, 1000.0); fall back on anything unparseable.

    A malformed comparison-ledger list must degrade to the default rather than
    take the worker down: these books are research instruments, and losing them
    is not worth losing the trader.
    """
    raw = os.getenv(name)
    if not raw:
        return default
    out: list[float] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = float(part)
        except ValueError:
            return default
        if value > 0:
            out.append(value)
    return tuple(out) if out else default


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
    #: Side-by-side research books. They follow the SAME decisions as the
    #: primary ledger at a different account size, so they measure capacity,
    #: not strategy. Configurable because which sizes are interesting changes
    #: with the primary: running a $5,000 comparison against a $5,000 primary
    #: would be two identical books.
    comparison_ledgers: tuple[float, ...] = field(
        default_factory=lambda: _money_tuple("COMPARISON_LEDGERS", (1_000.0, 5_000.0))
    )

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
    profile: str = _es("NET_TP_PROFILE", "venue_base")
    # The four research profiles the brief asks for.
    micro_pct: float = _ef("NET_TP_MICRO_PCT", 0.25)
    base_pct: float = _ef("NET_TP_BASE_PCT", 0.50)
    fast_pct: float = _ef("NET_TP_FAST_PCT", 0.75)
    extended_pct: float = _ef("NET_TP_EXTENDED_PCT", 1.00)
    # ...and two calibrated to what this venue actually moves. Measured on
    # Binance.US, BTC/USD has a 1-minute ATR of ~2.2 bps, so a typical 25-minute
    # move is ~11 bps (0.11%) and a 60-minute one ~17 bps (0.17%). Against that,
    # even MICRO at 0.25% needs more than the whole typical hour: the four
    # requested profiles are not impossible, they are tail outcomes, and a bot
    # aiming only at tails trades almost never and produces no evidence. These
    # two sit inside a normal hour so the system can actually run here. The
    # requested four stay selectable and are swept by the experiment harness
    # alongside these. See docs/ECONOMICS.md.
    venue_micro_pct: float = _ef("NET_TP_VENUE_MICRO_PCT", 0.05)
    venue_base_pct: float = _ef("NET_TP_VENUE_BASE_PCT", 0.10)

    # Extra margin demanded above the modelled cost, to cover execution
    # uncertainty the model cannot see.
    latency_buffer_bps: float = _ef("NET_TP_LATENCY_BUFFER_BPS", 1.0)
    uncertainty_margin: float = _ef("NET_TP_UNCERTAINTY_MARGIN", 1.25)

    # Volatility sanity: a NET target the market cannot reach in the horizon is
    # not a target, it is a time stop with extra steps.
    max_target_atr_mult: float = _ef("NET_TP_MAX_ATR_MULT", 6.0)

    def target_pct(self, name: str | None = None) -> float:
        return {
            "venue_micro": self.venue_micro_pct, "venue_base": self.venue_base_pct,
            "micro": self.micro_pct, "base": self.base_pct,
            "fast": self.fast_pct, "extended": self.extended_pct,
        }.get(name or self.profile, self.base_pct)

    @property
    def all_profiles(self) -> tuple[str, ...]:
        """Every profile the experiment harness sweeps, smallest target first."""
        return ("venue_micro", "venue_base", "micro", "base", "fast", "extended")

    @property
    def requested_profiles(self) -> tuple[str, ...]:
        """The four from the brief, kept separable from the calibrated pair."""
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

    #: Emergency ceiling only, and it must stay one. 0 means "derive", so the
    #: count never becomes the binding constraint on a larger account: a $500
    #: book at $50 a position tops out at 10 positions, a $5,000 book at $500 a
    #: position needs the same 10 -- but a book whose positions are capped far
    #: below that by L2 depth needs many more slots to use the same capital.
    max_open_positions: int = _ei("MAX_OPEN_POSITIONS", 0)
    total_open_risk_pct: float = _ef("TOTAL_OPEN_RISK_PCT", 3.0)  # 2 | 3 | 5 profiles
    cash_reserve_pct: float = _ef("CASH_RESERVE_PCT", 20.0)       # 10 | 20 | 30
    min_position_usd: float = _ef("MIN_POSITION_USD", 10.0)
    # 10% of a $500 account. This is not just a risk preference: the risk engine
    # applies its portfolio gate to its OWN un-shrunk size before the allocator
    # shrinks a candidate to the per-cycle slice, so a $125 ceiling made every
    # candidate face the gate at five times the size that would actually be
    # sent. Measured effect: deployment capped at 46.8% with the 60-85% band
    # unreachable, and the dominant idle reason misreported as
    # CORRELATION_LIMIT. At $50 the same candidate list deploys 57.1% and the
    # dominant reason is NO_QUALIFIED_SIGNAL -- the diagnosis itself changes.
    #: Explicit override; 0 means "derive from equity" (see
    #: :meth:`Settings.max_position_usd_for`). The derivation keeps the
    #: documented reasoning -- $50 was 10% of a $500 account -- while letting
    #: the ceiling follow the account instead of freezing at a dollar figure
    #: chosen for a different one.
    max_position_usd: float = _ef("MAX_POSITION_USD", 0.0)
    max_position_pct_equity_alloc: float = _ef("MAX_POSITION_PCT_EQUITY_ALLOC", 10.0)
    #: Share of equity the allocator aims to have working. A stopping
    #: condition, never a quota: it stops allocating when the band is reached
    #: OR when candidates run out, and never lowers the bar to fill it.
    target_deployed_pct: float = _ef("TARGET_DEPLOYED_PCT", 80.0)
    max_positions_per_symbol: int = _ei("MAX_POSITIONS_PER_SYMBOL", 1)

    # Correlation: eight simultaneous crypto longs are one large crypto long.
    correlation_window_bars: int = _ei("CORRELATION_WINDOW_BARS", 120)
    max_correlated_risk_pct: float = _ef("MAX_CORRELATED_RISK_PCT", 2.0)
    correlation_threshold: float = _ef("CORRELATION_THRESHOLD", 0.65)
    max_directional_exposure_pct: float = _ef("MAX_DIRECTIONAL_EXPOSURE_PCT", 70.0)

    # Allocation: better setups get more capital, but only via edge and risk.
    allocation_mode: str = _es("ALLOCATION_MODE", "dynamic")  # few_large|many_small|dynamic


@dataclass(frozen=True)
class RecycleConfig:
    """Capital recycling: the close → release → rescan → redeploy path (§11–§15).

    The defaults encode two decisions the phase spec makes explicitly.

    **A close triggers a rescan.**  Waiting for the next scheduler tick leaves
    freed capital idle for a whole interval for no reason other than loop
    shape, and the idle attribution would charge that to ``SCANNER_DELAY``
    rather than to the market — a real cost, invisible in the P&L.

    **There is no global cooldown after a close.**  Closing SOL must not block
    opening ETH, so ``symbol_cooldown_seconds`` is per *symbol* and defaults to
    **0 — off**.  It exists because churning the same coin on the same idea is
    a real failure mode worth being able to switch on, not because anything
    measured says it should be on.  Anything broader than one symbol would be a
    global cooldown by another name and is not configurable here at all.
    """

    rescan_on_close: bool = _eb("RECYCLE_RESCAN_ON_CLOSE", True)
    queue_enabled: bool = _eb("RECYCLE_QUEUE_ENABLED", True)
    #: Board size.  Bounded because a queue that keeps everything is a log, and
    #: the worst entry on a 500-deep board will never be deployed anyway.
    queue_max_size: int = _ei("RECYCLE_QUEUE_MAX", 50)
    #: How long a ranked candidate stays on the board.  Two minutes is roughly
    #: the horizon over which the features it was ranked on are still recognisable;
    #: past that, revalidation would reject it anyway and it is only taking a slot.
    queue_ttl_seconds: float = _ef("RECYCLE_QUEUE_TTL_SECONDS", 120.0)
    #: Extra decay guard on revalidation, as a percentage of the queued edge.
    #: 100 means "only the live gate binds" — the recomputed edge still has to
    #: clear ``risk.min_net_edge_bps`` on its own, which is the real protection.
    max_ev_decay_pct: float = _ef("RECYCLE_MAX_EV_DECAY_PCT", 100.0)
    #: Quantisation of the triggering feature inside ``episode_id``.  Coarse on
    #: purpose: the bucket exists so the *same* impulse cannot mint a new
    #: episode by drifting a fraction of a basis point.
    signal_bucket_bps: float = _ef("RECYCLE_SIGNAL_BUCKET_BPS", 5.0)
    #: Anti-churn, one symbol at a time.  **Off by default** (§15).
    symbol_cooldown_seconds: float = _ef("RECYCLE_SYMBOL_COOLDOWN_SECONDS", 0.0)
    #: How many recycle events are kept for the latency report.
    event_history: int = _ei("RECYCLE_EVENT_HISTORY", 500)


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
class HoldingConfig:
    """Thresholds for strategy-specific position management (Phase 6, §7-§10).

    The per-strategy numbers — min/typical/max hold, decay halflife, whether the
    entry score may be re-read at all — live in ``app.holding.PROFILES``, because
    they are *measurements of a strategy* and belong next to the strategies they
    describe.  What is configurable here is the machinery those profiles are fed
    through: how much retracement counts as a reversal, what a deteriorated book
    looks like, how hard volatility is allowed to stretch a clock.
    """

    enabled: bool = _eb("HOLDING_POLICY_ENABLED", True)

    # A trigger that has flipped this far *against* the position is no longer a
    # decayed signal, it is the opposite signal.
    reversal_frac: float = _ef("HOLDING_REVERSAL_FRAC", 0.5)

    # Only consulted for profiles with ``reeval_entry_score``; for the slow
    # strategies the live score is ignored entirely, whatever this says.
    entry_score_exit_frac: float = _ef("HOLDING_ENTRY_SCORE_EXIT_FRAC", 0.6)

    # Execution state.  Relative *and* absolute, both: a spread that doubles
    # from 0.3 bps has doubled and still costs nothing.  The absolute floor is
    # the risk engine's entry gate — above it we would not have opened.
    exec_spread_mult: float = _ef("HOLDING_EXEC_SPREAD_MULT", 3.0)
    exec_min_spread_bps: float = _ef("HOLDING_EXEC_MIN_SPREAD_BPS", 6.0)
    exec_depth_collapse_frac: float = _ef("HOLDING_EXEC_DEPTH_FRAC", 0.25)
    exec_depth_notional_mult: float = _ef("HOLDING_EXEC_DEPTH_NOTIONAL_MULT", 2.0)

    # A model that now expects the position to lose money is not a hold.
    # Slightly negative is noise around the gate; this is materially negative.
    negative_ev_bps: float = _ef("HOLDING_NEGATIVE_EV_BPS", -2.0)

    # mean_reversion's own horizon: value reached, and structure invalidated.
    reversion_target_frac: float = _ef("HOLDING_REVERSION_TARGET_FRAC", 0.25)
    reversion_invalidate_mult: float = _ef("HOLDING_REVERSION_INVALIDATE_MULT", 1.6)

    # Volatility stretches the strategy clock: quiet markets need longer to
    # deliver the same move, violent ones deliver it or kill it sooner.
    vol_scale: float = _ef("HOLDING_VOL_SCALE", 1.0)
    vol_scale_min: float = _ef("HOLDING_VOL_SCALE_MIN", 0.5)
    vol_scale_max: float = _ef("HOLDING_VOL_SCALE_MAX", 1.5)


@dataclass(frozen=True)
class RecorderConfig:
    """Raw stream capture, so maker-first can be evaluated honestly.

    A resting order lives or dies on queue position and trade-through, neither
    of which exists in a 1-minute bar. Recording the tape is the only way to
    replay a maker strategy against something real.
    """

    enabled: bool = _eb("RECORD_TAPE", True)
    directory: str = _es("TAPE_DIR", "/data/tape")
    retention_hours: int = _ei("TAPE_RETENTION_HOURS", 336)   # 14 days
    roll_seconds: int = _ei("TAPE_ROLL_SECONDS", 3600)


@dataclass(frozen=True)
class ResearchConfig:
    """Where the tournament's output and the promoted champion live.

    The search runs off-box (the VPS has 2 vCPU and a live engine on it), so its
    results arrive as files. The worker reads them and republishes them with the
    dashboard payload, which keeps the API container free of research code.
    """

    directory: str = _es("RESEARCH_DIR", "/data/research")
    tournament_file: str = _es("TOURNAMENT_FILE", "tournament.json")
    finalists_file: str = _es("FINALISTS_FILE", "finalists.json")


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
    recycle: RecycleConfig = field(default_factory=RecycleConfig)
    horizon: HorizonConfig = field(default_factory=HorizonConfig)
    holding: HoldingConfig = field(default_factory=HoldingConfig)

    def __post_init__(self) -> None:
        """Resolve account-size derivations into ``portfolio``.

        Done here rather than at each call site because eight modules already
        read ``settings.portfolio.max_position_usd`` and
        ``settings.portfolio.max_open_positions``. Resolving centrally means a
        sentinel of 0 never escapes into sizing arithmetic, where it would
        silently become "no positions allowed".
        """
        resolved = replace(
            self.portfolio,
            max_position_usd=self._derived_max_position_usd(),
            max_open_positions=self._derived_max_open_positions(),
        )
        object.__setattr__(self, "portfolio", resolved)

    def _derived_max_position_usd(self) -> float:
        override = self.portfolio.max_position_usd
        if override > 0:
            return override
        pct = max(0.0, self.portfolio.max_position_pct_equity_alloc) / 100.0
        return max(self.portfolio.min_position_usd, self.risk.starting_equity * pct)

    def _derived_max_open_positions(self) -> int:
        override = self.portfolio.max_open_positions
        if override > 0:
            return override
        floor_size = max(self.portfolio.min_position_usd, 1.0)
        target = self.risk.starting_equity * self.portfolio.target_deployed_pct / 100.0
        return int(min(max(20, math.ceil(target / floor_size)), 200))

    # ---------------------------------------------------------------- #
    # Account-size derivations
    #
    # These exist because a dollar constant chosen for a $500 book silently
    # becomes the binding constraint on a $5,000 one. Each returns the explicit
    # override when set, so an operator can still pin any of them.
    # ---------------------------------------------------------------- #

    @property
    def max_position_usd(self) -> float:
        """Largest position the allocator may open, in USD (already resolved)."""
        return self.portfolio.max_position_usd

    @property
    def target_active_capital_usd(self) -> float:
        """The capital the allocator aims to have working. Never a quota."""
        return self.risk.starting_equity * self.portfolio.target_deployed_pct / 100.0

    @property
    def cash_reserve_usd(self) -> float:
        """Capital deliberately held back. Policy, not failure."""
        return self.risk.starting_equity * self.portfolio.cash_reserve_pct / 100.0

    @property
    def max_open_positions(self) -> int:
        """Emergency ceiling on concurrent positions (already resolved)."""
        return self.portfolio.max_open_positions
    recorder: RecorderConfig = field(default_factory=RecorderConfig)
    research: ResearchConfig = field(default_factory=ResearchConfig)

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
