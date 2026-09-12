"""Per-symbol funnel: where every eligible Binance.US market dies.

Phase 6 opens with an accusation -- the trader is "BTC-only" -- and the
accusation is half wrong in a way that matters. BTC is 41.7% of live trades,
not 70%+. What is true is that only three symbols ever trade, out of 253
tradable pairs, and the reason for that is upstream of any ranking decision.

This module answers the question with counts instead of argument. Every symbol
walks a fixed ladder of stages; a symbol that dies records the stage and the
reason, so "why is XRP never traded?" has one answer and it is a string, not a
theory.

The ladder is monotone by construction: a symbol cannot reach ``candidate``
without passing ``features``. :meth:`SymbolFunnel.check` enforces that, because
a funnel whose counts go back up is measuring its own instrumentation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["STAGES", "SymbolFunnel", "FunnelReport"]

#: The ladder, in order. A symbol reaching stage *i* has reached every stage
#: before it. Names are frozen in ``docs/PHASE6_SPEC.md``.
STAGES: tuple[str, ...] = (
    "discovered",
    "subscribed",
    "data_healthy",
    "features",
    "strategy_evaluated",
    "model_evaluated",
    "candidate",
    "cost_gate",
    "liquidity_gate",
    "risk_gate",
    "correlation_gate",
    "ranked",
    "selected",
    "traded",
)

_STAGE_INDEX = {name: i for i, name in enumerate(STAGES)}


@dataclass
class SymbolFunnel:
    """One symbol's journey from "the venue lists it" to "we traded it"."""

    symbol: str
    stages: dict[str, int] = field(default_factory=lambda: {s: 0 for s in STAGES})
    drop_reasons: dict[str, dict[str, int]] = field(default_factory=dict)

    def reached(self, stage: str, n: int = 1) -> None:
        if stage not in _STAGE_INDEX:
            raise KeyError(f"unknown stage {stage!r}")
        self.stages[stage] = self.stages.get(stage, 0) + n

    def dropped(self, stage: str, reason: str, n: int = 1) -> None:
        """Record that ``n`` attempts died *at* ``stage`` for ``reason``.

        The stage named is the one the symbol failed to clear, so a symbol
        rejected by the cost gate is ``dropped("cost_gate", ...)`` and its
        ``cost_gate`` count stays where it is.
        """
        if stage not in _STAGE_INDEX:
            raise KeyError(f"unknown stage {stage!r}")
        bucket = self.drop_reasons.setdefault(stage, {})
        bucket[reason] = bucket.get(reason, 0) + n

    @property
    def deepest_stage(self) -> str:
        """The last stage this symbol ever reached; ``STAGES[0]`` if none."""
        deepest = STAGES[0]
        for stage in STAGES:
            if self.stages.get(stage, 0) > 0:
                deepest = stage
        return deepest

    def bottleneck(self) -> tuple[str, str]:
        """The stage that stopped this symbol, and the commonest reason there.

        The bottleneck is the first stage with a zero count *after* a non-zero
        one -- i.e. the wall, not the last place anything happened.
        """
        wall = None
        for stage in STAGES:
            if self.stages.get(stage, 0) > 0:
                wall = None
            elif wall is None:
                wall = stage
        if wall is None:
            return ("traded", "")
        reasons = self.drop_reasons.get(wall) or {}
        # A stage can be blocked without recording a reason there: the symbol
        # never arrived to be judged. Borrow the nearest recorded reason rather
        # than invent one -- nearest in either direction, because a reason can
        # legitimately be filed at a later stage than the one it blocks. A pair
        # whose flow cannot absorb the minimum order is recorded at
        # ``candidate`` (that is the stage it can never clear) while the wall
        # sits earlier, at the first stage it never reached.
        if not reasons:
            idx = _STAGE_INDEX[wall]
            by_distance = sorted(
                (s for s in self.drop_reasons if self.drop_reasons[s]),
                key=lambda s: (abs(_STAGE_INDEX[s] - idx), _STAGE_INDEX[s]),
            )
            if by_distance:
                reasons = self.drop_reasons[by_distance[0]]
        top = max(reasons.items(), key=lambda kv: kv[1])[0] if reasons else "never reached"
        return (wall, top)

    def check(self) -> list[str]:
        """Monotonicity violations. Empty means the instrumentation is sane."""
        problems: list[str] = []
        prev_name, prev = STAGES[0], self.stages.get(STAGES[0], 0)
        for stage in STAGES[1:]:
            cur = self.stages.get(stage, 0)
            if cur > prev:
                problems.append(
                    f"{self.symbol}: {stage}={cur} exceeds {prev_name}={prev}"
                )
            prev_name, prev = stage, cur
        return problems

    def as_dict(self) -> dict:
        stage, reason = self.bottleneck()
        return {
            "symbol": self.symbol,
            "stages": {s: self.stages.get(s, 0) for s in STAGES},
            "drop_reasons": {k: dict(v) for k, v in self.drop_reasons.items()},
            "deepest_stage": self.deepest_stage,
            "bottleneck_stage": stage,
            "bottleneck_reason": reason,
        }


@dataclass
class FunnelReport:
    """Every symbol's funnel, plus the venue-level arithmetic."""

    symbols: dict[str, SymbolFunnel] = field(default_factory=dict)

    def for_symbol(self, symbol: str) -> SymbolFunnel:
        return self.symbols.setdefault(symbol, SymbolFunnel(symbol))

    def bottleneck(self, symbol: str) -> tuple[str, str]:
        return self.for_symbol(symbol).bottleneck()

    def stage_totals(self) -> dict[str, int]:
        """How many *symbols* (not events) reached each stage."""
        return {
            s: sum(1 for f in self.symbols.values() if f.stages.get(s, 0) > 0)
            for s in STAGES
        }

    def wall(self) -> dict[str, int]:
        """Symbols per bottleneck stage — the venue's biggest losses, in order."""
        out: dict[str, int] = {}
        for f in self.symbols.values():
            stage, _ = f.bottleneck()
            out[stage] = out.get(stage, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: _STAGE_INDEX[kv[0]]))

    def concentration(self) -> dict[str, float]:
        """Share of trades per symbol, so 'BTC dominates' becomes a number."""
        traded = {s: f.stages.get("traded", 0) for s, f in self.symbols.items()}
        total = sum(traded.values())
        if total <= 0:
            return {}
        return {s: n / total for s, n in sorted(traded.items(), key=lambda kv: -kv[1]) if n}

    def check(self) -> list[str]:
        return [p for f in self.symbols.values() for p in f.check()]

    def as_dict(self) -> dict:
        return {
            "symbols": {s: f.as_dict() for s, f in sorted(self.symbols.items())},
            "stage_totals": self.stage_totals(),
            "wall": self.wall(),
            "concentration": {k: round(v, 4) for k, v in self.concentration().items()},
            "violations": self.check(),
        }
