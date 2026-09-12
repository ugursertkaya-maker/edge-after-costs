"""V2 wiring: portfolio allocation, maker-first entry, the 25-minute checkpoint.

These test the *worker*, not the engines — the engines have their own suites.
What matters here is that the worker actually routes through them: that it no
longer opens one position per pass, that a missed maker order is not silently
crossed, and that the checkpoint's verdicts reach the ledger.
"""

from __future__ import annotations

import pytest

from app.config import reload_settings
from app.exits import CheckpointVerdict
from app.types import (
    ExitReason,
    Features,
    Opportunity,
    Regime,
    RegimeState,
    Side,
    Signal,
    SymbolInfo,
)

from tests.test_worker_loop import FakeFeed, make_bars, make_book, make_trades

SYMBOL = "BTCUSD"
INFO = SymbolInfo(
    symbol=SYMBOL, base_asset="BTC", quote_asset="USD", status="TRADING",
    tick_size=0.01, step_size=0.00001, min_qty=0.00001, min_notional=1.0,
)


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "PAPER")
    monkeypatch.setenv("MIN_NET_EDGE_BPS", "0.1")
    monkeypatch.setenv("UNIVERSE_MAX_SYMBOLS", "1")
    reload_settings()

    from app.runtime import RuntimeStore
    from app.universe import UniverseEntry
    from app.worker.main import TradingWorker

    store = RuntimeStore(client=None)
    store._client = None
    w = TradingWorker(store=store)
    monkeypatch.setattr(w, "_persist", lambda fn: None)

    mid = 60_000.0
    w.feed = FakeFeed(
        make_bars(180, start=58_000.0, drift_bps_per_bar=6.0),
        make_trades(120, price=mid, ts_ms=1_000_000),
    )
    w.symbol_info = {SYMBOL: INFO}
    book = make_book(mid)
    w.books.apply_snapshot(
        SYMBOL, 1,
        [(lv.price, lv.qty) for lv in book.bids],
        [(lv.price, lv.qty) for lv in book.asks],
        1_000_000,
    )
    w.universe = [
        UniverseEntry(
            symbol=SYMBOL, info=INFO, quote_volume_24h=9e8, trades_24h=400_000,
            spread_bps=2.0, volatility_bps=120.0, depth_usd=50_000.0,
            score=1.0, tier="hot",
        )
    ]
    yield w
    reload_settings()


def synthetic_opportunity(worker, ts: int, *, symbol: str = SYMBOL,
                          net_edge_bps: float = 40.0) -> Opportunity:
    """A hand-built, comfortably profitable candidate.

    These tests are about worker *wiring*, not signal generation: relying on a
    strategy to fire on synthetic bars would make them silently skip whenever
    the strategy layer changes, which is exactly how wiring regressions escape.
    """
    book = worker.books.get(symbol)
    feats = worker.features.compute(
        worker._snapshot(worker.universe[0], ts)  # real features, real book
    )
    signal = Signal(
        strategy="momentum_scalp", symbol=symbol, side=Side.LONG, score=0.85,
        target_bps=120.0, stop_bps=60.0, ts_ms=ts, horizon_seconds=900,
    )
    return Opportunity(
        signal=signal,
        features=feats,
        regime=RegimeState(regime=Regime.TRENDING, confidence=0.9),
        gross_edge_bps=net_edge_bps + 6.0,
        fee_bps=2.0, spread_cost_bps=2.0, slippage_bps=2.0,
        net_edge_bps=net_edge_bps, rank_score=1.0, expected_hold_seconds=900,
    )


# --------------------------------------------------------------------------- #
# The engines are actually wired in
# --------------------------------------------------------------------------- #


def test_worker_owns_the_v2_engines(worker):
    from app.portfolio import PortfolioEngine
    from app.profit import NetTargetEngine

    assert isinstance(worker.portfolio, PortfolioEngine)
    assert isinstance(worker.profit, NetTargetEngine)
    assert worker.risk.portfolio is worker.portfolio, "risk must share the engine"


def test_risk_state_carries_portfolio_state(worker):
    state = worker._risk_state(worker.ledgers["base"], 1_000_000)
    assert state.portfolio is not None
    assert state.portfolio.equity == pytest.approx(500.0)
    assert state.portfolio.reserve_required_usd > 0, "a reserve must be held back"
    assert state.portfolio.available_usd < state.portfolio.cash


def test_position_count_is_no_longer_the_binding_constraint(worker):
    """The headline V2 change: risk and cash decide, not an arbitrary 1-3 cap."""
    assert worker.s.portfolio.max_open_positions > 3
    assert worker.s.portfolio.total_open_risk_pct > 0


# --------------------------------------------------------------------------- #
# Entry path
# --------------------------------------------------------------------------- #


