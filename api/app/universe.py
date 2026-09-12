"""Which pairs are even worth scanning.

Universe selection is the cheapest risk control we have: a symbol that never
enters the list can never produce a bad fill.  The rules come straight from
``UniverseConfig`` and are applied in one pure, deterministic pass so the same
exchange snapshot always yields the same list — tests and the dashboard both
depend on that.

Scoring is intentionally *absolute*, not relative to the candidate set: each
component is normalised against a configured threshold, so adding or removing
one candidate never re-orders the others.

Phase 5 adds a second, wider reading of the same snapshot.  ``select`` answers
"which handful do we scan", and answers it exactly as it always has.
``select_tiers`` answers "what does the whole venue look like, and what is each
pair worth *after the cost of trading it*" — HOT is scanned every cycle, WARM
every ``warm_every`` cycles, COLD is a watchlist refreshed on the slow loop.

The distinction that makes the tiering worth having is economic, not technical.
The deepest pairs on Binance.US are not the cheap ones: BTCUSDT turns over ~3.5x
BTCUSD but costs 10 bps a side against ~1, so its ~20 bps round trip is the
whole of BTC's median 60-minute move.  Ranking on volume alone therefore ranks
the wrong pairs first, which is why every entry now carries ``round_trip_bps``,
``expected_move_bps`` and their ratio, and why ``net_score`` — not ``score`` —
orders the tier path.  See docs/ECONOMICS.md.
"""

from __future__ import annotations

import asyncio
import enum
import math
from dataclasses import dataclass, replace

from .config import FeeConfig, Settings, get_settings
from .types import OrderBook, SymbolInfo

#: Score weights — liquidity dominates because it is what makes a scalp exitable.
W_LIQUIDITY = 0.45
W_VOLATILITY = 0.35
W_TIGHTNESS = 0.20

#: Volume this many times the configured minimum scores a full liquidity point.
LIQUIDITY_SPAN = 100.0

#: Depth is measured this far from mid on each side.
DEPTH_BPS = 10.0

#: Cost-aware weights.  Used only by :meth:`UniverseSelector.score` when a round
#: trip is supplied; the three-argument call keeps the weights above so the
#: legacy ordering is bit-for-bit what it was.  Cost carries the largest single
#: weight deliberately: on this venue it is the term that decides whether a
#: setup can exist at all, where the others only decide how good it is.
W_NET_COST = 0.40
W_NET_LIQUIDITY = 0.30
W_NET_VOLATILITY = 0.20
W_NET_TIGHTNESS = 0.10

#: An edge ratio (expected move / round trip) at or above this scores a full
#: cost point.  ~8 is roughly where BTC/USD sits at Tier-0 fees on a 60-minute
#: horizon, so the best pair the venue actually offers tops the scale.
EDGE_RATIO_FULL_MARK = 8.0

#: Horizon the entry's ``expected_move_bps`` is quoted at.  60 minutes because
#: that is where docs/ECONOMICS.md Part 4 measured a median move several times
#: the cost floor; at 1-2 minutes the move and the cost are the same size.
DEFAULT_HORIZON_MINUTES = 60.0

MINUTES_PER_DAY = 1440.0

#: Converting a 24h high-low *range* into a median absolute move over a shorter
#: horizon.  For a driftless random walk E[range] ~= 2.5307 sigma and the median
#: |move| is 0.6745 sigma, so median(H) ~= range_24h * 0.2665 * sqrt(H / 1440).
#: Checked against the measured table in docs/ECONOMICS.md Part 4: BTC/USD's
#: ~340 bps daily range implies 18.5 bps at 60 minutes against 19.7 measured.
#: It is an estimate, and it is used only to rank and to tier — never to size,
#: never to decide an entry.
RANGE_TO_SIGMA = 2.5307
SIGMA_TO_MEDIAN_ABS = 0.6745
RANGE_TO_MEDIAN_MOVE = SIGMA_TO_MEDIAN_ABS / RANGE_TO_SIGMA


class Tier(str, enum.Enum):
    """How often a pair is looked at, from every cycle down to a watchlist."""

    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


