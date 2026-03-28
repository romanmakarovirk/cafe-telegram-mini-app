"""Optional Prometheus metrics. Degrades gracefully if prometheus_client not installed."""
from __future__ import annotations

try:
    from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
    METRICS_AVAILABLE = True
except ImportError:
    METRICS_AVAILABLE = False
    generate_latest = None  # type: ignore[assignment]
    CONTENT_TYPE_LATEST = None  # type: ignore[assignment]

if METRICS_AVAILABLE:
    ORDERS_CREATED = Counter("cafe_orders_created_total", "Total orders created")
    ORDERS_PAID = Counter("cafe_orders_paid_total", "Total orders paid")
    ORDERS_CANCELLED = Counter("cafe_orders_cancelled_total", "Total orders cancelled")
    FISCAL_RETRIES = Counter("cafe_fiscal_retries_total", "Total fiscal retry attempts")
    FISCAL_FAILURES = Counter("cafe_fiscal_failures_total", "Fiscal operations exhausted all retries")
    PAYMENT_WEBHOOKS = Counter("cafe_payment_webhooks_total", "Payment webhooks received", ["result"])
    PAYMENT_ERRORS = Counter("cafe_payment_errors_total", "Payment processing errors", ["type"])
    PAYMENT_DURATION = Histogram(
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