def test_entry_carries_net_targets_and_a_break_even_price(worker):
    ts = 1_000_000
    worker.evaluate_once(ts)
    assert worker.try_enter([synthetic_opportunity(worker, ts)], ts) >= 1

    pos = next(iter(worker.ledgers["base"].open_positions.values()))
    assert pos.targets is not None
    assert pos.break_even_price > 0
    assert pos.checkpoint_ms == pos.entry_ms + worker.s.horizon.checkpoint_seconds * 1000
    assert pos.time_stop_ms == pos.entry_ms + worker.s.horizon.time_stop_seconds * 1000
    # The net take-profit must sit beyond break-even, or it is not a profit.
    if pos.side is Side.LONG:
        assert pos.take_profit > pos.break_even_price > 0


def test_a_missed_maker_order_is_never_silently_crossed(worker, monkeypatch):
    """A post-only miss must re-qualify on taker economics, not auto-market."""
    calls: list[str] = []

    real_execute = worker.sim.execute

    def spy(req, book, info, **kw):
        calls.append("post_only" if kw.get("post_only") else req.order_type.value)
        if kw.get("post_only"):
            return None  # the maker order never fills
        return real_execute(req, book, info, **{k: v for k, v in kw.items() if k != "post_only"})

    monkeypatch.setattr(worker.sim, "execute", spy)
    monkeypatch.setattr(worker, "_taker_still_qualifies", lambda *a, **k: False)

    ts = 1_000_000
    worker.evaluate_once(ts)
    worker.try_enter([synthetic_opportunity(worker, ts)], ts)

    assert not worker.ledgers["base"].open_positions, (
        "a maker miss that fails taker economics must not become a market order"
    )


def test_maker_price_never_crosses_the_spread(worker):
    book = worker.books.get(SYMBOL)
    price = worker._maker_price(Side.LONG, book, INFO)
    assert 0 < price <= book.best_bid, "a post-only buy must rest at or below the bid"
    ask_side = worker._maker_price(Side.SHORT, book, INFO)
    assert ask_side >= book.best_ask, "a post-only sell must rest at or above the ask"


# --------------------------------------------------------------------------- #
# The 25-minute checkpoint
# --------------------------------------------------------------------------- #


def _force_open(worker, ts):
    worker.evaluate_once(ts)
    assert worker.try_enter([synthetic_opportunity(worker, ts)], ts) >= 1
    return next(iter(worker.ledgers["base"].open_positions.values()))


def test_checkpoint_is_not_due_before_its_time(worker):
    ts = 1_000_000
    pos = _force_open(worker, ts)
    assert not worker._checkpoint_due(pos, ts + 60_000)
    assert worker._checkpoint_due(pos, pos.checkpoint_ms + 1)


def test_checkpoint_keep_leaves_the_position_open(worker, monkeypatch):
    ts = 1_000_000
    pos = _force_open(worker, ts)
    monkeypatch.setattr(
        worker.exits, "checkpoint", lambda *a, **k: CheckpointVerdict.KEEP
    )
    assert worker._run_checkpoint(pos, worker.books.get(SYMBOL), None, pos.checkpoint_ms) is None
    assert pos.checkpoint_done is True


@pytest.mark.parametrize(
    "verdict,expected",
    [
        (CheckpointVerdict.TAKE_PROFIT, ExitReason.TAKE_PROFIT),
        (CheckpointVerdict.EXIT_WEAK, ExitReason.EXIT_WEAK),
        (CheckpointVerdict.ROTATE_CAPITAL, ExitReason.ROTATE_CAPITAL),
    ],
)
def test_checkpoint_verdicts_map_to_exit_reasons(worker, monkeypatch, verdict, expected):
    ts = 1_000_000
    pos = _force_open(worker, ts)
    monkeypatch.setattr(worker.exits, "checkpoint", lambda *a, **k: verdict)
    got = worker._run_checkpoint(pos, worker.books.get(SYMBOL), None, pos.checkpoint_ms)
    assert got is expected


def test_a_stop_beats_the_checkpoint(worker, monkeypatch):
    """A stop hit at minute two is honoured at minute two, never deferred."""
    ts = 1_000_000
    pos = _force_open(worker, ts)
    crashed = make_book(pos.stop_loss * 0.98)
    worker.books.apply_snapshot(
        SYMBOL, 2,
        [(lv.price, lv.qty) for lv in crashed.bids],
        [(lv.price, lv.qty) for lv in crashed.asks],
        ts + 120_000,
    )
    monkeypatch.setattr(
        worker.exits, "checkpoint", lambda *a, **k: CheckpointVerdict.KEEP
    )
    # Every ledger holds the same position, so all of them stop out together.
    assert worker.monitor_once(ts + 120_000) == len(worker.ledgers)
    assert worker.ledgers["base"].closed_trades[-1].exit_reason is ExitReason.STOP_LOSS


