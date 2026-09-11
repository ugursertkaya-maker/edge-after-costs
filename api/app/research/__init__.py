"""Phase 3 research package — the profitability search.

Nothing in here runs on the server or touches the live paper engine.  Every
module is offline, deterministic and driven from cached data on the laptop.

Modules are owned by exactly one workstream (see ``docs/PHASE3_SPEC.md``):

* ``dataset``      — aggTrades download/cache, multi-timeframe bar construction
* ``features_mtf`` — the multi-timeframe feature block
* ``regimes``      — the seven-state regime classifier
* ``families``     — parameterised strategy families
* ``tournament``   — the grid / walk-forward search
* ``robustness``   — cost stress, Monte Carlo, the promotion gates
* ``maker_replay`` — queue-aware maker/taker replay on the tape

Imports here are intentionally lazy: importing ``app.research`` must not drag
in half the search machinery just because something wanted ``build_bars``.
"""

from __future__ import annotations

__all__ = ["dataset", "features_mtf", "regimes"]
