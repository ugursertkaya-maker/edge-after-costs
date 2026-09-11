"""Paper execution and paper accounting.

``PaperSimulator`` turns an order request into a costed fill against a real
order book; ``Ledger`` holds the cash, the open positions and the closed
trades.  Nothing in this package can reach the network — a simulated fill is
the only kind of fill this codebase knows how to produce.
"""

from __future__ import annotations

from app.sim.ledger import Ledger
from app.sim.simulator import PaperSimulator

__all__ = ["Ledger", "PaperSimulator"]
