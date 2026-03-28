"""Optional Prometheus metrics. Degrades gracefully if prometheus_client not installed."""
from __future__ import annotations

try:
    from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST, REGISTRY
    METRICS_AVAILABLE = True
except ImportError:
    METRICS_AVAILABLE = False
    generate_latest = None  # type: ignore[assignment]
    CONTENT_TYPE_LATEST = None  # type: ignore[assignment]


def _counter(name: str, desc: str, labels: list[str] | None = None):
    """Create or retrieve existing Counter (safe for module reload)."""
    try:
        if labels:
            return Counter(name, desc, labels)
        return Counter(name, desc)
    except ValueError:
        # Already registered — retrieve from registry
        for collector in REGISTRY._names_to_collectors.values():
            if getattr(collector, "_name", None) == name:
                return collector
        raise


def _histogram(name: str, desc: str, **kwargs):
    """Create or retrieve existing Histogram (safe for module reload)."""
    try:
        return Histogram(name, desc, **kwargs)
    except ValueError:
        for collector in REGISTRY._names_to_collectors.values():
            if getattr(collector, "_name", None) == name:
                return collector
        raise


if METRICS_AVAILABLE:
    ORDERS_CREATED = _counter("cafe_orders_created_total", "Total orders created")
    ORDERS_PAID = _counter("cafe_orders_paid_total", "Total orders paid")
    ORDERS_CANCELLED = _counter("cafe_orders_cancelled_total", "Total orders cancelled")
    FISCAL_RETRIES = _counter("cafe_fiscal_retries_total", "Total fiscal retry attempts")
    FISCAL_FAILURES = _counter("cafe_fiscal_failures_total", "Fiscal operations exhausted all retries")
    PAYMENT_WEBHOOKS = _counter("cafe_payment_webhooks_total", "Payment webhooks received", ["result"])
    PAYMENT_ERRORS = _counter("cafe_payment_errors_total", "Payment processing errors", ["type"])
    PAYMENT_DURATION = _histogram(
        "cafe_payment_processing_seconds",
        "Time to process a paid order (end-to-end)",
        buckets=[0.5, 1, 2, 5, 10, 30, 60],
    )
else:
    # No-op stubs when prometheus_client is not installed
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