@dataclass(frozen=True)
class TierPolicy:
    """Sizes and cadences of the three tiers.

    The first five fields are the frozen Phase 5 contract.  The four that follow
    are the economics half of the same rule and all carry defaults, so every
    existing construction — positional or keyword — keeps working:

    * ``horizon_minutes`` — the holding period a pair's move is judged over.
    * ``hot_min_edge_ratio`` — a HOT slot costs a full-rate order book every
      cycle; do not spend one on a pair whose expected move is only just larger
      than its round trip, because the risk engine will refuse it every time.
      The edge ratio converts straight into the directional accuracy a
      symmetric-payoff trade needs to break even (:func:`break_even_hit_rate`),
      and 4.0 is p = 62.5% — the "demanding but not absurd" line that
      docs/ECONOMICS.md Part 4 draws for the 25-60 minute horizon.
    * ``warm_min_edge_ratio`` — 1.5 is p = 83%, which is not a strategy; it is
      the level at which a pair is worth *watching* in case its spread tightens
      or its volatility rises.  Below it the median move barely pays for the
      round trip at all and the pair is a watchlist entry (COLD).
    * ``exclude_below_edge_ratio`` — hard drop.  Defaults to 0.0, i.e. off: the
      liquidity bars and the fee-tier restriction already do the excluding, and
      a pair that is hopeless today can be promoted tomorrow.
    """

    hot_max: int = 8          # scanned every cycle
    warm_max: int = 20        # scanned every N cycles
    cold_max: int = 40        # refreshed on the slow loop only
    warm_every: int = 5
    cold_every: int = 60

    horizon_minutes: float = DEFAULT_HORIZON_MINUTES
    hot_min_edge_ratio: float = 4.0
    warm_min_edge_ratio: float = 1.5
    exclude_below_edge_ratio: float = 0.0

    def cap(self, tier: "Tier") -> int:
        return {Tier.HOT: self.hot_max, Tier.WARM: self.warm_max}.get(tier, self.cold_max)

    def every(self, tier: "Tier") -> int:
        """Cycles between scans: HOT is every cycle by definition."""
        return {Tier.HOT: 1, Tier.WARM: self.warm_every}.get(tier, self.cold_every)

    def due(self, tier: "Tier", cycle: int) -> bool:
        """Is ``tier`` scanned on this cycle index?  HOT always is."""
        every = max(1, self.every(tier))
        return cycle % every == 0


@dataclass(frozen=True)
class UniverseEntry:
    """One tradable pair with the metrics that earned it a place.

    Everything from ``fee_bps`` down is Phase 5 and defaulted, so an entry built
    the old way is still a valid entry — it simply reports a zero-cost, zero-edge
    economics block.  ``tier`` stays a plain ``str`` (the ``Tier`` values) rather
    than the enum: it is persisted through ``repo.upsert_markets`` and rendered
    in the dashboard, and ``str(Tier.HOT)`` is ``"Tier.HOT"``, not ``"hot"``.
    """

    symbol: str
    info: SymbolInfo
    quote_volume_24h: float
    trades_24h: int
    spread_bps: float
    volatility_bps: float
    depth_usd: float
    score: float
    tier: str  # "hot" | "warm" | "cold"

    #: Taker fee for one side, from the per-pair table: ~1 bps on the Tier-0
    #: list, 10 bps off it.  This is the whole of the tension in Phase 5.
    fee_bps: float = 0.0
    #: What a full in-and-out actually costs: both fees plus the spread crossed.
    round_trip_bps: float = 0.0
    #: Median absolute move over ``DEFAULT_HORIZON_MINUTES``, inferred from the
    #: 24h range.  An estimate for ranking, never an input to a trade decision.
    expected_move_bps: float = 0.0
    #: ``expected_move_bps / round_trip_bps``.  Below 1 the median move does not
    #: pay for the trade; ``inf`` when the round trip is genuinely zero.
    edge_ratio: float = 0.0
    #: Cost-aware score.  Orders the tier path; ``score`` still orders ``select``.
    net_score: float = 0.0
    #: Membership of the published Tier-0 list, recorded rather than re-derived:
    #: it is a property of the venue's fee table, not of the rate we happen to
    #: model (``FEE_MODEL=standard`` prices every pair at 10 bps but does not
    #: change which pairs are on the list).
    fee_tier0: bool = False

    def as_dict(self) -> dict:
        """Flat, JSON-safe view — the dashboard and the reports both want this."""
        return {
            "symbol": self.symbol,
            "base_asset": self.info.base_asset,
            "quote_asset": self.info.quote_asset,
            "tier": self.tier,
            "quote_volume_24h": self.quote_volume_24h,
            "trades_24h": self.trades_24h,
            "spread_bps": self.spread_bps,
            "volatility_bps": self.volatility_bps,
            "depth_usd": self.depth_usd,
            "fee_bps": self.fee_bps,
            "fee_tier0": self.fee_tier0,
            "round_trip_bps": self.round_trip_bps,
            "expected_move_bps": self.expected_move_bps,
            "edge_ratio": None if math.isinf(self.edge_ratio) else self.edge_ratio,
            "score": self.score,
            "net_score": self.net_score,
        }


def expected_move_bps(
    volatility_bps_24h: float, horizon_minutes: float = DEFAULT_HORIZON_MINUTES
) -> float:
    """Median absolute move over ``horizon_minutes``, inferred from a 24h range.

    The 24h high-low range is the only per-pair volatility figure the ticker
    snapshot gives for all 253 pairs, so the tiering has to work from it.  The
    conversion is the random-walk one (see ``RANGE_TO_MEDIAN_MOVE``) and it
    lands within ~6% of the directly measured 60-minute medians in
    docs/ECONOMICS.md Part 4 for BTC, ETH and SOL.

    Deliberately a *median*, not a mean: the tails on this venue are fat, and a
    mean would flatter every pair by the same tail the risk engine refuses to
    trade on.
    """
    if volatility_bps_24h <= 0.0 or horizon_minutes <= 0.0:
        return 0.0
    return volatility_bps_24h * RANGE_TO_MEDIAN_MOVE * math.sqrt(
        horizon_minutes / MINUTES_PER_DAY
    )


