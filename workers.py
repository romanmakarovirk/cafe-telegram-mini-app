from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select, update

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
    async with aiohttp.ClientSession() as s:
        while True:
            try:
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
                    try:
                        from routes import invalidate_menu_cache
                        invalidate_menu_cache()
                    except ImportError:
                        pass
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
                # Логируем заказы, которые будут отменены
                expired_orders = session.scalars(
                    select(Order).where(
                        Order.status == "created",
                        Order.payment_status == "pending",
                        Order.created_at < cutoff,
                    )
                ).all()
                for order in expired_orders:
                    logging.info(
                        "Таймаут оплаты: заказ №%d будет отменён (создан %s)",
                        order.public_order_number,
                        order.created_at.isoformat(),
                    )

                # Atomic conditional UPDATE — не перезапишет заказ,
                # если payment_status изменился между SELECT и UPDATE
                if expired_orders:
                    session.execute(
                        update(Order).where(
                            Order.status == "created",
                            Order.payment_status == "pending",
                            Order.created_at < cutoff,
                        ).values(
                            status="cancelled",
                            payment_status="expired",
                            updated_at=database.now_utc(),
                        )
                    )
                    session.commit()

                # Cleanup зависших маркеров "creating" (crash при создании платежа)
                # Atomic UPDATE — не перезапишет gateway_order_id, если он
                # уже изменился между SELECT и UPDATE (race condition protection)
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
                # (crash между commit и SBP refund call)
                refund_stuck_cutoff = database.now_utc() - timedelta(minutes=10)
                stuck_refunds = session.scalars(
                    select(Order).where(
                        Order.payment_status == "refund_pending",
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
                            f"Требуется ручной возврат через ЛК банка!"
                        )
                        continue

                    from payments.sbp import refund_sbp_payment
                    refund_result = await refund_sbp_payment(gw_oid, amount)
                    with database.db_session() as refund_session:
                        order = refund_session.scalars(
                            select(Order).where(
                                Order.id == oid,
                                Order.payment_status == "refund_pending",
                            ).with_for_update()
                        ).first()
                        if not order:
                            refund_session.commit()
                            continue
                        if refund_result.success:
                            order.payment_status = "refunded"
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
                            order.payment_status = "refund_failed"
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
                                f"Требуется ручной возврат через ЛК банка!"
                            )
        except Exception:
            logging.exception("Ошибка в order timeout worker")

        await asyncio.sleep(60)


async def _fiscal_retry_worker() -> None:
    """Фоновая задача: повторная фискализация неудачных чеков (54-ФЗ)."""
    from payments.fiscal import fiscalize_order, refund_order
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
                    .with_for_update(skip_locked=True)
                ).all()

                for fq in pending:
                    parent_order = session.get(Order, fq.order_id)
                    if parent_order and parent_order.status == "cancelled" and \
                            parent_order.payment_status not in ("refunded", "refund_pending", "refund_failed"):
                        fq.status = "failed"
                        fq.last_error = "Order cancelled — fiscal retry skipped"
                        session.commit()
                        logging.info("Fiscal retry: пропуск отменённого заказа %d", fq.order_id)
                        continue

                    # sell_refund нельзя обработать раньше sell (54-ФЗ)
                    if fq.operation == "sell_refund":
                        if parent_order and not parent_order.fiscal_prepayment_uuid:
                            fq.next_retry_at = database.now_utc() + timedelta(minutes=5)
                            session.commit()
                            logging.info(
                                "Fiscal retry: sell_refund для заказа %d отложен — ждём sell чек",
                                fq.order_id,
                            )
                            continue

                    fq.status = "processing"
                    fq.attempts += 1
                    session.commit()

                    try:
                        payload = json_module.loads(fq.payload_json)

                        if fq.operation == "sell_refund":
                            # Чек возврата — другая функция АТОЛ API
                            result = await refund_order(
                                order_id=fq.order_id,
                                order_number=fq.order_number,
                                items=payload["items"],
                                total_amount=payload["total_amount"],
                                payment_method=payload.get("payment_method", "prepayment"),
                            )
                        else:
                            # Чек продажи: prepayment (Phase 1) или full_payment (Phase 2)
                            pm = "prepayment" if fq.operation == "sell" else "full_payment"
                            result = await fiscalize_order(
                                order_id=fq.order_id,
                                order_number=fq.order_number,
                                items=payload["items"],
                                total_amount=payload["total_amount"],
                                payment_method=pm,
                            )

                        if result.success and result.uuid:
                            fq.status = "done"
                            fq.fiscal_uuid = result.uuid
                            fq.completed_at = database.now_utc()
                            order = session.get(Order, fq.order_id)
                            if order:
                                if fq.operation == "sell":
                                    order.fiscal_prepayment_uuid = result.uuid
                                else:
                                    order.fiscal_uuid = result.uuid
                            logging.info(
                                "Fiscal retry: чек создан для заказа %d (попытка %d)",
                                fq.order_id, fq.attempts,
                            )
                        else:
                            fq.status = "pending"
                            fq.last_error = str(result.error)[:500] if result.error else "Unknown error"
                            backoff_minutes = min(5 * 2 ** (fq.attempts - 1), 120)
                            fq.next_retry_at = database.now_utc() + timedelta(minutes=backoff_minutes)
                            logging.warning(
                                "Fiscal retry: ошибка для заказа %d (попытка %d): %s",
                                fq.order_id, fq.attempts, fq.last_error,
                            )
                    except (json_module.JSONDecodeError, KeyError) as exc:
                        fq.status = "failed"
                        fq.last_error = f"Corrupt payload: {str(exc)[:200]}"
                        logging.critical(
                            "Fiscal retry: corrupt payload for order %d, marking failed: %s",
                            fq.order_id, fq.last_error,
                        )
                        from bot_handlers import alert_admin
                        await alert_admin(
                            f"⚠️ FiscalQueue #{fq.id} (заказ {fq.order_id}): повреждённый payload, "
                            f"требуется ручное исправление."
                        )
                    except Exception as exc:
                        fq.status = "pending"
                        fq.last_error = str(exc)[:500]
                        backoff_minutes = min(5 * 2 ** (fq.attempts - 1), 120)
                        fq.next_retry_at = database.now_utc() + timedelta(minutes=backoff_minutes)
                        logging.exception("Fiscal retry: exception for order %d", fq.order_id)

                    if fq.attempts >= fq.max_attempts and fq.status == "pending":
                        fq.status = "failed"
                        logging.critical(
                            "Fiscal retry: ИСЧЕРПАНЫ ПОПЫТКИ для заказа %d (54-ФЗ нарушение!)",
                            fq.order_id,
                        )
                        from bot_handlers import alert_admin
                        await alert_admin(
                            f"Фискализация заказа #{fq.order_id} провалилась после "
                            f"{fq.attempts} попыток! Возможно нарушение 54-ФЗ."
                        )

                    session.commit()
        except Exception:
            logging.exception("Ошибка в fiscal retry worker")

        await asyncio.sleep(120)


