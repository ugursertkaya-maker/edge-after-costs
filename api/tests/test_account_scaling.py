"""Account-size parameters must follow the account, not a past one.

A dollar constant chosen for a $500 book silently becomes the binding
constraint on a $5,000 one, and the idle-capital attribution then blames a
risk limit for what is really an arithmetic leftover. Every derivation here
keeps its explicit override, so an operator can still pin any of them.
"""

from __future__ import annotations

import os

import pytest

from app.config import reload_settings


@pytest.fixture
def env(monkeypatch):
    for k in ("STARTING_EQUITY", "COMPARISON_LEDGERS", "MAX_POSITION_USD",
              "MAX_OPEN_POSITIONS", "TARGET_DEPLOYED_PCT", "CASH_RESERVE_PCT",
              "MIN_POSITION_USD", "MAX_POSITION_PCT_EQUITY_ALLOC"):
        monkeypatch.delenv(k, raising=False)

    def _set(**kw):
        for k, v in kw.items():
            monkeypatch.setenv(k, str(v))
        return reload_settings()

    yield _set
    reload_settings()


class TestBackwardCompatibility:
    def test_a_500_account_gets_exactly_the_old_constants(self, env):
        """$50 was 10% of $500. The derivation must reproduce it, or every
        number in Phases 1-6 stops comparing to the ones that follow."""
        s = env(STARTING_EQUITY=500)
        assert s.portfolio.max_position_usd == pytest.approx(50.0)
        assert s.target_active_capital_usd == pytest.approx(400.0)
        assert s.cash_reserve_usd == pytest.approx(100.0)


class TestPrimaryLedgerScaling:
    def test_the_requested_5000_profile(self, env):
        s = env(STARTING_EQUITY=5000)
        assert s.risk.starting_equity == pytest.approx(5_000.0)
        assert s.target_active_capital_usd == pytest.approx(4_000.0)
        assert s.cash_reserve_usd == pytest.approx(1_000.0)
        assert s.portfolio.target_deployed_pct == pytest.approx(80.0)

    def test_target_and_reserve_account_for_the_whole_book(self, env):
        s = env(STARTING_EQUITY=5000)
        assert s.target_active_capital_usd + s.cash_reserve_usd == pytest.approx(
            s.risk.starting_equity
        )

    def test_position_ceiling_scales_with_the_account(self, env):
        assert env(STARTING_EQUITY=5000).portfolio.max_position_usd == pytest.approx(500.0)

    def test_the_count_ceiling_does_not_become_the_binding_constraint(self, env):
        """Measured at the size the allocator would actually use, not the floor.

        The floor is the wrong yardstick: $4,000 in $10 positions needs 400
        slots, and 400 concurrent positions on a three-symbol venue is not a
        configuration anyone wants. What matters is that the COUNT is not what
        stops us -- at the derived position size it must have ample room, so
        idle capital is never attributed to a position-count limit when the
        real cause is depth or supply.
        """
        s = env(STARTING_EQUITY=5000)
        reachable = s.portfolio.max_open_positions * s.portfolio.max_position_usd
        assert reachable >= s.target_active_capital_usd

    def test_the_old_fixed_ceiling_would_have_been_binding(self, env):
        """Why this derivation exists: 20 slots at $50 tops out at $1,000."""
        s = env(STARTING_EQUITY=5000, MAX_OPEN_POSITIONS=20, MAX_POSITION_USD=50)
        reachable = s.portfolio.max_open_positions * s.portfolio.max_position_usd
        assert reachable < s.target_active_capital_usd

    def test_the_ceiling_is_still_bounded(self, env):
        """An emergency ceiling that grows without limit is not a ceiling."""
        assert env(STARTING_EQUITY=1_000_000).portfolio.max_open_positions <= 200


class TestOverridesStillWin:
    def test_an_explicit_position_cap_is_honoured(self, env):
        assert env(STARTING_EQUITY=5000,
                   MAX_POSITION_USD=75).portfolio.max_position_usd == pytest.approx(75.0)

    def test_an_explicit_count_cap_is_honoured(self, env):
        assert env(STARTING_EQUITY=5000, MAX_OPEN_POSITIONS=12).portfolio.max_open_positions == 12

    def test_the_sentinel_never_escapes_into_sizing(self, env):
        """A 0 reaching the allocator reads as 'no position may be opened'."""
        s = env(STARTING_EQUITY=5000, MAX_POSITION_USD=0, MAX_OPEN_POSITIONS=0)
        assert s.portfolio.max_position_usd > 0
        assert s.portfolio.max_open_positions > 0


class TestComparisonLedgers:
    def test_the_list_is_configurable(self, env):
        assert env(COMPARISON_LEDGERS="500,1000").risk.comparison_ledgers == (500.0, 1000.0)

    def test_a_malformed_list_degrades_instead_of_crashing(self, env):
        """These books are research instruments; losing them is not worth
        losing the trader."""
        assert env(COMPARISON_LEDGERS="nonsense").risk.comparison_ledgers == (1000.0, 5000.0)

    def test_the_primary_need_not_appear_among_the_comparisons(self, env):
        s = env(STARTING_EQUITY=5000, COMPARISON_LEDGERS="500,1000")
        assert s.risk.starting_equity not in s.risk.comparison_ledgers