def round_trip_bps(
    symbol: str, spread_bps: float, fees: FeeConfig, *, maker: bool = False
) -> float:
    """Both fees plus the spread — what an in-and-out costs before slippage.

    The spread is counted once, not twice: crossing it on entry and again on
    exit costs half of it each time.  ``maker=True`` prices the passive version
    (0% on this venue), which is the only structure docs/ECONOMICS.md does not
    rule out; the default is the taker cost, because that is what the current
    system actually pays.
    """
    per_side = fees.maker_bps(symbol) if maker else fees.taker_bps(symbol)
    return 2.0 * per_side + max(0.0, spread_bps)


def edge_ratio(move_bps: float, cost_bps: float) -> float:
    """How many round trips the median move pays for.  1.0 is break-even.

    A zero cost is not a trick answer — a pure-maker fill on a Tier-0 pair
    really is free — so it returns ``inf`` rather than dividing by zero, and the
    callers treat ``inf`` as "unbounded edge", which it is.
    """
    if move_bps <= 0.0:
        return 0.0
    if cost_bps <= 0.0:
        return math.inf
    return move_bps / cost_bps


def break_even_hit_rate(ratio: float) -> float:
    """Directional accuracy a symmetric-payoff trade needs, from the edge ratio.

    ``p > 0.5 + costs / (2 * E|move|)`` (docs/ECONOMICS.md Part 4), and the edge
    ratio is ``E|move| / costs``, so this is just ``0.5 + 1 / (2 * ratio)``.  It
    is the number that makes a tier threshold arguable instead of arbitrary: an
    edge ratio of 2 sounds comfortable and means calling direction right 75% of
    the time, which nothing in this system does.
    """
    if ratio == math.inf:
        return 0.5
    if ratio <= 0.0:
        return 1.0
    return min(1.0, 0.5 + 1.0 / (2.0 * ratio))


def tier_for(entry: UniverseEntry, policy: TierPolicy, rank: int) -> Tier:
    """Which tier a pair belongs to, from its rank *and* its economics.

    Rank alone is not enough.  A pair can be the 3rd most attractive thing on
    the venue and still be something the risk engine will reject on every single
    cycle, because its round trip is larger than the move it is trying to catch
    — that is precisely BTCUSDT's position here.  Spending a HOT slot (a full
    order book, every cycle) on it buys nothing, so the economics can only ever
    demote: a good rank cannot rescue a pair whose move does not cover its cost,
    and a good cost cannot promote a pair the ranking put 200th.

    ``rank`` is "how many better pairs have already claimed a scanning slot",
    which for a caller walking a ranked list in order is just the index.  They
    differ only once a pair has been demoted on economics, and there the slot
    interpretation is the useful one: a HOT slot vacated by a pair that cannot
    clear its costs should go to the next pair that can, not sit empty while
    the universe stays narrower than the policy allows.  :meth:`select_tiers`
    passes the slot count for exactly that reason.
    """
    ratio = edge_ratio(
        expected_move_bps(entry.volatility_bps, policy.horizon_minutes),
        entry.round_trip_bps,
    )
    rank = max(0, rank)
    if ratio < policy.warm_min_edge_ratio:
        return Tier.COLD
    if rank < policy.hot_max and ratio >= policy.hot_min_edge_ratio:
        return Tier.HOT
    if rank < policy.hot_max + policy.warm_max:
        return Tier.WARM
    return Tier.COLD


def symbols_by_tier(entries: list[UniverseEntry]) -> dict[str, list[str]]:
    """``{"hot": [...], "warm": [...], "cold": [...]}`` in rank order.

    Shaped for ``BinanceUSFeed.set_universe(hot, warm)``: COLD is deliberately a
    third list rather than folded into WARM, because subscribing to a cold pair
    is exactly the cost the tiering exists to avoid.
    """
    out: dict[str, list[str]] = {t.value: [] for t in Tier}
    for entry in entries:
        out.setdefault(str(entry.tier), []).append(entry.symbol)
    return out


