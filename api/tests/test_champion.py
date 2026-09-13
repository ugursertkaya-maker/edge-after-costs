"""Promotion must be auditable, versioned, and impossible to do by accident."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.research.champion import (
    Champion,
    ChampionStore,
    champion_from_gate_report,
    next_version,
)


def make_champion(**over) -> Champion:
    base = dict(
        key="momentum_continuation|BTCUSD|h1800",
        family="momentum_continuation",
        params={"lookback": 5},
        symbol="BTCUSD",
        horizon_seconds=1_800,
        oos_trades=42,
        oos_profit_factor=1.35,
        oos_expectancy=0.04,
    )
    base.update(over)
    return Champion(**base)


# --------------------------------------------------------------------------- #
# Versioning — the whole point of promotion being heavyweight
# --------------------------------------------------------------------------- #


def test_version_bumps_on_promotion():
    assert next_version("sim-1.0.0") == "sim-1.0.1"
    assert next_version("sim-2.7.9") == "sim-2.7.10"


def test_an_unparseable_version_never_collides():
    a = next_version("garbage")
    assert a.startswith("sim-")
    assert a != "garbage"


def test_two_promotions_never_share_a_version(tmp_path):
    store = ChampionStore(tmp_path)
    first = store.promote(make_champion(), current_version="sim-1.0.0")
    second = store.promote(make_champion(family="breakout"))
    assert first.simulator_version != second.simulator_version
    assert second.simulator_version == "sim-1.0.2"


def test_promotion_stamps_the_time_and_survives_a_reload(tmp_path):
    store = ChampionStore(tmp_path)
    stored = store.promote(make_champion(), current_version="sim-1.0.0", now_ms=1_700_000_000_000)
    assert stored.promoted_ms == 1_700_000_000_000

    reloaded = ChampionStore(tmp_path).load()
    assert reloaded is not None
    assert reloaded.simulator_version == stored.simulator_version
    assert reloaded.family == "momentum_continuation"
    assert reloaded.params == {"lookback": 5}
    assert reloaded.oos_profit_factor == pytest.approx(1.35)


# --------------------------------------------------------------------------- #
# Storage behaviour
# --------------------------------------------------------------------------- #


def test_no_champion_is_a_clean_none(tmp_path):
    assert ChampionStore(tmp_path).load() is None


def test_a_corrupt_record_is_ignored_not_raised(tmp_path):
    (tmp_path / "champion.json").write_text("{not json")
    assert ChampionStore(tmp_path).load() is None


def test_an_unknown_field_does_not_break_loading(tmp_path):
    """A record written by a future version must not brick the current one."""
    store = ChampionStore(tmp_path)
    store.promote(make_champion())
    import json

    raw = json.loads((tmp_path / "champion.json").read_text())
    raw["a_field_from_the_future"] = 1
    (tmp_path / "champion.json").write_text(json.dumps(raw))

    assert ChampionStore(tmp_path).load() is not None


def test_clear_removes_the_champion(tmp_path):
    store = ChampionStore(tmp_path)
    store.promote(make_champion())
    store.clear()
    assert store.load() is None


def test_redis_is_a_cache_not_a_requirement(tmp_path):
    class BrokenRedis:
        def get(self, _k):
            raise RuntimeError("redis down")

        def set(self, *_a):
            raise RuntimeError("redis down")

        def delete(self, _k):
            raise RuntimeError("redis down")

    store = ChampionStore(tmp_path, redis_client=BrokenRedis())
    stored = store.promote(make_champion())
    assert store.load().simulator_version == stored.simulator_version


def test_redis_serves_when_disk_is_empty(tmp_path):
    import json

    class MemoryRedis:
        def __init__(self):
            self.data = {}

        def get(self, k):
            return self.data.get(k)

        def set(self, k, v):
            self.data[k] = v

        def delete(self, k):
            self.data.pop(k, None)

    redis = MemoryRedis()
    ChampionStore(tmp_path, redis_client=redis).promote(make_champion())
    (tmp_path / "champion.json").unlink()

    reloaded = ChampionStore(tmp_path, redis_client=redis).load()
    assert reloaded is not None and reloaded.family == "momentum_continuation"
    assert json.loads(redis.data["edge_after_costs:champion"])["family"] == "momentum_continuation"


# --------------------------------------------------------------------------- #
# A failing strategy cannot be promoted by accident
# --------------------------------------------------------------------------- #


@dataclass
class FakeCandidate:
    family: str = "breakout"
    symbol: str = "ETHUSD"
    params: dict | None = None
    horizon_seconds: int = 900
    exit_profile: str = "atr"
    stop_profile: str = "structure"
    regime = None

    def key(self) -> str:
        return "breakout|ETHUSD|h900"


@dataclass
class FakeStats:
    trades: int = 55
    profit_factor: float = 1.4
    expectancy: float = 0.03
    net_pnl: float = 1.65
    max_drawdown: float = 3.2


@dataclass
class FakeArm:
    candidate: FakeCandidate
    out_of_sample: FakeStats


@dataclass
class FakeReport:
    passed: bool
    reasons: list


def test_a_failed_gate_report_cannot_become_a_champion():
    arm = FakeArm(FakeCandidate(params={}), FakeStats())
    assert champion_from_gate_report(FakeReport(False, ["PF 0.4 < 1.20"]), arm=arm) is None


def test_a_passing_report_carries_its_evidence_across():
    arm = FakeArm(FakeCandidate(params={"n": 3}), FakeStats())
    report = FakeReport(True, ["expectancy > 0", "PF 1.40 >= 1.20", "6/8 windows positive"])

    champ = champion_from_gate_report(report, arm=arm, source_run="tournament-2026-09-11")
    assert champ is not None
    assert champ.family == "breakout"
    assert champ.symbol == "ETHUSD"
    assert champ.params == {"n": 3}
    assert champ.oos_trades == 55
    assert champ.oos_profit_factor == pytest.approx(1.4)
    assert champ.source_run == "tournament-2026-09-11"
    assert "6/8 windows positive" in champ.gate_reasons


def test_describe_is_readable():
    text = make_champion().describe()
    assert "momentum_continuation" in text and "BTCUSD" in text and "1.35" in text
