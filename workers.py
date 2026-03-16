from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select

import database
from config import (
    APP_BASE_URL,
    FISCAL_INITIAL_DELAY_SECONDS,
    FISCAL_RETRY_BATCH_SIZE,
    KEEPALIVE_INTERVAL_SECONDS,
    KEEPALIVE_STARTUP_DELAY_SECONDS,
    ORDER_PAYMENT_TIMEOUT_MINUTES,
)
from models import FiscalQueue, MenuItem, Order


async def _keep_alive_ping() -> None:
    """Self-ping to prevent Render free tier from sleeping."""
    import aiohttp

    await asyncio.sleep(KEEPALIVE_STARTUP_DELAY_SECONDS)
    url = f"{APP_BASE_URL}/healthz"
    logging.info("Keep-alive started: pinging %s every %d sec", url, KEEPALIVE_INTERVAL_SECONDS)
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    logging.info("Keep-alive ping: %s", r.status)
        except Exception as exc:
            logging.warning("Keep-alive ping failed: %s", exc)
        await asyncio.sleep(KEEPALIVE_INTERVAL_SECONDS)


async def _stoplist_auto_enable_worker() -> None:
    """Фоновая задача: автоматическое включение блюд по расписанию."""
    await asyncio.sleep(30)
    while True:
        try:
            with database.db_session() as session:
                now = database.now_utc()
                expired = session.scalars(
                    select(MenuItem).where(
                        MenuItem.is_available.is_(False),
                        MenuItem.available_at.isnot(None),
                        MenuItem.available_at <= now,
                    )
                ).all()

                for item in expired:
                    item.is_available = True
                    item.unavailable_reason = None
                    item.available_at = None
                    logging.info("Стоп-лист: авто-включение '%s' (id=%d)", item.name, item.id)

                if expired:
                    session.commit()
        except Exception:
            logging.exception("Ошибка в stoplist auto-enable worker")

        await asyncio.sleep(60)


async def _order_timeout_worker() -> None:
    """Фоновая задача: отмена неоплаченных заказов по таймауту."""
    await asyncio.sleep(60)
    while True:
        try:
            cutoff = database.now_utc() - timedelta(minutes=ORDER_PAYMENT_TIMEOUT_MINUTES)
            with database.db_session() as session:
                expired_orders = session.scalars(
                    select(Order).where(
                        Order.status == "created",
                        Order.payment_status == "pending",
                        Order.created_at < cutoff,
                    )
                ).all()

                for order in expired_orders:
                    order.status = "cancelled"
                    order.payment_status = "expired"
                    order.updated_at = database.now_utc()
                    logging.info(
                        "Таймаут оплаты: заказ №%d отменён (создан %s)",
                        order.public_order_number,
                        order.created_at.isoformat(),
                    )

                if expired_orders:
                    session.commit()
        except Exception:
            logging.exception("Ошибка в order timeout worker")

        await asyncio.sleep(60)


async def _fiscal_retry_worker() -> None:
    """Фоновая задача: повторная фискализация неудачных чеков (54-ФЗ)."""
    from payments.fiscal import fiscalize_order
    import json as json_module

    await asyncio.sleep(FISCAL_INITIAL_DELAY_SECONDS)

    # Восстановление stuck записей
    try:
        with database.db_session() as session:
            stuck = session.scalars(
                select(FiscalQueue).where(FiscalQueue.status == "processing")
            ).all()
            for fq in stuck:
                fq.status = "pending"
                fq.next_retry_at = database.now_utc()
                logging.warning("Fiscal retry: восстановление stuck записи order_id=%d", fq.order_id)
            if stuck:
                session.commit()
    except Exception:
        logging.exception("Ошибка восстановления fiscal_queue processing records")

    while True:
        try:
            with database.db_session() as session:
                pending = session.scalars(
                    select(FiscalQueue).where(
                        FiscalQueue.status == "pending",
                        FiscalQueue.next_retry_at <= database.now_utc(),
                        FiscalQueue.attempts < FiscalQueue.max_attempts,
                    ).order_by(FiscalQueue.next_retry_at).limit(FISCAL_RETRY_BATCH_SIZE)
                ).all()

                for fq in pending:
                    parent_order = session.get(Order, fq.order_id)
                    if parent_order and parent_order.status == "cancelled":
                        fq.status = "failed"
                        fq.last_error = "Order cancelled — fiscal retry skipped"
                        session.commit()
                        logging.info("Fiscal retry: пропуск отменённого заказа %d", fq.order_id)
                        continue

                    fq.status = "processing"
                    fq.attempts += 1
                    session.commit()

                    try:
                        payload = json_module.loads(fq.payload_json)
                        result = await fiscalize_order(
                            order_id=fq.order_id,
                            order_number=fq.order_number,
                            items=payload["items"],
                            total_amount=payload["total_amount"],
                        )

                        if result.success and result.uuid:
                            fq.status = "done"
                            fq.fiscal_uuid = result.uuid
                            fq.completed_at = database.now_utc()
                            order = session.get(Order, fq.order_id)
                            if order:
                                order.fiscal_uuid = result.uuid
                            logging.info(
                                "Fiscal retry: чек создан для заказа %d (попытка %d)",
                                fq.order_id, fq.attempts,
                            )
                        else:
                            fq.status = "pending"
                            fq.last_error = str(result.error)[:500] if result.error else "Unknown error"
                            backoff_minutes = min(fq.attempts * 5, 120)
                            fq.next_retry_at = database.now_utc() + timedelta(minutes=backoff_minutes)
                            logging.warning(
                                "Fiscal retry: ошибка для заказа %d (попытка %d): %s",
                                fq.order_id, fq.attempts, fq.last_error,
                            )
                    except Exception as exc:
                        fq.status = "pending"
                        fq.last_error = str(exc)[:500]
                        backoff_minutes = min(fq.attempts * 5, 120)
                        fq.next_retry_at = database.now_utc() + timedelta(minutes=backoff_minutes)
                        logging.exception("Fiscal retry: exception for order %d", fq.order_id)

                    if fq.attempts >= fq.max_attempts and fq.status == "pending":
                        fq.status = "failed"
                        logging.critical(
                            "Fiscal retry: ИСЧЕРПАНЫ ПОПЫТКИ для заказа %d (54-ФЗ нарушение!)",
                            fq.order_id,
                        )

                    session.commit()
        except Exception:
            logging.exception("Ошибка в fiscal retry worker")

        await asyncio.sleep(120)