class UniverseSelector:
    """Turns raw 24h tickers + exchange info into the ranked scan list."""

    def __init__(self, *, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        #: Reasons the last ``select`` rejected each symbol — dashboard fodder.
        self.rejections: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # selection
    # ------------------------------------------------------------------ #

    def select(
        self,
        tickers: list[dict],
        infos: dict[str, SymbolInfo],
        *,
        books: dict[str, OrderBook] | None = None,
    ) -> list[UniverseEntry]:
        """Apply every filter, score, sort, truncate and tag.

        Pure and deterministic: no clock, no randomness, no network.  When
        ``books`` is supplied the spread/depth gates apply too; without it a
        symbol is judged on 24h statistics alone and carries the configured
        maximum spread as a deliberately pessimistic placeholder.
        """
        u = self.settings.universe
        ranked = self._candidates(tickers, infos, books)[: max(0, u.max_symbols)]
        return [
            replace(e, tier=Tier.HOT.value if i < u.hot_symbols else Tier.WARM.value)
            for i, e in enumerate(ranked)
        ]

    # ------------------------------------------------------------------ #
    # tiering — the whole venue, ranked by what a trade would net
    # ------------------------------------------------------------------ #

    def select_tiers(
        self,
        tickers: list[dict],
        infos: dict[str, SymbolInfo],
        *,
        books: dict[str, OrderBook] | None = None,
        policy: TierPolicy | None = None,
        max_symbols: int | None = None,
    ) -> list[UniverseEntry]:
        """Rank the eligible venue cost-first and tag every survivor with a tier.

        The same filters as :meth:`select` — a pair that is not tradable is not
        tradable in any tier — but three differences after that:

        1.  the ordering is ``net_score``, so a cheap pair beats a marginally
            deeper expensive one instead of losing to it;
        2.  the USD/USDT dedupe therefore keeps the *affordable* leg of a base
            asset rather than the busiest one;
        3.  ``max_symbols`` no longer truncates.  The per-tier caps do, which is
            the whole point: the cap that used to end the universe at 12 now
            just ends the HOT tier at 8 and lets 60 more pairs be watched.

        Slots are filled greedily in rank order: the best pair whose economics
        clear the HOT bar takes the next HOT slot even if better-ranked pairs
        ahead of it were demoted.  Demotion is a statement about one pair's
        costs, not a reason to run a smaller universe than the policy allows.

        ``max_symbols`` is still honoured when a caller passes it explicitly —
        it caps the ranked list *before* tiering, so the tiers are drawn from
        exactly the pairs the caller allowed and nothing is silently ignored.
        """
        policy = policy or TierPolicy()
        ranked = self._candidates(tickers, infos, books, key="net_score")
        if max_symbols is not None:
            ranked = ranked[: max(0, max_symbols)]

        counts: dict[Tier, int] = {t: 0 for t in Tier}
        out: list[UniverseEntry] = []
        for entry in ranked:
            if entry.edge_ratio < policy.exclude_below_edge_ratio:
                self.rejections[entry.symbol] = (
                    f"edge ratio {entry.edge_ratio:.2f} below "
                    f"{policy.exclude_below_edge_ratio:.2f}"
                )
                continue
            # The rank handed to ``tier_for`` is the next *free* scanning slot,
            # not the raw list index.  The two are identical while nothing is
            # demoted; once a top-ranked pair is demoted on economics, the slot
            # it did not use is offered to the next pair that can use it rather
            # than left empty.
            tier = tier_for(entry, policy, _next_free_slot(counts, policy))
            # Demote, never promote: a full HOT tier spills into WARM, a full
            # WARM into COLD, and a pair that cannot fit COLD is off the list.
            for candidate in _DEMOTION_PATH[tier]:
                if counts[candidate] < policy.cap(candidate):
                    counts[candidate] += 1
                    out.append(replace(entry, tier=candidate.value))
                    break
            else:
                self.rejections[entry.symbol] = "every tier full"
        return out

    async def refresh_tiers(
        self, rest: "BinanceUSRest", *, policy: TierPolicy | None = None  # noqa: F821
    ) -> list[UniverseEntry]:
        """:meth:`refresh` for the tier path: books for HOT/WARM, stats for COLD.

        COLD deliberately gets no ``/depth`` call.  A watchlist that costs 60
        REST round trips a cycle is not a watchlist, and a cold pair is judged
        on 24h statistics alone until the day it ranks high enough to be probed.
        """
        policy = policy or TierPolicy()
        infos = await rest.exchange_info()
        tickers = await rest.ticker_24h()

        probe_depth = max(0, policy.hot_max + policy.warm_max)
        shortlist = self._candidates(tickers, infos, None, key="net_score")[:probe_depth]
        if not shortlist:
            return []

        symbols = [e.symbol for e in shortlist]
        results = await asyncio.gather(
            *(rest.depth(sym, limit=20) for sym in symbols), return_exceptions=True
        )
        books = {
            sym: res
            for sym, res in zip(symbols, results)
            if isinstance(res, OrderBook) and res.is_valid()
        }
        return self.select_tiers(tickers, infos, books=books, policy=policy)

    def _candidates(
        self,
        tickers: list[dict],
        infos: dict[str, SymbolInfo],
        books: dict[str, OrderBook] | None,
        *,
        key: str = "score",
    ) -> list[UniverseEntry]:
        """Everything that survives the filters, deduped and sorted best-first.

        ``key`` picks the ranking: ``"score"`` is the original liquidity-first
        blend that :meth:`select` has always used, ``"net_score"`` the
        cost-aware one.  It also decides the dedupe, which matters more than the
        ordering does — dedupe on volume and BTCUSDT wins the BTC slot at 20 bps
        a round trip; dedupe on net and BTCUSD keeps it at ~2.
        """
        self.rejections = {}
        candidates: list[UniverseEntry] = []
        for raw in tickers or []:
            if not isinstance(raw, dict):
                continue
            symbol = str(raw.get("symbol", "")).upper()
            if not symbol:
                continue
            entry = self._evaluate(symbol, raw, infos, books)
            if entry is not None:
                candidates.append(entry)
        # The fee-tier restriction has to bite BEFORE the USD/USDT dedupe.
        # Doing it after is a trap: BTCUSDT out-turns BTCUSD on volume, so the
        # dedupe discards the Tier-0 pair, the restriction then finds nothing,
        # and the fallback quietly puts us on a 10 bps/side pair — precisely
        # the case docs/ECONOMICS.md says cannot be profitable.
        if self.settings.universe.restrict_to_fee_tier0:
            tier0 = self.settings.fees.tier0_symbols
            eligible = [e for e in candidates if e.symbol in tier0]
            # A stale fee table must degrade to "trade badly", not "go blind".
            if eligible:
                candidates = eligible
        deduped = self._dedupe_quotes(candidates, key=key)
        return sorted(deduped, key=lambda e: (-getattr(e, key), e.symbol))

    def _evaluate(
        self,
        symbol: str,
        raw: dict,
        infos: dict[str, SymbolInfo],
        books: dict[str, OrderBook] | None,
    ) -> UniverseEntry | None:
        u = self.settings.universe
        info = infos.get(symbol)
        if info is None:
            return self._reject(symbol, "no exchangeInfo entry")
        if info.status != "TRADING":
            return self._reject(symbol, f"status {info.status}")
        if info.quote_asset not in self.settings.exchange.quote_assets:
            return self._reject(symbol, f"quote {info.quote_asset} not tradable")
        if info.base_asset in u.deny_bases:
            return self._reject(symbol, f"base {info.base_asset} denied")
        if any(bad in symbol for bad in u.deny_substrings):
            return self._reject(symbol, "denylisted symbol pattern")

        quote_volume = _f(raw.get("quoteVolume"))
        if quote_volume < u.min_quote_volume_24h:
            return self._reject(symbol, f"24h quote volume {quote_volume:,.0f} too low")

        trades = _i(raw.get("count"))
        if trades < u.min_trades_24h:
            return self._reject(symbol, f"24h trade count {trades} too low")

        volatility_bps = _volatility_bps(raw)
        if volatility_bps < u.min_volatility_bps:
            return self._reject(symbol, f"volatility {volatility_bps:.1f}bps too low")
        if volatility_bps > u.max_volatility_bps:
            return self._reject(symbol, f"volatility {volatility_bps:.1f}bps too high")

        book = (books or {}).get(symbol)
        if book is not None:
            spread_bps = book.spread_bps
            depth_usd = min(book.depth_usd("bid", DEPTH_BPS), book.depth_usd("ask", DEPTH_BPS))
            if not math.isfinite(spread_bps) or spread_bps > u.max_spread_bps:
                return self._reject(symbol, f"spread {spread_bps:.1f}bps too wide")
            if depth_usd < u.min_depth_usd:
                return self._reject(symbol, f"depth ${depth_usd:,.0f} too thin")
        else:
            # Unknown until the feed warms up; assume the worst tolerable value
            # so a book-less candidate never out-scores a measured tight one.
            spread_bps = u.max_spread_bps
            depth_usd = 0.0

        fees = self.settings.fees
        # The economics use the best spread we actually know, in this order:
        # measured book, then the ticker's own bid/ask, then the pessimistic
        # placeholder.  ``spread_bps`` above keeps its original meaning (book or
        # placeholder) because existing callers read it as a gate, not a cost;
        # charging a book-less pair 8 bps of *cost* when the venue is quoting it
        # at 0.5 would bury every cold pair under a spread it never pays.
        cost_spread = spread_bps if book is not None else _quoted_spread_bps(raw, u.max_spread_bps)
        trip = round_trip_bps(symbol, cost_spread, fees)
        move = expected_move_bps(volatility_bps, DEFAULT_HORIZON_MINUTES)

        return UniverseEntry(
            symbol=symbol,
            info=info,
            quote_volume_24h=quote_volume,
            trades_24h=trades,
            spread_bps=spread_bps,
            volatility_bps=volatility_bps,
            depth_usd=depth_usd,
            score=self.score(quote_volume, volatility_bps, spread_bps),
            tier=Tier.WARM.value,
            fee_bps=fees.taker_bps(symbol),
            round_trip_bps=trip,
            expected_move_bps=move,
            edge_ratio=edge_ratio(move, trip),
            net_score=self.score(
                quote_volume, volatility_bps, cost_spread, round_trip_bps=trip
            ),
            fee_tier0=symbol in fees.tier0_symbols,
        )

    def score(
        self,
        quote_volume: float,
        volatility_bps: float,
        spread_bps: float,
        *,
        round_trip_bps: float | None = None,
        horizon_minutes: float = DEFAULT_HORIZON_MINUTES,
    ) -> float:
        """Weighted blend of liquidity, volatility and tightness, all in [0, 1].

        * **liquidity** — log-scaled 24h quote volume; the configured minimum
          scores 0 and ``LIQUIDITY_SPAN``x the minimum scores 1.  Log, because
          the difference between $2M and $20M matters far more than between
          $200M and $220M.
        * **volatility** — linear between the configured floor and ceiling.
          Movement is the raw material of a scalp; the ceiling already excludes
          the dangerous tail, so inside the band more is better.
        * **tightness** — 1 at a zero spread, 0 at the configured maximum.  This
          is the cost side of the same trade.

        With ``round_trip_bps`` supplied a fourth, heaviest component joins and
        the weights change to the ``W_NET_*`` set: **cost**, the pair's median
        move divided by what an in-and-out costs, full marks at
        ``EDGE_RATIO_FULL_MARK``.  Without it the function is byte-for-byte what
        it was, because ``select`` and everything downstream of it are not
        supposed to move.
        """
        u = self.settings.universe
        floor = max(1.0, u.min_quote_volume_24h)
        liquidity = _clamp01(
            math.log10(max(quote_volume, floor) / floor) / math.log10(LIQUIDITY_SPAN)
        )
        span = max(1e-9, u.max_volatility_bps - u.min_volatility_bps)
        volatility = _clamp01((volatility_bps - u.min_volatility_bps) / span)
        tightness = _clamp01(1.0 - spread_bps / max(1e-9, u.max_spread_bps))
        if round_trip_bps is None:
            return round(
                W_LIQUIDITY * liquidity + W_VOLATILITY * volatility + W_TIGHTNESS * tightness,
                10,
            )

        # Cost-aware variant.  Volume is not comparable across fee tiers: a
        # $4.9M pair at 10 bps a side and a $1.4M pair at 1 bps are not the same
        # opportunity, and ranking them on volume says they are.  The cost term
        # is the median move divided by the round trip, i.e. how many times over
        # the pair's own volatility pays for the transaction.
        ratio = edge_ratio(expected_move_bps(volatility_bps, horizon_minutes), round_trip_bps)
        cost = 1.0 if math.isinf(ratio) else _clamp01(ratio / EDGE_RATIO_FULL_MARK)
        return round(
            W_NET_COST * cost
            + W_NET_LIQUIDITY * liquidity
            + W_NET_VOLATILITY * volatility
            + W_NET_TIGHTNESS * tightness,
            10,
        )

    def _dedupe_quotes(
        self, entries: list[UniverseEntry], *, key: str = "score"
    ) -> list[UniverseEntry]:
        """One pair per base asset — BTCUSD and BTCUSDT are the same exposure."""
        best: dict[str, UniverseEntry] = {}
        for entry in entries:
            base = entry.info.base_asset
            incumbent = best.get(base)
            if incumbent is None or self._prefer(entry, incumbent, key=key):
                best[base] = entry
        return list(best.values())

    def _prefer(self, a: UniverseEntry, b: UniverseEntry, *, key: str = "score") -> bool:
        """True if ``a`` should replace ``b``: score, then quote order, then name."""
        if getattr(a, key) != getattr(b, key):
            return getattr(a, key) > getattr(b, key)
        quotes = self.settings.exchange.quote_assets
        rank_a = _index_or_last(quotes, a.info.quote_asset)
        rank_b = _index_or_last(quotes, b.info.quote_asset)
        if rank_a != rank_b:
            return rank_a < rank_b
        return a.symbol < b.symbol

    def _reject(self, symbol: str, why: str) -> None:
        self.rejections[symbol] = why
        return None

    # ------------------------------------------------------------------ #
    # live refresh
    # ------------------------------------------------------------------ #

    async def refresh(self, rest: "BinanceUSRest") -> list[UniverseEntry]:  # noqa: F821
        """Fetch, shortlist, measure the shortlist's books, then re-select.

        Two passes so the spread/depth gates are real without fetching depth for
        every pair on the exchange: the statistics pass narrows to a shortlist,
        and only those get a ``/depth`` call (bounded by the REST semaphore).
        """
        infos = await rest.exchange_info()
        tickers = await rest.ticker_24h()

        # Shortlist wider than the final list: the book gates below will drop
        # some, and we would rather refill the slot than run a short universe.
        shortlist = self._candidates(tickers, infos, None)[: self.settings.universe.max_symbols * 2]
        if not shortlist:
            return []

        symbols = [e.symbol for e in shortlist]
        results = await asyncio.gather(
            *(rest.depth(sym, limit=20) for sym in symbols), return_exceptions=True
        )
        books = {
            sym: res
            for sym, res in zip(symbols, results)
            if isinstance(res, OrderBook) and res.is_valid()
        }
        by_symbol = {str(t.get("symbol", "")).upper(): t for t in tickers if isinstance(t, dict)}
        relevant = [by_symbol[s] for s in symbols if s in by_symbol]
        return self.select(relevant, infos, books=books)


# --------------------------------------------------------------------------- #
# the trade-off, as a table
# --------------------------------------------------------------------------- #

#: Where a tier spills to when its cap is full.  COLD spilling nowhere means the
#: pair leaves the universe, which is the only place a cap can drop a pair.
_DEMOTION_PATH: dict[Tier, tuple[Tier, ...]] = {
    Tier.HOT: (Tier.HOT, Tier.WARM, Tier.COLD),
    Tier.WARM: (Tier.WARM, Tier.COLD),
    Tier.COLD: (Tier.COLD,),
}

#: (min 24h quote volume, min 24h trades).  Spans three orders of magnitude
#: because the question is not "how many pairs clear the current bar" but "does
#: lowering the bar buy anything real"; $100k/200 is today's default and sits in
#: the middle on purpose.
DEFAULT_LIQUIDITY_BARS: tuple[tuple[float, int], ...] = (
    (10_000.0, 20),
    (50_000.0, 100),
    (100_000.0, 200),
    (250_000.0, 500),
    (1_000_000.0, 2_000),
    (5_000_000.0, 10_000),
)


@dataclass(frozen=True)
class LiquidityBar:
    """One row of the widen-or-not table: who clears this bar, and at what cost."""

    min_quote_volume_24h: float
    min_trades_24h: int
    tier0_count: int
    other_count: int
    tier0_median_symbol: str
    other_median_symbol: str
    tier0_median_quote_volume: float
    other_median_quote_volume: float
    tier0_median_round_trip_bps: float
    other_median_round_trip_bps: float
    tier0_median_expected_move_bps: float
    other_median_expected_move_bps: float

    @property
    def total_count(self) -> int:
        return self.tier0_count + self.other_count

    def as_dict(self) -> dict:
        return {
            "min_quote_volume_24h": self.min_quote_volume_24h,
            "min_trades_24h": self.min_trades_24h,
            "tier0_count": self.tier0_count,
            "other_count": self.other_count,
            "total_count": self.total_count,
            "tier0_median_symbol": self.tier0_median_symbol,
            "other_median_symbol": self.other_median_symbol,
            "tier0_median_quote_volume": self.tier0_median_quote_volume,
            "other_median_quote_volume": self.other_median_quote_volume,
            "tier0_median_round_trip_bps": self.tier0_median_round_trip_bps,
            "other_median_round_trip_bps": self.other_median_round_trip_bps,
            "tier0_median_expected_move_bps": self.tier0_median_expected_move_bps,
            "other_median_expected_move_bps": self.other_median_expected_move_bps,
        }


def liquidity_profile(
    tickers: list[dict],
    infos: dict[str, SymbolInfo] | None = None,
    *,
    bars: tuple[tuple[float, int], ...] = DEFAULT_LIQUIDITY_BARS,
    settings: Settings | None = None,
) -> list[LiquidityBar]:
    """How many pairs clear each liquidity bar, split by Tier-0 membership.

    This is the measurement that decides whether widening the universe is worth
    anything.  Counting pairs alone cannot answer it: lowering the bar always
    finds more pairs, and almost all of them are off the Tier-0 list and cost
    ~20 bps a round trip, which on this venue is the whole of a 60-minute move.
    So each row carries the *median pair's* round trip and its median move
    beside the counts — if the extra pairs arrive with a cost larger than their
    move, the widening added scanning, not opportunity.

    It lives here rather than in a script so it can be re-run against a fresh
    snapshot when the fee table changes, which docs/ECONOMICS.md says it will.
    """
    s = settings or get_settings()
    rows = _eligible_rows(tickers, infos, s)

    out: list[LiquidityBar] = []
    for min_volume, min_trades in bars:
        passing = [
            r
            for r in rows
            if r["quote_volume"] >= min_volume and r["trades"] >= min_trades
        ]
        tier0 = sorted(
            (r for r in passing if r["tier0"]), key=lambda r: (r["quote_volume"], r["symbol"])
        )
        other = sorted(
            (r for r in passing if not r["tier0"]), key=lambda r: (r["quote_volume"], r["symbol"])
        )
        a, b = _median_row(tier0), _median_row(other)
        out.append(
            LiquidityBar(
                min_quote_volume_24h=min_volume,
                min_trades_24h=min_trades,
                tier0_count=len(tier0),
                other_count=len(other),
                tier0_median_symbol=a["symbol"],
                other_median_symbol=b["symbol"],
                tier0_median_quote_volume=a["quote_volume"],
                other_median_quote_volume=b["quote_volume"],
                tier0_median_round_trip_bps=a["round_trip_bps"],
                other_median_round_trip_bps=b["round_trip_bps"],
                tier0_median_expected_move_bps=a["expected_move_bps"],
                other_median_expected_move_bps=b["expected_move_bps"],
            )
        )
    return out


def format_liquidity_profile(rows: list[LiquidityBar]) -> str:
    """The same table as markdown, for a report or a log line."""
    head = (
        "| 24h volume | trades | Tier-0 | other | T0 median pair | T0 RT bps | "
        "T0 move bps | other median pair | other RT bps | other move bps |\n"
        "|---|---|---|---|---|---|---|---|---|---|"
    )
    lines = [head]
    for r in rows:
        lines.append(
            f"| ${r.min_quote_volume_24h:,.0f} | {r.min_trades_24h:,} | {r.tier0_count} | "
            f"{r.other_count} | {r.tier0_median_symbol or '-'} | "
            f"{r.tier0_median_round_trip_bps:.1f} | {r.tier0_median_expected_move_bps:.1f} | "
            f"{r.other_median_symbol or '-'} | {r.other_median_round_trip_bps:.1f} | "
            f"{r.other_median_expected_move_bps:.1f} |"
        )
    return "\n".join(lines)


def _eligible_rows(
    tickers: list[dict], infos: dict[str, SymbolInfo] | None, s: Settings
) -> list[dict]:
    """Venue rows worth counting: TRADING, quoted in a tradable asset, not denied.

    ``infos`` is optional so the table can be built from a bare ticker snapshot,
    in which case the quote asset is inferred from the symbol suffix.  The
    denylists still apply either way — counting leveraged tokens as available
    opportunity would be flattering the answer.
    """
    u = s.universe
    quotes = tuple(s.exchange.quote_assets)
    out: list[dict] = []
    seen: set[str] = set()
    for raw in tickers or []:
        if not isinstance(raw, dict):
            continue
        symbol = str(raw.get("symbol", "")).upper()
        if not symbol or symbol in seen:
            continue
        info = (infos or {}).get(symbol)
        if info is not None:
            if info.status != "TRADING" or info.quote_asset not in quotes:
                continue
            base = info.base_asset
        else:
            quote = next(
                (q for q in sorted(quotes, key=len, reverse=True) if symbol.endswith(q)), None
            )
            if quote is None:
                continue
            base = symbol[: -len(quote)]
        if base in u.deny_bases or any(bad in symbol for bad in u.deny_substrings):
            continue
        seen.add(symbol)
        volatility = _volatility_bps(raw)
        spread = _quoted_spread_bps(raw, u.max_spread_bps)
        out.append(
            {
                "symbol": symbol,
                "quote_volume": _f(raw.get("quoteVolume")),
                "trades": _i(raw.get("count")),
                "tier0": symbol in s.fees.tier0_symbols,
                "volatility_bps": volatility,
                "expected_move_bps": expected_move_bps(volatility),
                "round_trip_bps": round_trip_bps(symbol, spread, s.fees),
            }
        )
    return out


_EMPTY_ROW = {
    "symbol": "",
    "quote_volume": 0.0,
    "trades": 0,
    "tier0": False,
    "volatility_bps": 0.0,
    "expected_move_bps": 0.0,
    "round_trip_bps": 0.0,
}


def _next_free_slot(counts: dict[Tier, int], policy: TierPolicy) -> int:
    """The rank of the first unclaimed scanning slot, in ``tier_for``'s terms.

    HOT occupies ranks ``[0, hot_max)`` and WARM ``[hot_max, hot_max + warm_max)``,
    so "the first free slot" is the rank a new pair is really competing for.
    Feeding the raw list index instead would let a demoted pair consume a slot
    it was never given.
    """
    if counts[Tier.HOT] < policy.hot_max:
        return counts[Tier.HOT]
    if counts[Tier.WARM] < policy.warm_max:
        return policy.hot_max + counts[Tier.WARM]
    return policy.hot_max + policy.warm_max


def _median_row(rows: list[dict]) -> dict:
    """The lower-middle element of a volume-sorted list, or an empty stand-in.

    Lower-middle rather than an interpolated midpoint because the row has to be
    a real pair — "the median pair costs 20 bps" is a statement you can go and
    check, "the median costs 11 bps" when no pair does is not.
    """
    if not rows:
        return dict(_EMPTY_ROW)
    return rows[(len(rows) - 1) // 2]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _quoted_spread_bps(raw: dict, fallback: float) -> float:
    """Spread from the ticker's own top of book, or ``fallback`` if it has none.

    ``/ticker/24hr`` carries ``bidPrice``/``askPrice`` on Binance.US, which is
    how a pair we have never fetched depth for can still be costed honestly.
    """
    bid = _f(raw.get("bidPrice"))
    ask = _f(raw.get("askPrice"))
    if bid <= 0.0 or ask <= 0.0 or ask < bid:
        return fallback
    mid = (ask + bid) / 2.0
    if mid <= 0.0:
        return fallback
    return (ask - bid) / mid * 10_000.0


def _volatility_bps(raw: dict) -> float:
    """24h high-low range as bps of the 24h VWAP.

    ``weightedAvgPrice`` is the right denominator — it is the average price the
    market actually traded at, so the figure is comparable across pairs whose
    last price happens to sit at an extreme.
    """
    high = _f(raw.get("highPrice"))
    low = _f(raw.get("lowPrice"))
    ref = _f(raw.get("weightedAvgPrice"))
    if ref <= 0:
        ref = _f(raw.get("lastPrice"))
    if ref <= 0 or high <= 0 or high < low:
        return 0.0
    return (high - low) / ref * 10_000.0


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def _index_or_last(items: tuple[str, ...], value: str) -> int:
    return items.index(value) if value in items else len(items)


def _f(value, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(out) else out


def _i(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
