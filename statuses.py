"""Перечисления статусов заказов, платежей и фискальных операций."""
from __future__ import annotations

from enum import StrEnum


class OrderStatus(StrEnum):
    CREATED = "created"
    PAID = "paid"
    PREPARING = "preparing"
    READY = "ready"
    CANCELLED = "cancelled"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    AMOUNT_MISMATCH = "amount_mismatch"
    REFUND_PENDING = "refund_pending"
    REFUNDED = "refunded"
    REFUND_FAILED = "refund_failed"


class FiscalOperation(StrEnum):
    SELL = "sell"
    SELL_REFUND = "sell_refund"
    SELL_SETTLEMENT = "sell_settlement"


class FiscalQueueStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
