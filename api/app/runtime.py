"""Runtime state shared between the worker and the API.

The worker and the API run in separate containers, so they cannot share Python
objects.  The worker owns all live state and publishes a rendered dashboard
payload to Redis after every evaluation cycle; the API reads that key and serves
it.  History (trades, equity, strategy performance) comes from Postgres, which
the worker also writes to.

Keeping the payload pre-rendered means the API container stays idle-cheap on a
2-vCPU host: it never recomputes features or statistics.
"""

from __future__ import annotations

import json
import time
from typing import Any

try:  # redis is optional in unit tests
    import redis as redis_lib
except Exception:  # pragma: no cover - exercised only when redis is absent
    redis_lib = None  # type: ignore[assignment]

from app.config import Settings, get_settings

DASHBOARD_KEY = "netedge:dashboard:{ledger}"
HEALTH_KEY = "netedge:health"
HEARTBEAT_KEY = "netedge:worker:heartbeat"
KILL_SWITCH_KEY = "netedge:kill_switch"

# If the worker has not written a heartbeat for this long, the API reports the
# data as stale rather than serving a frozen snapshot as if it were live.
HEARTBEAT_STALE_MS = 30_000


class RuntimeStore:
    """Thin Redis wrapper that degrades to an in-process dict when Redis is
    unavailable, so tests and local runs never need a server."""

    def __init__(self, *, settings: Settings | None = None, client: Any = None) -> None:
        self.settings = settings or get_settings()
        self._memory: dict[str, str] = {}
        if client is not None:
            self._client = client
        elif redis_lib is not None:
            try:
                self._client = redis_lib.Redis.from_url(
                    self.settings.redis_url, decode_responses=True, socket_timeout=2
                )
                self._client.ping()
            except Exception:
                self._client = None
        else:
            self._client = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    def _set(self, key: str, value: str, ttl: int | None = None) -> None:
        if self._client is not None:
            try:
                self._client.set(key, value, ex=ttl)
                return
            except Exception:
                pass
        self._memory[key] = value

    def _get(self, key: str) -> str | None:
        if self._client is not None:
            try:
                return self._client.get(key)
            except Exception:
                pass
        return self._memory.get(key)

    # -- dashboard ---------------------------------------------------------- #

    def publish_dashboard(self, ledger: str, payload: dict[str, Any]) -> None:
        self._set(DASHBOARD_KEY.format(ledger=ledger), json.dumps(payload), ttl=300)

    def read_dashboard(self, ledger: str) -> dict[str, Any] | None:
        raw = self._get(DASHBOARD_KEY.format(ledger=ledger))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    # -- health / heartbeat ------------------------------------------------- #

    def publish_health(self, payload: dict[str, Any]) -> None:
        self._set(HEALTH_KEY, json.dumps(payload), ttl=120)

    def read_health(self) -> dict[str, Any] | None:
        raw = self._get(HEALTH_KEY)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def beat(self) -> None:
        self._set(HEARTBEAT_KEY, str(int(time.time() * 1000)), ttl=120)

    def last_beat_ms(self) -> int:
        raw = self._get(HEARTBEAT_KEY)
        try:
            return int(raw) if raw else 0
        except ValueError:
            return 0

    def worker_alive(self, *, now_ms: int | None = None) -> bool:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        beat = self.last_beat_ms()
        return beat > 0 and (now - beat) < HEARTBEAT_STALE_MS

    # -- kill switch -------------------------------------------------------- #

    def set_kill_switch(self, on: bool) -> None:
        self._set(KILL_SWITCH_KEY, "1" if on else "0")

    def kill_switch(self) -> bool:
        return self._get(KILL_SWITCH_KEY) == "1"
