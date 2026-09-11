"""Hand-built market-data fixtures.

Everything here is deterministic and offline: no network, no wall clock, no
randomness.  Timestamps are derived from :data:`BASE_MS` so a test can always
say exactly how old a book or a trade is.
"""

from __future__ import annotations

from app.types import (
    Bar,
    Features,
    MarketSnapshot,
    OrderBook,
    BookLevel,
    Regime,
    RegimeState,
    Side,
    Signal,
    SymbolInfo,
    Trade,
)

SYMBOL = "BTCUSD"
BASE_MS = 1_700_000_000_000       # a fixed, arbitrary epoch-ms anchor
MINUTE_MS = 60_000


# --------------------------------------------------------------------------- #
# Static metadata
# --------------------------------------------------------------------------- #


def make_info(symbol: str = SYMBOL, **over) -> SymbolInfo:
    kwargs = dict(
        symbol=symbol,
        base_asset=symbol[:-3],
        quote_asset=symbol[-3:],
        status="TRADING",
        tick_size=0.01,
        step_size=0.00001,
        min_qty=0.00001,
        min_notional=1.0,
    )
    kwargs.update(over)
    return SymbolInfo(**kwargs)


# --------------------------------------------------------------------------- #
# Bars
# --------------------------------------------------------------------------- #


def make_bar(
    *,
    symbol: str = SYMBOL,
    index: int = 0,
    open_: float = 100.0,
    high: float | None = None,
    low: float | None = None,
    close: float = 100.0,
    quote_volume: float = 100_000.0,
    trades: int = 500,
    start_ms: int = BASE_MS,
) -> Bar:
    hi = high if high is not None else max(open_, close)
    lo = low if low is not None else min(open_, close)
    volume = quote_volume / close if close > 0 else 0.0
    return Bar(
        symbol=symbol,
        open_ms=start_ms + index * MINUTE_MS,
        close_ms=start_ms + (index + 1) * MINUTE_MS - 1,
        open=open_,
        high=hi,
        low=lo,
        close=close,
        volume=volume,
        quote_volume=quote_volume,
        trades=trades,
        taker_buy_quote=quote_volume / 2.0,
    )