def test_best_alternative_edge_excludes_the_position_we_hold(worker):
    ts = 1_000_000
    worker.evaluate_once(ts)
    assert worker._best_alternative_edge(SYMBOL) == 0.0 or all(
        o.symbol != SYMBOL for o in worker._ranked
    )


# --------------------------------------------------------------------------- #
# Dashboard payload
# --------------------------------------------------------------------------- #


def test_payload_separates_gross_costs_and_net(worker):
    worker.evaluate_once(1_000_000)
    m = worker.dashboard_payload("base")["metrics"]
    for key in ("profit_before_costs", "total_costs", "real_net_profit"):
        assert key in m, f"the dashboard must show {key} as its own value"


def test_payload_has_a_portfolio_block(worker):
    worker.evaluate_once(1_000_000)
    pf = worker.dashboard_payload("base")["portfolio"]
    for key in (
        "open_positions", "capital_deployed", "available_cash", "cash_reserve",
        "open_risk_usd", "open_risk_pct", "correlated_exposure_pct",
        "avg_position_usd", "median_position_usd", "avg_holding_seconds",
        "positions_under_25m", "positions_25_60m", "positions_over_60m",
        "capital_turnover_per_day", "net_profit_per_dollar_deployed",
        "allocation_mode", "net_tp_profile",
    ):
        assert key in pf, f"portfolio block missing {key}"
    assert pf["cash_reserve"] > 0, "a reserve must always be held back"


def test_position_rows_carry_the_full_cost_breakdown(worker):
    ts = 1_000_000
    worker.evaluate_once(ts)
    assert worker.try_enter([synthetic_opportunity(worker, ts)], ts) >= 1
    row = worker.dashboard_payload("base")["positions"][0]
    for key in (
        "gross_pnl_usd", "entry_fee_usd", "expected_exit_fee_usd",
        "spread_cost_usd", "slippage_usd", "total_cost_usd", "net_pnl_usd",
        "maker_or_taker", "break_even_price", "net_tp_price",
        "account_risk_usd", "age_seconds", "checkpoint_status", "time_stop_ms",
    ):
        assert key in row, f"position row missing {key}"
    assert row["maker_or_taker"] in ("MAKER", "TAKER")
    assert row["checkpoint_status"] in ("PENDING", "DUE", "KEEP", "TRAIL")
    # Net is gross minus every cost — never the gross number relabelled.
    assert row["net_pnl_usd"] == pytest.approx(
        row["gross_pnl_usd"] - row["total_cost_usd"], abs=1e-6
    )


# --------------------------------------------------------------------------- #
# Phase 3 — research payloads
# --------------------------------------------------------------------------- #


def test_payload_has_research_keys_even_with_no_research_yet(worker):
    worker.evaluate_once(1_000_000)
    payload = worker.dashboard_payload("base")
    assert payload["tournament"] is None
    assert payload["finalists"] == []
    assert payload["champion"] is None, "no champion until one is promoted"


def test_a_promoted_champion_appears_with_its_oos_claim(worker, tmp_path, monkeypatch):
    """The out-of-sample claim and the live result sit side by side on purpose:
    they will disagree, and the gap is the information."""
    from dataclasses import replace as dc_replace

    from app.research.champion import Champion, ChampionStore

    store = ChampionStore(tmp_path)
    store.promote(
        Champion(
            key="breakout|BTCUSD|h1800", family="breakout", symbol="BTCUSD",
            horizon_seconds=1_800, oos_trades=48, oos_profit_factor=1.31,
            oos_expectancy=0.021, gate_reasons=("PF 1.31 >= 1.20",),
        ),
        current_version="sim-1.0.0",
    )
    worker.champions = store
    worker.champion = store.load()

    block = worker.dashboard_payload("base")["champion"]
    assert block is not None
    assert block["family"] == "breakout"
    assert block["version"] == "sim-1.0.1"
    assert block["oos_profit_factor"] == pytest.approx(1.31)
    assert "live_profit_factor" in block and "expectancy" in block
    assert block["gate_reasons"] == ["PF 1.31 >= 1.20"]


def test_tournament_output_is_read_from_disk(worker, tmp_path, monkeypatch):
    import json as _json
    from dataclasses import replace as dc_replace

    from app.config import ResearchConfig

    (tmp_path / "tournament.json").write_text(
        _json.dumps({"candidates_evaluated": 1234, "arms": [{"key": "a", "oos_status": "FAILED"}]})
    )
    (tmp_path / "finalists.json").write_text(_json.dumps({"finalists": []}))
    worker.s = dc_replace(worker.s, research=ResearchConfig(directory=str(tmp_path)))

    payload = worker.dashboard_payload("base")
    assert payload["tournament"]["candidates_evaluated"] == 1234
    assert payload["finalists"] == []


