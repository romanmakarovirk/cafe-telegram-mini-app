from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select, update

import database
from statuses import OrderStatus, PaymentStatus
from config import (
    APP_BASE_URL,
    KEEPALIVE_INTERVAL_SECONDS,
    KEEPALIVE_STARTUP_DELAY_SECONDS,
    ORDER_PAYMENT_TIMEOUT_MINUTES,
)
from models import MenuItem, Order


async def _keep_alive_ping() -> None:
    """Self-ping to prevent Render free tier from sleeping."""
    import aiohttp

    await asyncio.sleep(KEEPALIVE_STARTUP_DELAY_SECONDS)
    url = f"{APP_BASE_URL}/healthz"
    logging.info("Keep-alive started: pinging %s every %d sec", url, KEEPALIVE_INTERVAL_SECONDS)
    try:
        async with aiohttp.ClientSession() as s:
            while True:
                try:
                    async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                        logging.info("Keep-alive ping: %s", r.status)
                except Exception as exc:
                    logging.warning("Keep-alive ping failed: %s", exc)
                await asyncio.sleep(KEEPALIVE_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        logging.info("Keep-alive worker shutting down")
        raise


async def _stoplist_auto_enable_worker() -> None:
    """Фоновая задача: автоматическое включение блюд по расписанию."""
    await asyncio.sleep(30)
    try:
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
                        try:
                            from routes import invalidate_menu_cache
                            invalidate_menu_cache()
                        except ImportError:
                            pass
            except Exception:
                logging.exception("Ошибка в stoplist auto-enable worker")

            await asyncio.sleep(60)
    except asyncio.CancelledError:
        logging.info("Stoplist auto-enable worker shutting down")
        raise


async def _order_timeout_worker() -> None:
    """Фоновая задача: отмена неоплаченных заказов по таймауту."""
    await asyncio.sleep(60)
    try:
        while True:
            try:
                cutoff = database.now_utc() - timedelta(minutes=ORDER_PAYMENT_TIMEOUT_MINUTES)
                with database.db_session() as session:
                    # Логируем заказы, которые будут отменены
                    expired_orders = session.scalars(
                        select(Order).where(
                            Order.status == OrderStatus.CREATED,
                            Order.payment_status == PaymentStatus.PENDING,
                            Order.created_at < cutoff,
                        )
                    ).all()
                    for order in expired_orders:
                        logging.info(
                            "Таймаут оплаты: заказ №%d будет отменён (создан %s)",
                            order.public_order_number,
                            order.created_at.isoformat(),
                        )

                    # Atomic conditional UPDATE
                    if expired_orders:
                        session.execute(
                            update(Order).where(
                                Order.status == OrderStatus.CREATED,
                                Order.payment_status == PaymentStatus.PENDING,
                                Order.created_at < cutoff,
                            ).values(
                                status=OrderStatus.CANCELLED,
                                payment_status=PaymentStatus.EXPIRED,
                                updated_at=database.now_utc(),
                            )
                        )
                        session.commit()

                    # Cleanup зависших маркеров "creating" (crash при создании платежа)
                    creating_cutoff = database.now_utc() - timedelta(minutes=5)
                    stuck_creating = session.scalars(
                        select(Order).where(
                            Order.gateway_order_id == "creating",
                            Order.updated_at < creating_cutoff,
                        )
                    ).all()
                    if stuck_creating:
                        for order in stuck_creating:
                            logging.warning(
                                "Timeout worker: сброс зависшего маркера 'creating' для заказа №%d",
                                order.public_order_number,
                            )
                        session.execute(
                            update(Order).where(
                                Order.gateway_order_id == "creating",
                                Order.updated_at < creating_cutoff,
                            ).values(
                                gateway_order_id=None,
                                updated_at=database.now_utc(),
                            )
                        )
                        session.commit()

                    # Обнаружение и auto-retry застрявших refund_pending заказов
                    refund_stuck_cutoff = database.now_utc() - timedelta(minutes=10)
                    stuck_refunds = session.scalars(
                        select(Order).where(
                            Order.payment_status == PaymentStatus.REFUND_PENDING,
                            Order.updated_at < refund_stuck_cutoff,
                        )
                    ).all()
                    stuck_data = [
                        (o.id, o.public_order_number, o.gateway_order_id, o.total_amount)
                        for o in stuck_refunds
                    ]
                    session.commit()  # release before async I/O

                for oid, onum, gw_oid, amount in stuck_data:
                    if not gw_oid or gw_oid == "creating":
                        logging.critical(
                            "Stuck refund_pending: заказ №%d без gateway_order_id, ручной возврат",
                            onum,
                        )
                        from bot_handlers import alert_admin
                        await alert_admin(
                            f"ЗАСТРЯВШИЙ ВОЗВРАТ: заказ #{onum} без gateway_order_id. "
                            f"Требуется ручной возврат через ЛК ЮKassa!"
                        )
                        continue

                    from payments.yookassa_payment import refund_yookassa_payment
                    refund_result = await refund_yookassa_payment(gw_oid, amount)
                    with database.db_session() as refund_session:
                        order = refund_session.scalars(
                            select(Order).where(
                                Order.id == oid,
                                Order.payment_status == PaymentStatus.REFUND_PENDING,
                            ).with_for_update()
                        ).first()
                        if not order:
                            refund_session.commit()
                            continue
                        if refund_result.success:
                            order.payment_status = PaymentStatus.REFUNDED
                            order.updated_at = database.now_utc()
                            refund_session.commit()
                            logging.info(
                                "Auto-retry refund: заказ №%d успешно возвращён", onum,
                            )
                            from bot_handlers import alert_admin
                            await alert_admin(
                                f"Автовозврат: заказ #{onum} — деньги возвращены клиенту (auto-retry)."
                            )
                        else:
                            order.payment_status = PaymentStatus.REFUND_FAILED
                            order.updated_at = database.now_utc()
                            refund_session.commit()
                            logging.critical(
                                "Auto-retry refund FAILED: заказ №%d, ошибка: %s",
                                onum, refund_result.error_message,
                            )
                            from bot_handlers import alert_admin
                            await alert_admin(
                                f"ВОЗВРАТ НЕ УДАЛСЯ (auto-retry): заказ #{onum}. "
                                f"Ошибка: {refund_result.error_message}. "
                                f"Требуется ручной возврат через ЛК ЮKassa!"
                            )
            except Exception:
                logging.exception("Ошибка в order timeout worker")

            await asyncio.sleep(60)
    except asyncio.CancelledError:
        logging.info("Order timeout worker shutting down")
        raise


async def _yookassa_payment_polling_worker() -> None:
    """Фоновая задача: опрос статуса pending платежей через ЮKassa API.

    Webhook от ЮKassa — основной механизм. Этот worker — подстраховка
    на случай если webhook не дошёл (сетевой сбой, рестарт сервера).
    """
    from payments.yookassa_payment import check_yookassa_payment

    await asyncio.sleep(30)  # Ждём старт приложения

    try:
        while True:
            try:
                with database.db_session() as session:
                    cutoff_recent = database.now_utc() - timedelta(seconds=30)
                    cutoff_old = database.now_utc() - timedelta(minutes=ORDER_PAYMENT_TIMEOUT_MINUTES)
                    pending_orders = session.scalars(
                        select(Order).where(
                            Order.payment_status == PaymentStatus.PENDING,
                            Order.gateway_order_id.isnot(None),
                            Order.gateway_order_id != "creating",
                            Order.updated_at < cutoff_recent,
                            Order.created_at > cutoff_old,
                        ).limit(10)
                    ).all()

                    orders_to_check = [
                        (o.id, o.gateway_order_id, o.total_amount)
                        for o in pending_orders
                    ]

                for order_id, gateway_order_id, total_amount in orders_to_check:
                    try:
                        result = await check_yookassa_payment(gateway_order_id)
                        if not result.success:
                            continue

                        if result.is_paid:
                            expected_kopecks = total_amount * 100
                            if result.amount is not None and result.amount != expected_kopecks:
                                with database.db_session() as session:
                                    order = session.scalars(
                                        select(Order).where(
                                            Order.id == order_id
                                        ).with_for_update()
                                    ).first()
                                    if order and order.payment_status == PaymentStatus.PENDING:
                                        order.payment_status = PaymentStatus.AMOUNT_MISMATCH
                                        order.updated_at = database.now_utc()
                                        session.commit()
                                    else:
                                        session.commit()

                                logging.critical(
                                    "YooKassa polling: AMOUNT MISMATCH order %d expected %d got %d",
                                    order_id, expected_kopecks, result.amount,
                                )
                                from bot_handlers import alert_admin
                                await alert_admin(
                                    f"AMOUNT MISMATCH (polling): заказ #{order_id}, "
                                    f"ожидали {expected_kopecks} коп, получили {result.amount} коп"
                                )
                                continue

                            from routes import _process_paid_order
                            await _process_paid_order(order_id)
                            logging.info(
                                "YooKassa polling: заказ %d оплачен (webhook не пришёл, обнаружен polling)",
                                order_id,
                            )

                    except Exception:
                        logging.exception("YooKassa polling: ошибка проверки заказа %d", order_id)

                # Проверяем expired заказы — деньги могли списаться после таймаута
                with database.db_session() as session:
                    expired_cutoff = database.now_utc() - timedelta(minutes=ORDER_PAYMENT_TIMEOUT_MINUTES)
                    expired_floor = database.now_utc() - timedelta(minutes=ORDER_PAYMENT_TIMEOUT_MINUTES * 2)
                    expired_orders = session.scalars(
                        select(Order).where(
                            Order.payment_status == PaymentStatus.EXPIRED,
                            Order.gateway_order_id.isnot(None),
                            Order.gateway_order_id != "creating",
                            Order.updated_at > expired_floor,
                            Order.updated_at < expired_cutoff,
                        ).limit(5)
                    ).all()
                    expired_to_check = [
                        (o.id, o.gateway_order_id, o.total_amount, o.public_order_number)
                        for o in expired_orders
                    ]

                for oid, gw_oid, amount, onum in expired_to_check:
                    try:
                        result = await check_yookassa_payment(gw_oid)
                        if not result.success:
                            continue
                        if result.is_paid:
                            expected_kopecks = amount * 100
                            if result.amount is not None and result.amount != expected_kopecks:
                                logging.critical(
                                    "YooKassa polling: AMOUNT MISMATCH expired order %d "
                                    "expected %d got %d",
                                    oid, expected_kopecks, result.amount,
                                )
                                from bot_handlers import alert_admin
                                await alert_admin(
                                    f"AMOUNT MISMATCH expired заказ #{onum}: "
                                    f"ожидали {expected_kopecks} коп, получили {result.amount} коп."
                                )
                                continue

                            logging.warning(
                                "YooKassa polling: expired заказ %d оплачен! Воскрешаем.", oid,
                            )
                            from routes import _process_paid_order
                            await _process_paid_order(oid)
                            from bot_handlers import alert_admin
                            await alert_admin(
                                f"Заказ #{onum} оплачен ПОСЛЕ таймаута (webhook потерян). "
                                f"Заказ автоматически воскрешён."
                            )
                    except Exception:
                        logging.exception("YooKassa polling: ошибка проверки expired заказа %d", oid)

            except Exception:
                logging.exception("Ошибка в YooKassa payment polling worker")

            await asyncio.sleep(15)
    except asyncio.CancelledError:
        logging.info("YooKassa payment polling worker shutting down")
        raise