def make_bars(
    closes: list[float],
    *,
    symbol: str = SYMBOL,
    start_ms: int = BASE_MS,
    wick_frac: float = 0.0005,
    quote_volumes: list[float] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> list[Bar]:
    """Build a bar series from a close path.

    ``open`` chains from the previous close; wicks are a fixed fraction either
    side unless explicit ``highs``/``lows`` are supplied.
    """
    bars: list[Bar] = []
    prev = closes[0] if closes else 0.0
    for i, c in enumerate(closes):
        o = prev
        hi = highs[i] if highs else max(o, c) * (1.0 + wick_frac)
        lo = lows[i] if lows else min(o, c) * (1.0 - wick_frac)
        qv = quote_volumes[i] if quote_volumes else 100_000.0
        bars.append(
            make_bar(
                symbol=symbol,
                index=i,
                open_=o,
                high=hi,
                low=lo,
                close=c,
                quote_volume=qv,
                start_ms=start_ms,
            )
        )
        prev = c
    return bars


def flat_closes(n: int, price: float = 100.0) -> list[float]:
    return [price] * n


def trend_closes(n: int, start: float = 100.0, step_bps: float = 5.0) -> list[float]:
    """A steady up-drift of ``step_bps`` per bar."""
    out = [start]
    for _ in range(n - 1):
        out.append(out[-1] * (1.0 + step_bps / 10_000.0))
    return out


def zigzag_closes(n: int, start: float = 100.0, amp_bps: float = 10.0) -> list[float]:
    """Alternating up/down bars — a range with non-zero realized vol."""
    out = []
    for i in range(n):
        out.append(start * (1.0 + (amp_bps / 10_000.0) * (1 if i % 2 else -1)))
    return out


# --------------------------------------------------------------------------- #
# Order books
# --------------------------------------------------------------------------- #


def make_book(
    *,
    symbol: str = SYMBOL,
    mid: float = 100.0,
    spread_bps: float = 2.0,
    bid_qty: float = 50.0,
    ask_qty: float = 50.0,
    levels: int = 5,
    level_step_bps: float = 2.0,
    ts_ms: int = BASE_MS,
    last_update_id: int = 1,
    desynced: bool = False,
) -> OrderBook:
    half = mid * spread_bps / 2.0 / 10_000.0
    step = mid * level_step_bps / 10_000.0
    bids = [BookLevel(price=mid - half - i * step, qty=bid_qty) for i in range(levels)]
    asks = [BookLevel(price=mid + half + i * step, qty=ask_qty) for i in range(levels)]
    return OrderBook(
        symbol=symbol,
        bids=bids,
        asks=asks,
        last_update_id=last_update_id,
        last_update_ms=ts_ms,
        desynced=desynced,
    )


def empty_book(symbol: str = SYMBOL) -> OrderBook:
    return OrderBook(symbol=symbol)


# --------------------------------------------------------------------------- #
# Trades
# --------------------------------------------------------------------------- #


def make_trade(
    *,
    symbol: str = SYMBOL,
    price: float = 100.0,
    notional: float = 1_000.0,
    ts_ms: int = BASE_MS,
    is_buy: bool = True,
) -> Trade:
    return Trade(
        symbol=symbol,
        price=price,
        qty=notional / price,
        ts_ms=ts_ms,
        # buyer_is_maker True means the aggressor was the SELLER.
        buyer_is_maker=not is_buy,
    )


def flow_trades(
    *,
    symbol: str = SYMBOL,
    ts_ms: int = BASE_MS,
    imb_60: float = 0.0,
    imb_15: float | None = None,
    total_60: float = 100_000.0,
    frac_15: float = 0.25,
    price: float = 100.0,
) -> list[Trade]:
    """Trades that produce exactly the requested 60 s / 15 s flow imbalances.

    ``imb_15`` defaults to ``imb_60`` (no acceleration).  The split is exact, so
    a test can assert on ``trade_flow_imbalance`` and ``trade_flow_accel``
    without tolerance games.
    """
    if imb_15 is None:
        imb_15 = imb_60
    total_15 = total_60 * frac_15
    total_old = total_60 - total_15
    diff_15 = imb_15 * total_15
    diff_old = imb_60 * total_60 - diff_15
    assert abs(diff_old) <= total_old + 1e-9, "requested imbalances are unreachable"

    buy_15 = (total_15 + diff_15) / 2.0
    sell_15 = (total_15 - diff_15) / 2.0
    buy_old = (total_old + diff_old) / 2.0
    sell_old = (total_old - diff_old) / 2.0

    out: list[Trade] = []
    for age_ms, notional, is_buy in (
        (5_000, buy_15, True),
        (6_000, sell_15, False),
        (30_000, buy_old, True),
        (31_000, sell_old, False),
    ):
        if notional > 1e-12:
            out.append(
                make_trade(
                    symbol=symbol,
                    price=price,
                    notional=notional,
                    ts_ms=ts_ms - age_ms,
                    is_buy=is_buy,
                )
            )
    return out


# --------------------------------------------------------------------------- #
# Snapshots
# --------------------------------------------------------------------------- #


def make_snapshot(
    *,
    symbol: str = SYMBOL,
    ts_ms: int | None = None,
    bars: list[Bar] | None = None,
    book: OrderBook | None = None,
    trades: list[Trade] | None = None,
    info: SymbolInfo | None = None,
    quote_volume_24h: float = 50_000_000.0,
    trades_24h: int = 120_000,
) -> MarketSnapshot:
    bars = [] if bars is None else bars
    if ts_ms is None:
        ts_ms = (bars[-1].close_ms + 1) if bars else BASE_MS
    price = bars[-1].close if bars else 100.0
    if book is None:
        book = make_book(symbol=symbol, mid=price, ts_ms=ts_ms - 100)
    return MarketSnapshot(
        symbol=symbol,
        ts_ms=ts_ms,
        book=book,
        bars_1m=bars,
        recent_trades=[] if trades is None else trades,
        info=info or make_info(symbol),
        quote_volume_24h=quote_volume_24h,
        trades_24h=trades_24h,
    )


# --------------------------------------------------------------------------- #
# Synthetic Features / Signals — for regime, strategy and ranker tests, which
# take Features as an input and should not have to reverse-engineer a bar path.
# --------------------------------------------------------------------------- #


def make_features(**over) -> Features:
    """A healthy, boring, mid-range market.  Override only what a test cares
    about; every other field stays neutral."""
    kwargs: dict[str, object] = dict(
        symbol=SYMBOL,
        ts_ms=BASE_MS,
        price=100.0,
        mid=100.0,
        microprice=100.0,
        spread_bps=2.0,
        book_age_ms=200,
        depth_bid_usd=20_000.0,
        depth_ask_usd=20_000.0,
        imbalance=0.0,
        atr_bps=20.0,
        realized_vol_bps=12.0,
        vol_percentile=0.5,
        ret_1m_bps=0.0,
        ret_5m_bps=0.0,
        ret_15m_bps=0.0,
        ema_fast=100.0,
        ema_slow=100.0,
        ema_slope_bps=0.0,
        trend_strength=0.1,
        range_pos=0.5,
        bb_width_bps=40.0,
        compression=0.5,
        volume_ratio=1.0,
        trade_flow_imbalance=0.0,
        trade_flow_accel=0.0,
        vwap=100.0,
        vwap_dev_bps=0.0,
        quote_volume_24h=50_000_000.0,
        trades_24h=120_000,
        bars_available=200,
    )
    extras = {
        "spread_baseline_bps": 2.0,
        "compression_prev": 0.5,
        "bb_width_prev_bps": 40.0,
        "range_bps": 20.0,
        "new_bars": 1.0,
    }
    extras.update(over.pop("extras", {}) or {})
    kwargs.update(over)
    kwargs["extras"] = extras
    return Features(**kwargs)  # type: ignore[arg-type]


def make_regime(
    regime: Regime = Regime.RANGING,
    confidence: float = 0.6,
    reasons: list[str] | None = None,
) -> RegimeState:
    return RegimeState(regime=regime, confidence=confidence,
                       reasons=reasons or ["fixture"])


def make_signal(**over) -> Signal:
    kwargs: dict[str, object] = dict(
        strategy="momentum_scalp",
        symbol=SYMBOL,
        side=Side.LONG,
        score=0.6,
        target_bps=30.0,
        stop_bps=20.0,
        ts_ms=BASE_MS,
        horizon_seconds=300,
        reasons=["fixture"],
        features_used={},
    )
    kwargs.update(over)
    return Signal(**kwargs)  # type: ignore[arg-type]