async def _sbp_payment_polling_worker() -> None:
    """Фоновая задача: опрос статуса pending SBP-платежей.

    Сбербанк делает только 3 попытки доставить callback (интервал 60 сек).
    Если callback потерян — этот worker поймает оплату через getOrderStatusExtended.
    """
    from payments.sbp import check_sbp_payment

    await asyncio.sleep(30)  # Ждём старт приложения

    while True:
        try:
            with database.db_session() as session:
                cutoff_recent = database.now_utc() - timedelta(seconds=30)
                cutoff_old = database.now_utc() - timedelta(minutes=ORDER_PAYMENT_TIMEOUT_MINUTES)
                pending_orders = session.scalars(
                    select(Order).where(
                        Order.payment_status == "pending",
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
                    result = await check_sbp_payment(gateway_order_id)
                    if not result.success:
                        continue

                    if result.is_paid:
                        expected_kopecks = total_amount * 100
                        if result.amount is None:
                            logging.error(
                                "SBP polling: amount=None for paid order %d, skipping — cannot verify",
                                order_id,
                            )
                            from bot_handlers import alert_admin
                            await alert_admin(
                                f"⚠️ SBP polling: заказ #{order_id} оплачен, но SBP API не вернул сумму. "
                                f"Требуется ручная проверка."
                            )
                            continue
                        if result.amount != expected_kopecks:
                            with database.db_session() as session:
                                order = session.scalars(
                                    select(Order).where(
                                        Order.id == order_id
                                    ).with_for_update()
                                ).first()
                                if order and order.payment_status == "pending":
                                    order.payment_status = "amount_mismatch"
                                    order.updated_at = database.now_utc()
                                    session.commit()
                                    logging.critical(
                                        "SBP polling: AMOUNT MISMATCH order %d expected %d got %d",
                                        order_id, expected_kopecks, result.amount,
                                    )
                                    from bot_handlers import alert_admin
                                    await alert_admin(
                                        f"AMOUNT MISMATCH (polling): заказ #{order_id}, "
                                        f"ожидали {expected_kopecks} коп, получили {result.amount} коп"
                                    )
                                else:
                                    session.commit()
                            continue

                        from routes import _process_paid_order
                        await _process_paid_order(order_id)
                        logging.info(
                            "SBP polling: заказ %d оплачен (callback не пришёл, обнаружен polling)",
                            order_id,
                        )

                except Exception:
                    logging.exception("SBP polling: ошибка проверки заказа %d", order_id)

            # Проверяем expired заказы с gateway_order_id — деньги могли списаться
            # после таймаута, но callback потерялся. Если оплачен — воскрешаем,
            # если нет — ничего не делаем (Сбербанк сам отменит по своему таймауту).
            with database.db_session() as session:
                expired_cutoff = database.now_utc() - timedelta(minutes=ORDER_PAYMENT_TIMEOUT_MINUTES)
                # Только недавно expired (не старше 2х таймаутов) — нет смысла проверять старые
                expired_floor = database.now_utc() - timedelta(minutes=ORDER_PAYMENT_TIMEOUT_MINUTES * 2)
                expired_orders = session.scalars(
                    select(Order).where(
                        Order.payment_status == "expired",
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
                    result = await check_sbp_payment(gw_oid)
                    if not result.success:
                        continue
                    if result.is_paid:
                        # Деньги списались после таймаута — воскрешаем заказ
                        logging.warning(
                            "SBP polling: expired заказ %d оплачен! Воскрешаем.", oid,
                        )
                        from routes import _process_paid_order
                        await _process_paid_order(oid)
                        from bot_handlers import alert_admin
                        await alert_admin(
                            f"Заказ #{onum} оплачен ПОСЛЕ таймаута (callback потерян). "
                            f"Заказ автоматически воскрешён."
                        )
                except Exception:
                    logging.exception("SBP polling: ошибка проверки expired заказа %d", oid)

        except Exception:
            logging.exception("Ошибка в SBP payment polling worker")

        await asyncio.sleep(15)
