"""Phase 4 — the predictive entry engine.

Modules here answer one question: *given the market state now, what is the
probability this trade reaches a profitable NET exit before its stop, inside
the horizon, and what is the expected value after costs?*

Nothing in this package places or signs an order.  It is research plumbing
plus a numpy-only inference bundle the worker can evaluate.

The package deliberately has **no eager imports** of its own submodules: the
modules are owned by different workstreams and several of them pull in
scikit-learn, which the worker container must never need.  Import what you
want explicitly, e.g. ``from app.predict.labels import build_labels``.
"""