def test_corrupt_research_files_do_not_break_the_dashboard(worker, tmp_path):
    from dataclasses import replace as dc_replace

    from app.config import ResearchConfig

    (tmp_path / "tournament.json").write_text("{not json")
    worker.s = dc_replace(worker.s, research=ResearchConfig(directory=str(tmp_path)))

    payload = worker.dashboard_payload("base")
    assert payload["tournament"] is None
    assert payload["finalists"] == []


def test_health_reports_the_tape_recorder(worker):
    worker.evaluate_once(1_000_000)
    h = worker.dashboard_payload("base")["health"]
    assert "recorder" in h and "tape" in h
    assert "enabled" in h["recorder"]


# --------------------------------------------------------------------------- #
# Phase 5 — capital utilisation
# --------------------------------------------------------------------------- #


def test_payload_carries_capital_and_missed_keys(worker):
    worker.evaluate_once(1_000_000)
    payload = worker.dashboard_payload("base")
    assert "capital" in payload and "missed" in payload


def test_every_idle_dollar_is_attributed_to_a_reason(worker):
    """A residual bucket would defeat the whole point: if $350 is idle the
    operator must be told exactly why, with no rounding to hide in."""
    worker.evaluate_once(1_000_000)
    cap = worker.dashboard_payload("base")["capital"]
    assert cap is not None
    assert set(cap["idle_by_reason"]) == {
        "NO_QUALIFIED_SIGNAL", "CORRELATION_LIMIT", "RISK_LIMIT",
        "LIQUIDITY_LIMIT", "EXECUTION_COST", "CASH_RESERVE",
    }
    assert sum(cap["idle_by_reason"].values()) == pytest.approx(
        cap["idle_usd"], abs=0.02
    )


def test_capital_block_reports_the_band_it_is_aiming_at(worker):
    worker.evaluate_once(1_000_000)
    cap = worker.dashboard_payload("base")["capital"]
    assert 60.0 <= cap["target_pct"] <= 85.0
    assert cap["deployed_pct"] >= 0.0
    assert cap["equity"] == pytest.approx(500.0)


def test_an_idle_account_attributes_to_no_qualified_signal(worker):
    """The Phase 3 shape: nothing ranked, so nothing was refused for cost or
    correlation — the money had nothing to buy."""
    worker._ranked = []
    worker.stats.rejects = {}
    cap = worker.dashboard_payload("base")["capital"]
    by = cap["idle_by_reason"]
    assert by["NO_QUALIFIED_SIGNAL"] > 0
    assert by["NO_QUALIFIED_SIGNAL"] >= max(
        by["EXECUTION_COST"], by["CORRELATION_LIMIT"], by["RISK_LIMIT"]
    )


def test_cold_tier_symbols_are_not_subscribed_to_the_feed(worker, monkeypatch):
    """COLD is a watchlist. Subscribing it is exactly the CPU the tiering
    exists to avoid on a 2-vCPU host."""
    from app.universe import UniverseEntry

    seen = {}

    class Feed(FakeFeed):
        def set_universe(self, hot, warm):
            seen["hot"], seen["warm"] = list(hot), list(warm)

    worker.feed = Feed([], [])
    entries = [
        UniverseEntry(symbol="BTCUSD", info=INFO, quote_volume_24h=1e6,
                      trades_24h=4000, spread_bps=1.0, volatility_bps=400.0,
                      depth_usd=50_000.0, score=1.0, tier="hot"),
        UniverseEntry(symbol="ETHUSD", info=INFO, quote_volume_24h=1e6,
                      trades_24h=2000, spread_bps=1.0, volatility_bps=400.0,
                      depth_usd=40_000.0, score=0.9, tier="warm"),
        UniverseEntry(symbol="ZECUSDT", info=INFO, quote_volume_24h=2e6,
                      trades_24h=3000, spread_bps=25.0, volatility_bps=900.0,
                      depth_usd=10_000.0, score=0.5, tier="cold"),
    ]
    monkeypatch.setattr(
        worker.universe_selector, "refresh",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    import asyncio

    async def fake_refresh(rest, *a, **k):
        return entries

    monkeypatch.setattr(worker.universe_selector, "refresh", fake_refresh)
    monkeypatch.setattr(worker, "_seed_bars", lambda syms: asyncio.sleep(0))
    asyncio.run(worker.refresh_universe())

    assert "ZECUSDT" not in seen.get("hot", []) + seen.get("warm", [])
    assert "BTCUSD" in seen["hot"]
