"""Which pairs are even worth scanning.

Universe selection is the cheapest risk control we have: a symbol that never
enters the list can never produce a bad fill.  The rules come straight from
``UniverseConfig`` and are applied in one pure, deterministic pass so the same
exchange snapshot always yields the same list — tests and the dashboard both
depend on that.

Scoring is intentionally *absolute*, not relative to the candidate set: each
component is normalised against a configured threshold, so adding or removing
one candidate never re-orders the others.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, replace

from .config import Settings, get_settings
from .types import OrderBook, SymbolInfo

#: Score weights — liquidity dominates because it is what makes a scalp exitable.
W_LIQUIDITY = 0.45
W_VOLATILITY = 0.35
W_TIGHTNESS = 0.20

#: Volume this many times the configured minimum scores a full liquidity point.
LIQUIDITY_SPAN = 100.0

#: Depth is measured this far from mid on each side.
DEPTH_BPS = 10.0


@dataclass(frozen=True)
class UniverseEntry:
    """One tradable pair with the metrics that earned it a place."""

    symbol: str
    info: SymbolInfo
    quote_volume_24h: float
    trades_24h: int
    spread_bps: float
    volatility_bps: float
    depth_usd: float
    score: float
    tier: str  # "hot" | "warm"


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
            replace(e, tier="hot" if i < u.hot_symbols else "warm")
            for i, e in enumerate(ranked)
        ]

    def _candidates(
        self,
        tickers: list[dict],
        infos: dict[str, SymbolInfo],
        books: dict[str, OrderBook] | None,
    ) -> list[UniverseEntry]:
        """Everything that survives the filters, deduped and sorted best-first."""
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
        deduped = self._dedupe_quotes(candidates)
        return sorted(deduped, key=lambda e: (-e.score, e.symbol))

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

        return UniverseEntry(
            symbol=symbol,
            info=info,
            quote_volume_24h=quote_volume,
            trades_24h=trades,
            spread_bps=spread_bps,
            volatility_bps=volatility_bps,
            depth_usd=depth_usd,
            score=self.score(quote_volume, volatility_bps, spread_bps),
            tier="warm",
        )

    def score(self, quote_volume: float, volatility_bps: float, spread_bps: float) -> float:
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
        """
        u = self.settings.universe
        floor = max(1.0, u.min_quote_volume_24h)
        liquidity = _clamp01(
            math.log10(max(quote_volume, floor) / floor) / math.log10(LIQUIDITY_SPAN)
        )
        span = max(1e-9, u.max_volatility_bps - u.min_volatility_bps)
        volatility = _clamp01((volatility_bps - u.min_volatility_bps) / span)
        tightness = _clamp01(1.0 - spread_bps / max(1e-9, u.max_spread_bps))
        return round(
            W_LIQUIDITY * liquidity + W_VOLATILITY * volatility + W_TIGHTNESS * tightness,
            10,
        )

    def _dedupe_quotes(self, entries: list[UniverseEntry]) -> list[UniverseEntry]:
        """One pair per base asset — BTCUSD and BTCUSDT are the same exposure."""
        best: dict[str, UniverseEntry] = {}
        for entry in entries:
            base = entry.info.base_asset
            incumbent = best.get(base)
            if incumbent is None or self._prefer(entry, incumbent):
                best[base] = entry
        return list(best.values())

    def _prefer(self, a: UniverseEntry, b: UniverseEntry) -> bool:
        """True if ``a`` should replace ``b``: score, then quote order, then name."""
        if a.score != b.score:
            return a.score > b.score
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
# helpers
# --------------------------------------------------------------------------- #


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
