"""The paper champion: the one strategy the live engine is currently running.

A champion is promoted only after it clears the robustness gates out-of-sample.
Promotion is deliberately heavyweight:

* it **bumps the simulator version**, so live P&L from before and after are
  never mixed into one number.  The brief is explicit about this and it is the
  difference between a track record and a story;
* it records *why* it was promoted — the out-of-sample numbers and the
  tournament run that produced them — so a champion can be audited later
  against what it actually went on to do;
* it is written to disk, not held in memory, so a worker restart keeps running
  the same strategy rather than silently reverting.

There is deliberately no auto-promotion path from a backtest. A tournament
produces `GateReport`s; a human (or an explicit command) promotes one. Nothing
in this module places an order, and nothing in it can enable live trading.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("edge_after_costs.champion")

CHAMPION_FILENAME = "champion.json"
REDIS_KEY = "edge_after_costs:champion"


@dataclass(frozen=True)
class Champion:
    """A frozen, versioned strategy configuration."""

    key: str
    family: str
    params: dict[str, Any] = field(default_factory=dict)
    symbol: str | None = None
    regime: str | None = None
    horizon_seconds: int = 1_800
    exit_profile: str = "net_pct"
    stop_profile: str = "atr"
    min_score: float = 0.0

    # Provenance — what earned the promotion.
    simulator_version: str = "sim-1.0.0"
    promoted_ms: int = 0
    source_run: str = ""
    oos_trades: int = 0
    oos_profit_factor: float = 0.0
    oos_expectancy: float = 0.0
    oos_net_pnl: float = 0.0
    oos_max_drawdown: float = 0.0
    gate_reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["gate_reasons"] = list(self.gate_reasons)
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Champion":
        known = {f for f in cls.__dataclass_fields__}
        data = {k: v for k, v in raw.items() if k in known}
        data["gate_reasons"] = tuple(data.get("gate_reasons") or ())
        data["params"] = dict(data.get("params") or {})
        return cls(**data)

    def describe(self) -> str:
        scope = self.symbol or "all coins"
        regime = f" in {self.regime}" if self.regime else ""
        return (
            f"{self.family} on {scope}{regime}, {self.horizon_seconds // 60}m horizon, "
            f"OOS PF {self.oos_profit_factor:.2f} over {self.oos_trades} trades"
        )


def next_version(current: str) -> str:
    """Bump the patch component of ``sim-X.Y.Z``.

    A promotion always changes the version, because the dashboard and the
    equity curve split their statistics on it. If the current value cannot be
    parsed, fall back to a timestamped version rather than reusing one — a
    collision would silently merge two strategies' records.
    """
    prefix, _, rest = current.partition("-")
    parts = rest.split(".")
    if prefix == "sim" and len(parts) == 3 and all(p.isdigit() for p in parts):
        major, minor, patch = (int(p) for p in parts)
        return f"sim-{major}.{minor}.{patch + 1}"
    return f"sim-{int(time.time())}"


class ChampionStore:
    """Reads and writes the champion record.

    Disk is the source of truth; Redis is a cache so the API container can serve
    it without a shared filesystem. A Redis failure is never fatal.
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        redis_client: Any = None,
    ) -> None:
        self.dir = Path(directory)
        self.path = self.dir / CHAMPION_FILENAME
        self.redis = redis_client

    # -- read ----------------------------------------------------------- #

    def load(self) -> Champion | None:
        raw = self._read_disk()
        if raw is None:
            raw = self._read_redis()
        if raw is None:
            return None
        try:
            return Champion.from_dict(raw)
        except (TypeError, ValueError) as exc:
            log.warning("champion record is unreadable, ignoring it: %s", exc)
            return None

    def _read_disk(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def _read_redis(self) -> dict[str, Any] | None:
        if self.redis is None:
            return None
        try:
            raw = self.redis.get(REDIS_KEY)
            return json.loads(raw) if raw else None
        except Exception:
            return None

    # -- write ---------------------------------------------------------- #

    def promote(
        self,
        champion: Champion,
        *,
        current_version: str | None = None,
        now_ms: int | None = None,
    ) -> Champion:
        """Store ``champion``, stamped with a fresh simulator version.

        Returns the stored record, which is what callers should use — the
        version and timestamp are assigned here, not by the caller, so two
        promotions can never share a version.
        """
        from dataclasses import replace

        base = current_version or self.current_version() or champion.simulator_version
        stamped = replace(
            champion,
            simulator_version=next_version(base),
            promoted_ms=now_ms if now_ms is not None else int(time.time() * 1000),
        )
        self._write(stamped.as_dict())
        log.info(
            "champion promoted: %s (version %s)",
            stamped.describe(), stamped.simulator_version,
        )
        return stamped

    def current_version(self) -> str | None:
        c = self.load()
        return c.simulator_version if c else None

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not remove champion record: %s", exc)
        if self.redis is not None:
            try:
                self.redis.delete(REDIS_KEY)
            except Exception:
                pass

    def _write(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            # Atomic: a half-written champion file would be worse than none.
            fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
            with os.fdopen(fd, "w") as fh:
                fh.write(body)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("could not persist champion to disk: %s", exc)
        if self.redis is not None:
            try:
                self.redis.set(REDIS_KEY, body)
            except Exception:
                pass


def champion_from_gate_report(
    report: Any,
    *,
    arm: Any,
    source_run: str = "",
) -> Champion | None:
    """Build a `Champion` from a passing `GateReport` and its `ArmResult`.

    Returns ``None`` for a report that did not pass, so a caller cannot promote
    a failing strategy by accident — the check lives here rather than at every
    call site.
    """
    if not getattr(report, "passed", False):
        return None
    candidate = getattr(arm, "candidate", None)
    if candidate is None:
        return None
    oos = getattr(arm, "out_of_sample", None)

    def stat(name: str, default: float = 0.0) -> float:
        return float(getattr(oos, name, default) or default) if oos else default

    regime = getattr(candidate, "regime", None)
    return Champion(
        key=getattr(candidate, "key", lambda: "")() if callable(
            getattr(candidate, "key", None)
        ) else str(getattr(candidate, "key", "")),
        family=str(getattr(candidate, "family", "")),
        params=dict(getattr(candidate, "params", {}) or {}),
        symbol=getattr(candidate, "symbol", None),
        regime=getattr(regime, "value", regime) if regime else None,
        horizon_seconds=int(getattr(candidate, "horizon_seconds", 1_800)),
        exit_profile=str(getattr(candidate, "exit_profile", "net_pct")),
        stop_profile=str(getattr(candidate, "stop_profile", "atr")),
        source_run=source_run,
        oos_trades=int(getattr(oos, "trades", 0) or 0) if oos else 0,
        oos_profit_factor=stat("profit_factor"),
        oos_expectancy=stat("expectancy"),
        oos_net_pnl=stat("net_pnl"),
        oos_max_drawdown=stat("max_drawdown"),
        gate_reasons=tuple(getattr(report, "reasons", ()) or ()),
    )
