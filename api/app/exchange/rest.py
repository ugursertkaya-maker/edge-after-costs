"""Binance.US public REST client.

Only the six public market-data endpoints the bot needs are implemented.  There
is no signing code, no API-key header and no order-placement path anywhere in
this module — V1 is paper-only and must stay structurally incapable of trading.

Two behaviours matter more than the endpoint list:

* **Politeness.**  Binance.US answers 429 (rate limit) and 418 (banned for
  ignoring 429s) with a ``Retry-After`` header.  Ignoring it escalates to an IP
  ban, so we honour it exactly and otherwise back off exponentially.
* **Bounded concurrency.**  The worker shares 1 vCPU with the rest of the VPS;
  a fan-out of 30 simultaneous depth calls would both burn the weight budget
  and stall the event loop.  A semaphore caps in-flight requests.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ..config import Settings, get_settings
from ..types import Bar, BookLevel, OrderBook, SymbolInfo, Trade, now_ms

#: Requests that may be retried; everything else is a client error we deserve.
RETRY_STATUS = frozenset({429, 418})

DEFAULT_TIMEOUT_S = 10.0
DEFAULT_MAX_CONCURRENCY = 4
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BACKOFF_BASE_S = 0.5
DEFAULT_BACKOFF_CAP_S = 8.0

#: Binance caps a single klines/aggTrades page at 1000 rows.
MAX_PAGE = 1000
#: Guard against a pathological paging loop eating the worker.
MAX_PAGES = 200


async def _sleep(seconds: float) -> None:
    """Indirection so tests can replace the wait without touching asyncio."""
    await asyncio.sleep(seconds)


class BinanceUSRest:
    """Async wrapper over the public Binance.US REST API."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        client: "httpx.AsyncClient | None" = None,
        settings: Settings | None = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self.settings = settings or get_settings()
        self.base_url = (base_url or self.settings.exchange.rest_base).rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(DEFAULT_TIMEOUT_S),
            headers={"Accept": "application/json"},
        )
        self._sem = asyncio.Semaphore(max(1, int(max_concurrency)))
        self._max_attempts = max(1, int(max_attempts))
        #: Cheap observability for the /health route and tests.
        self.requests = 0
        self.retries = 0

    # ------------------------------------------------------------------ #
    # transport
    # ------------------------------------------------------------------ #

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    @staticmethod
    def _retry_after_s(response: httpx.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            # Binance always sends delta-seconds, never an HTTP-date.
            return max(0.0, float(str(raw).strip()))
        except (TypeError, ValueError):
            return None

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET with bounded concurrency, retries and ``Retry-After`` support."""
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        last_error: Exception | None = None

        async with self._sem:
            for attempt in range(self._max_attempts):
                last = attempt == self._max_attempts - 1
                try:
                    self.requests += 1
                    response = await self._client.get(
                        self._url(path), params=clean, timeout=DEFAULT_TIMEOUT_S
                    )
                except (httpx.TransportError, httpx.InvalidURL) as exc:
                    last_error = exc
                    if last:
                        raise
                    self.retries += 1
                    await _sleep(self._backoff(attempt))
                    continue

                status = response.status_code
                if status in RETRY_STATUS or status >= 500:
                    if last:
                        response.raise_for_status()
                    self.retries += 1
                    wait = self._retry_after_s(response)
                    await _sleep(self._backoff(attempt) if wait is None else wait)
                    continue

                response.raise_for_status()
                return response.json()

        # Unreachable: the loop either returns or raises.
        raise last_error or RuntimeError(f"GET {path} exhausted retries")

    def _backoff(self, attempt: int) -> float:
        return min(DEFAULT_BACKOFF_CAP_S, DEFAULT_BACKOFF_BASE_S * (2**attempt))

    async def close(self) -> None:
        """Close the underlying client, but only if we created it."""
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ #
    # endpoints
    # ------------------------------------------------------------------ #

    async def ping(self) -> bool:
        try:
            await self._get("/api/v3/ping")
        except Exception:
            return False
        return True

    async def server_time_ms(self) -> int:
        data = await self._get("/api/v3/time")
        return int(data["serverTime"])

    async def exchange_info(self) -> dict[str, SymbolInfo]:
        """Static metadata keyed by symbol, with the three filters we need."""
        data = await self._get("/api/v3/exchangeInfo")
        out: dict[str, SymbolInfo] = {}
        for raw in data.get("symbols", []) or []:
            info = _parse_symbol_info(raw)
            if info is not None:
                out[info.symbol] = info
        return out

    async def ticker_24h(self) -> list[dict]:
        """Raw 24h rolling stats for every symbol (the universe layer parses it)."""
        data = await self._get("/api/v3/ticker/24hr")
        if isinstance(data, dict):
            return [data]
        return [row for row in (data or []) if isinstance(row, dict)]

    async def depth(self, symbol: str, limit: int = 100) -> OrderBook:
        data = await self._get(
            "/api/v3/depth", {"symbol": symbol.upper(), "limit": int(limit)}
        )
        book = OrderBook(symbol=symbol.upper())
        book.bids = _levels(data.get("bids"))
        book.asks = _levels(data.get("asks"))
        book.last_update_id = int(data.get("lastUpdateId", 0) or 0)
        book.last_update_ms = now_ms()
        return book

    async def klines(
        self,
        symbol: str,
        interval: str = "1m",
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 1000,
    ) -> list[Bar]:
        """Bars for ``symbol``, paging transparently over the 1000-row cap.

        With neither bound a single most-recent page is returned.  With a
        ``start_ms`` we keep walking forward from the last bar we received until
        the exchange hands back a short page or we pass ``end_ms``.
        """
        sym = symbol.upper()
        page_limit = min(MAX_PAGE, max(1, int(limit)))
        bounded = start_ms is not None or end_ms is not None
        cursor = start_ms
        seen: set[int] = set()
        bars: list[Bar] = []

        for _ in range(MAX_PAGES):
            rows = await self._get(
                "/api/v3/klines",
                {
                    "symbol": sym,
                    "interval": interval,
                    "limit": page_limit,
                    "startTime": cursor,
                    "endTime": end_ms,
                },
            )
            rows = rows or []
            for row in rows:
                bar = _parse_kline(sym, row)
                if bar is None or bar.open_ms in seen:
                    continue
                if end_ms is not None and bar.open_ms > end_ms:
                    continue
                seen.add(bar.open_ms)
                bars.append(bar)

            if not bounded or len(rows) < page_limit or not bars:
                break
            next_cursor = bars[-1].close_ms + 1
            if end_ms is not None and next_cursor > end_ms:
                break
            if cursor is not None and next_cursor <= cursor:
                break  # no forward progress; stop rather than spin
            cursor = next_cursor

        bars.sort(key=lambda b: b.open_ms)
        return bars

    async def agg_trades(
        self,
        symbol: str,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 1000,
    ) -> list[Trade]:
        sym = symbol.upper()
        rows = await self._get(
            "/api/v3/aggTrades",
            {
                "symbol": sym,
                "limit": min(MAX_PAGE, max(1, int(limit))),
                "startTime": start_ms,
                "endTime": end_ms,
            },
        )
        out: list[Trade] = []
        for row in rows or []:
            trade = _parse_agg_trade(sym, row)
            if trade is not None:
                out.append(trade)
        out.sort(key=lambda t: t.ts_ms)
        return out


# --------------------------------------------------------------------------- #
# parsing helpers (module level so tests can exercise them directly)
# --------------------------------------------------------------------------- #


def _levels(raw: Any) -> list[BookLevel]:
    out: list[BookLevel] = []
    for row in raw or []:
        try:
            price, qty = float(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if qty > 0 and price > 0:
            out.append(BookLevel(price, qty))
    return out


def _parse_symbol_info(raw: dict) -> SymbolInfo | None:
    """Flatten one ``exchangeInfo`` entry, folding its filters into scalars.

    Binance renamed ``MIN_NOTIONAL``/``minNotional`` to ``NOTIONAL``/``notional``
    part-way through 2023 and Binance.US still serves a mix, so both spellings
    are accepted.
    """
    symbol = raw.get("symbol")
    if not symbol:
        return None
    filters = {
        f.get("filterType"): f
        for f in raw.get("filters", []) or []
        if isinstance(f, dict)
    }
    price_f = filters.get("PRICE_FILTER", {})
    lot_f = filters.get("LOT_SIZE", {})
    notional_f = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}

    return SymbolInfo(
        symbol=str(symbol).upper(),
        base_asset=str(raw.get("baseAsset", "")).upper(),
        quote_asset=str(raw.get("quoteAsset", "")).upper(),
        status=str(raw.get("status", "")).upper(),
        tick_size=_f(price_f.get("tickSize")),
        step_size=_f(lot_f.get("stepSize")),
        min_qty=_f(lot_f.get("minQty")),
        min_notional=_f(notional_f.get("minNotional", notional_f.get("notional"))),
    )


def _parse_kline(symbol: str, row: Any) -> Bar | None:
    """Map Binance's 12-element kline array onto :class:`Bar`."""
    try:
        return Bar(
            symbol=symbol,
            open_ms=int(row[0]),
            close_ms=int(row[6]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            quote_volume=float(row[7]),
            trades=int(row[8]),
            taker_buy_quote=float(row[10]) if len(row) > 10 else 0.0,
        )
    except (TypeError, ValueError, IndexError, KeyError):
        return None


def _parse_agg_trade(symbol: str, row: Any) -> Trade | None:
    try:
        return Trade(
            symbol=symbol,
            price=float(row["p"]),
            qty=float(row["q"]),
            ts_ms=int(row["T"]),
            buyer_is_maker=bool(row["m"]),
        )
    except (TypeError, ValueError, KeyError, IndexError):
        return None


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
