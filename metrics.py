"""Optional Prometheus metrics. Degrades gracefully if prometheus_client not installed."""
from __future__ import annotations

import sys

try:
    from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
    METRICS_AVAILABLE = True
except ImportError:
    METRICS_AVAILABLE = False
    generate_latest = None  # type: ignore[assignment]
    CONTENT_TYPE_LATEST = None  # type: ignore[assignment]

# Persist metrics across importlib.reload() by storing them in sys.modules metadata.
# On reload, this module is re-executed but sys.modules["metrics"] still points to the
# OLD module object whose attributes hold the already-registered Prometheus collectors.
_prev = sys.modules.get("metrics")


def _reuse_or_create(name, factory):
    """Reuse metric from previous module version (reload) or create new."""
    if _prev is not None and hasattr(_prev, name):
        return getattr(_prev, name)
    return factory()


if METRICS_AVAILABLE:
    ORDERS_CREATED = _reuse_or_create("ORDERS_CREATED",
        lambda: Counter("cafe_orders_created_total", "Total orders created"))
    ORDERS_PAID = _reuse_or_create("ORDERS_PAID",
        lambda: Counter("cafe_orders_paid_total", "Total orders paid"))
    ORDERS_CANCELLED = _reuse_or_create("ORDERS_CANCELLED",
        lambda: Counter("cafe_orders_cancelled_total", "Total orders cancelled"))
    FISCAL_RETRIES = _reuse_or_create("FISCAL_RETRIES",
        lambda: Counter("cafe_fiscal_retries_total", "Total fiscal retry attempts"))
    FISCAL_FAILURES = _reuse_or_create("FISCAL_FAILURES",
        lambda: Counter("cafe_fiscal_failures_total", "Fiscal operations exhausted all retries"))
    PAYMENT_WEBHOOKS = _reuse_or_create("PAYMENT_WEBHOOKS",
        lambda: Counter("cafe_payment_webhooks_total", "Payment webhooks received", ["result"]))
    PAYMENT_ERRORS = _reuse_or_create("PAYMENT_ERRORS",
        lambda: Counter("cafe_payment_errors_total", "Payment processing errors", ["type"]))
    PAYMENT_DURATION = _reuse_or_create("PAYMENT_DURATION",
        lambda: Histogram("cafe_payment_processing_seconds",
                          "Time to process a paid order (end-to-end)",
                          buckets=[0.5, 1, 2, 5, 10, 30, 60]))
else:
    class _NoOp:
        def inc(self, *a, **kw): pass
        def observe(self, *a, **kw): pass
        def labels(self, *a, **kw): return self
        def time(self): return _NoOpCtx()

    class _NoOpCtx:
        def __enter__(self): return self
        def __exit__(self, *a): pass

    ORDERS_CREATED = _NoOp()  # type: ignore[assignment]
    ORDERS_PAID = _NoOp()  # type: ignore[assignment]
    ORDERS_CANCELLED = _NoOp()  # type: ignore[assignment]
    FISCAL_RETRIES = _NoOp()  # type: ignore[assignment]
    FISCAL_FAILURES = _NoOp()  # type: ignore[assignment]
    PAYMENT_WEBHOOKS = _NoOp()  # type: ignore[assignment]
    PAYMENT_ERRORS = _NoOp()  # type: ignore[assignment]
    PAYMENT_DURATION = _NoOp()  # type: ignore[assignment]
